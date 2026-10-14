# -*- coding: utf-8 -*-
"""Lote OmniVoice: um take verificado por fala, em série, com importação incremental.

O OmniVoice exige referência original e proveniência verificadas para CADA fala, e o runner
(`tools/omnivoice_tts.py`) grava um recibo e um ``redub_relatorio.json`` por take (modo
exclusivo). Por isso o lote não pode apontar todas as falas para a mesma pasta de saída:
cada fala recebe a sua própria subpasta ``<run>/lines/<JAMCRC>/output/<tabela>/`` e é
importada para revisão assim que termina. Cancelar ou falhar no meio preserva o que já saiu.

Este módulo não depende de Tk. A interface fornece callbacks.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from studio.tts.omnivoice import is_nonverbal_marker, spoken_text


@dataclass
class BatchItem:
    h: int
    key: str
    text: str
    command: list
    line_run: Path

    @property
    def output_dir(self):
        return self.line_run / 'output'


@dataclass
class BatchSummary:
    total: int = 0
    valid: int = 0
    quarantined: int = 0
    failed: list = field(default_factory=list)    # [(chave, motivo)]
    skipped: list = field(default_factory=list)   # [(chave, motivo)]
    imported: list = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    aborted: str = ''     # motivo quando o lote parou sozinho por um erro que se repete

    @property
    def attempted(self):
        return self.valid + self.quarantined + len(self.failed)


class BatchController:
    """Cancelamento cooperativo; imita a parte de ``ProcessJob`` que a janela usa."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self._job = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self.logs = []

    @property
    def done(self):
        return self._done.is_set()

    def finish(self):
        self._done.set()

    def set_job(self, job):
        with self._lock:
            self._job = job
        if self.cancel_event.is_set() and job is not None:
            job.cancel()

    def cancel(self):
        self.cancel_event.set()
        with self._lock:
            job = self._job
        if job is not None:
            job.cancel()


def line_run_dir(run, h):
    return Path(run) / 'lines' / ('%08X' % h)


def plan_lines(build_command, tab, hashes, texts, keys, run, limit=0, stop=None):
    """Decide o que gerar. ``build_command(line_run, h, text)`` devolve o comando do take.

    Falas sem texto falado, não verbais ou sem vínculo verificado são puladas COM motivo; nenhuma
    referência alternativa é inventada. ``limit`` (0 = todas) conta falas geráveis; ``stop()``
    interrompe o planejamento (cancelamento).
    """
    items, skipped = [], []
    for h in hashes:
        if (limit and len(items) >= limit) or (stop and stop()):
            break
        key = keys.get(h) or ('0x%08X' % h)
        text = texts.get(h, '')
        if not spoken_text(text):
            skipped.append((key, 'sem texto falado (vazio ou só tags)'))
            continue
        if is_nonverbal_marker(text):
            skipped.append((key, 'som não verbal; o OmniVoice gera falas'))
            continue
        line_run = line_run_dir(run, h)
        try:
            command = build_command(line_run, h, text)
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            skipped.append((key, str(exc)))
            continue
        items.append(BatchItem(h, key, text, list(command), line_run))
    return items, skipped


def result_is_timeout(result):
    return bool(result is not None and result.timed_out)


def _failure_reason(result):
    if result.error:
        return str(result.error)
    for line in reversed(result.logs or []):
        line = line.strip()
        if line.startswith('ValueError:') or line.startswith('RuntimeError:'):
            return line.split(':', 1)[1].strip()
    tail = [l.strip() for l in (result.logs or []) if l.strip()]
    return tail[-1][:300] if tail else 'o motor terminou com erro %s' % result.returncode


# Erros do próprio ambiente (não da fala) que costumam passar sozinhos: o runner recusa rodar se a
# GPU estiver acima do limite de uso logo após o processo anterior liberar o modelo.
TRANSIENT_MARKERS = ('GPU fora do limite seguro',)
TRANSIENT_RETRIES = 3
TRANSIENT_WAIT = 8.0
SAME_ERROR_ABORT = 3


def _transient(reason):
    return any(marker in reason for marker in TRANSIENT_MARKERS)


def run_batch(items, skipped, tab, submit, import_line, controller, on_line=None, cwd=None, timeout=600,
              wait=None):
    """Roda ``items`` em série.

    Falhas transitórias do ambiente (GPU ocupada) são repetidas algumas vezes com espera. Se a MESMA
    falha se repetir em ``SAME_ERROR_ABORT`` falas seguidas, o lote para em vez de queimar a tabela
    inteira: o problema é do ambiente, não das falas.

    ``submit(command, cwd, on_line, timeout)`` devolve um job (``wait()``/``cancel()``);
    ``import_line(item)`` importa o take para revisão e devolve o dict de contagens
    (``valid``, ``quarantined``, ``imported``).
    """
    log = on_line or (lambda _line: None)
    wait = wait or controller.cancel_event.wait   # espera interrompível pelo cancelamento
    streak = ['', 0]
    summary = BatchSummary(total=len(items), skipped=list(skipped))
    log('Total de falas a processar: %d' % len(items))
    for key, reason in skipped:
        log('[AVISO] %s pulada: %s' % (key, reason))
    try:
        for number, item in enumerate(items, 1):
            if controller.cancel_event.is_set():
                summary.cancelled = True
                break
            log('[%d/%d] %s' % (number, len(items), item.key))
            result, reason = None, ''
            for attempt in range(TRANSIENT_RETRIES + 1):
                try:
                    job = submit(item.command, cwd, log, timeout)
                except RuntimeError as exc:
                    result, reason = None, str(exc)
                    break
                controller.set_job(job)
                result = job.wait()
                controller.set_job(None)
                if result.cancelled or controller.cancel_event.is_set() or result.timed_out:
                    break
                if result.returncode == 0 and not result.error:
                    reason = ''
                    break
                reason = _failure_reason(result)
                if not _transient(reason) or attempt == TRANSIENT_RETRIES:
                    break
                log('[AVISO] %s: %s Nova tentativa em %.0f s (%d/%d).' % (
                    item.key, reason, TRANSIENT_WAIT, attempt + 1, TRANSIENT_RETRIES))
                wait(TRANSIENT_WAIT)
                if controller.cancel_event.is_set():
                    break
            if result is not None and (result.cancelled or controller.cancel_event.is_set()):
                summary.cancelled = True
                break
            if result is not None and result.timed_out:
                summary.timed_out = True
                reason = 'limite de tempo excedido'
            if reason:
                summary.failed.append((item.key, reason))
                log('[ERRO] %s: %s' % (item.key, reason))
                streak[:] = [reason, streak[1] + 1] if reason == streak[0] else [reason, 1]
                if streak[1] >= SAME_ERROR_ABORT and not result_is_timeout(result):
                    summary.aborted = reason
                    log('[ERRO] Lote interrompido: o mesmo erro se repetiu em %d falas seguidas. '
                        'Corrija o ambiente e rode de novo.' % streak[1])
                    break
                continue
            streak[:] = ['', 0]
            try:
                counts = import_line(item) or {}
            except Exception as exc:  # importação de uma fala não derruba o lote
                summary.failed.append((item.key, 'importação falhou: %s' % exc))
                log('[ERRO] %s: importação falhou: %s' % (item.key, exc))
                continue
            valid = int(counts.get('valid', 0))
            quarantined = int(counts.get('quarantined', 0))
            summary.valid += valid
            summary.quarantined += quarantined
            summary.imported.extend(counts.get('imported', []))
            if valid:
                log('[OK] %s: candidato pendente de revisão' % item.key)
            elif quarantined:
                log('[AVISO] %s: take em quarentena (duração/sinal); áudio preservado no run' % item.key)
            else:
                summary.failed.append((item.key, 'nenhum WAV válido foi produzido'))
                log('[ERRO] %s: nenhum WAV válido foi produzido' % item.key)
    finally:
        controller.finish()
    return summary
