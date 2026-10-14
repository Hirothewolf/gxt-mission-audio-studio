# -*- coding: utf-8 -*-
"""
Lote automático de CUTSCENE: planejar, gerar, conferir e montar sem recorte manual.

Fluxo (tudo derivado dos arquivos da instalação, sem diarização nem timestamps manuais):
  cena -> faixa -> janela da legenda (.cut) -> chave GXT -> texto EN (referência) e PT (alvo)

* ``plan_track`` decide, ANTES de gerar, o que é gerável e o que será mantido original,
  com o motivo de cada exclusão.
* ``run_track`` gera cada fala, confere o take com critérios objetivos e monta a faixa.
  O padrão usa apenas takes PT-BR sobre silêncio; falhas aparecem como falas ausentes.
  Preservar áudio original exige a opção explícita ``preserve_original``.
* Resultados são determinísticos: mesmo plano + mesmos parâmetros = mesmos nomes de arquivo.
  Repetir o lote reaproveita o que já existe e só gera o que mudou.
* Nada aqui altera o jogo nem o GXT. As janelas das legendas são pontos de partida de
  tempo, não alinhamento fonético: a faixa CUTSCENE mistura voz, música e efeitos, e a
  substituição troca todo o áudio da janela.
"""
from __future__ import annotations

import array
import csv
import hashlib
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from studio.audio import cutscene_studio as audio
from studio.core.utils import CREATE_FLAGS, json_atomico
from studio.tts.omnivoice import is_nonverbal_marker, spoken_text

CancelledError = audio.CancelledError


@dataclass(frozen=True)
class Policy:
    """Critérios objetivos do lote. Os valores padrão são conservadores."""
    min_ref_seconds: float = 1.2     # janela menor que isso não serve de referência de voz
    warn_ref_seconds: float = 3.0    # referência curta: avisa que o resultado pode variar
    warn_cps: float = 16.0           # caracteres falados por segundo: já é rápido
    block_cps: float = 24.0          # acima disso a fala não cabe na janela
    fit_soft: float = 1.08           # avisa extensão natural; nunca ajusta velocidade
    fit_hard: float = 1.20           # acima disso: mantém o original
    short_ratio: float = 0.55        # take bem menor que a janela deixa silêncio demais
    silent_rms: float = 0.004        # RMS (0..1) abaixo disso = take silencioso
    clip_ratio: float = 0.01         # fração de amostras no limite
    duration_mode: str = 'janela'    # 'janela' = pede ao motor o tamanho da legenda; 'livre'
    speed: float = 1.0
    seed: int = 20261009
    allow_overlap: bool | None = None
    background_mode: str = 'voice_only'

    def __post_init__(self):
        audio.validate_background_mode(self.background_mode)
        if self.allow_overlap is None:
            object.__setattr__(self, 'allow_overlap', self.background_mode == 'voice_only')

    def synth_signature(self):
        return json.dumps([self.speed, self.duration_mode, self.seed])


# código -> (gravidade, mensagem, o que fazer)
REASONS = {
    'AMBIGUOUS': ('skip', 'Cena ambígua no índice nativo.', 'Resolva no estúdio manual.'),
    'NO_PT': ('skip', 'Sem tradução no GXT aberto.', 'Traduza a linha e rode de novo.'),
    'PT_EQUALS_EN': ('skip', 'Tradução idêntica ao original (ainda não traduzida).', 'Traduza a linha.'),
    'NO_EN_SPEECH': ('skip', 'Legenda original sem fala (só tags).', 'Nada a fazer.'),
    'EN_NONVERBAL': ('skip', 'Original é um som sem fala (grito, risada…).', 'Mantém o áudio original.'),
    'NO_PT_SPEECH': ('skip', 'Tradução sem texto falado (só tags).', 'Confira o GXT.'),
    'PT_NONVERBAL': ('skip', 'Alvo é som sem fala; o motor gera falas.', 'Mantém o áudio original.'),
    'REF_TOO_SHORT': ('skip', 'Janela curta demais para servir de referência de voz.',
                      'Use o estúdio manual com uma referência do banco.'),
    'RATE_TOO_HIGH': ('skip', 'Texto longo demais para a janela da legenda.',
                      'Encurte a tradução.'),
    'OUT_OF_TRACK': ('skip', 'Legenda fora da duração da faixa.', 'Confira o índice nativo.'),
    'DUPLICATE_WINDOW': ('skip', 'Janela repetida de outra legenda.', 'Nada a fazer.'),
    'REF_SHORT': ('warn', 'Referência curta (< 3 s); a voz pode variar.', 'Ouça esta fala.'),
    'OVERLAP_REQUIRES_REVIEW': ('skip', 'Falas simultâneas sem autorização de mistura.',
                              'Confira as referências e permita mistura simultânea no estúdio.'),
    'OVERLAP_MIX': ('warn', 'Falas simultâneas serão misturadas no tempo original.', 'Ouça as duas vozes e confira as referências.'),
    'OVERLAP_PARTIAL_PTBR': ('warn', 'Mistura PT-BR incompleta: outra fala simultânea ficará ausente.',
                           'Gere o take que falta; nenhum áudio original foi inserido.'),
    'OVERLAP_GROUP_INCOMPLETE': ('fail', 'Grupo simultâneo mantido original porque falta um take aprovado.',
                               'Resolva as falas excluídas ou que falharam antes de substituir o grupo.'),
    'RATE_HIGH': ('warn', 'Fala rápida para a janela.', 'Considere encurtar a tradução.'),
    'CLIPPED_TO_TRACK_END': ('warn', 'Janela cortada no fim da faixa.', 'Ouça o final.'),
    'TAKE_EXTENDED': ('warn', 'Take maior que a janela: fim estendido sem acelerar.', 'Confira o encontro das falas.'),
    'TAKE_OUT_OF_TRACK': ('fail', 'Take ultrapassa o fim da faixa; não foi cortado nem aplicado.', 'Escolha outro take ou revise a duração nativa.'),
    'NATURAL_OVERLAP_REQUIRES_REVIEW': ('fail', 'O take natural cruza outra fala; mistura não permitida nesta montagem.',
                                     'Permita falas simultâneas ou revise os takes sem cortá-los.'),
    'TAKE_SHORT': ('warn', 'Take bem menor que a janela: sobra silêncio.', 'Ouça se cortou música/efeito.'),
    'TAKE_CLIPPED': ('warn', 'Take com picos saturados.', 'Ouça distorção.'),
    'TAKE_SILENT': ('fail', 'Take silencioso ou vazio.', 'Gere de novo.'),
    'TAKE_TOO_LONG': ('fail', 'Take muito maior que a janela; não aplicado.', 'Encurte a tradução.'),
    'GENERATION_FAILED': ('fail', 'O motor de voz falhou nesta fala.', 'Veja a mensagem de erro no relatório.'),
}


def _sha(*parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _norm(text):
    return ' '.join(spoken_text(text).casefold().split())


def make_text_lookup(native_gxt, target_gxt):
    """Devolve ``f(tabela, hash) -> (texto_EN_original, texto_PT_atual)``."""
    from studio.gxt.codec import decode_text_c

    def read(gxt, table, value, field):
        try:
            return decode_text_c(gxt.t[table][field][value])
        except (AttributeError, KeyError, TypeError):
            return ''
    return lambda table, value: (read(native_gxt, table, value, 'orig'), read(target_gxt, table, value, 'cur'))


# --------------------------------------------------------------------------- planejamento
def plan_track(report, track_name, text_lookup, track_duration=None, policy=None,
               gxt_paths=('', '')):
    """Lista as falas da faixa, em ordem de tempo, com status READY / WARN / SKIP."""
    policy = policy or Policy()
    records = [r for r in (report or {}).get('records', [])
               if r.get('pak') == 'CUTSCENE' and (r.get('stream') or {}).get('track') == track_name
               and r.get('gxt_stable_id')]
    records.sort(key=lambda r: (r.get('caption_start_ms', 0), r.get('caption_duration_ms', 0), r['gxt_stable_id']))
    items, seen = [], set()
    for record in records:
        gid = record['gxt_stable_id']
        table, crc = gid.split(':', 1)
        start_ms, dur_ms = int(record.get('caption_start_ms', 0)), int(record.get('caption_duration_ms', 0))
        en, pt = text_lookup(table, int(crc, 16))
        items.append({
            'id': _sha(track_name, gid, start_ms, dur_ms)[:12], 'gid': gid, 'table': table, 'hash': crc.upper(),
            'key': record.get('event_name') or crc, 'scene': record.get('scene'), 'track': track_name,
            'start': round(start_ms / 1000, 3), 'end': round((start_ms + dur_ms) / 1000, 3),
            'en_text': en, 'pt_text': pt, 'reasons': [], 'cps': None,
            'ambiguous': bool(record.get('cutscene_ambiguous')),
            'gxt_path': gxt_paths[0], 'original_gxt_path': gxt_paths[1]})
    overlapping = set()
    for index, left in enumerate(items):
        for right_index, right in enumerate(items[index + 1:], index + 1):
            if right['start'] >= left['end']:
                break
            if left['gid'] != right['gid'] and left['start'] < right['end'] and right['start'] < left['end']:
                overlapping.update((index, right_index))
    for index, item in enumerate(items):
        reasons = item['reasons']
        start, end = item['start'], item['end']
        key = (item['gid'], start, end)
        if key in seen:
            reasons.append('DUPLICATE_WINDOW')
        seen.add(key)
        if item['ambiguous']:
            reasons.append('AMBIGUOUS')
        if index in overlapping:
            reasons.append('OVERLAP_MIX' if policy.allow_overlap else 'OVERLAP_REQUIRES_REVIEW')
        if track_duration is not None:
            if start >= track_duration:
                reasons.append('OUT_OF_TRACK')
            elif end > track_duration:
                item['end'] = end = round(track_duration, 3)
                reasons.append('CLIPPED_TO_TRACK_END')
        length = end - start
        spoken_pt, spoken_en = spoken_text(item['pt_text']), spoken_text(item['en_text'])
        if not item['pt_text'].strip():
            reasons.append('NO_PT')
        elif not spoken_pt:
            reasons.append('NO_PT_SPEECH')
        elif is_nonverbal_marker(item['pt_text']):
            reasons.append('PT_NONVERBAL')
        elif _norm(item['pt_text']) == _norm(item['en_text']):
            reasons.append('PT_EQUALS_EN')
        if not spoken_en:
            reasons.append('NO_EN_SPEECH')
        elif is_nonverbal_marker(item['en_text']):
            reasons.append('EN_NONVERBAL')
        if length < policy.min_ref_seconds:
            reasons.append('REF_TOO_SHORT')
        elif length < policy.warn_ref_seconds:
            reasons.append('REF_SHORT')
        if spoken_pt and length > 0:
            item['cps'] = round(len(spoken_pt) / length, 1)
            if item['cps'] > policy.block_cps:
                reasons.append('RATE_TOO_HIGH')
            elif item['cps'] > policy.warn_cps:
                reasons.append('RATE_HIGH')
        item['reasons'] = list(dict.fromkeys(reasons))
        severities = {REASONS[code][0] for code in item['reasons']}
        item['status'] = 'SKIP' if 'skip' in severities else 'WARN' if 'warn' in severities else 'READY'
    return items


def plan_all(report, text_lookup, durations=None, policy=None, gxt_paths=('', '')):
    """Plano de todas as faixas que têm legendas de cutscene. ``{faixa: [itens]}``."""
    names = sorted({(r.get('stream') or {}).get('track') for r in (report or {}).get('records', [])
                    if r.get('pak') == 'CUTSCENE'} - {None})
    durations = durations or {}
    return {name: plan_track(report, name, text_lookup, durations.get(name), policy, gxt_paths) for name in names}


def summarize(items):
    counts = {'READY': 0, 'WARN': 0, 'SKIP': 0}
    for item in items:
        counts[item['status']] += 1
    counts['total'] = len(items)
    counts['generate'] = counts['READY'] + counts['WARN']
    return counts


def select_tracks(plans, spec=None):
    """'todas'/None = faixas com algo a gerar; ou nomes separados por vírgula (Track_016 ou 16)."""
    if not spec or str(spec).strip().lower() in ('todas', 'all'):
        return [name for name, items in sorted(plans.items()) if summarize(items)['generate']]
    chosen = []
    for part in str(spec).replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        name = part if part.startswith('Track_') else 'Track_%03d' % int(part)
        if name not in plans:
            raise ValueError('Faixa sem legendas de cutscene no índice: %s' % name)
        chosen.append(name)
    return chosen


def format_plan_table(plans):
    """Tabela de texto simples com o plano de cada faixa."""
    lines = ['%-10s %-24s %6s %8s %7s %9s' % ('Faixa', 'Cena', 'Falas', 'Geráveis', 'Avisos', 'Excluídas')]
    total = {'total': 0, 'generate': 0, 'WARN': 0, 'SKIP': 0}
    for name, items in sorted(plans.items()):
        s = summarize(items)
        scenes = ','.join(sorted({i['scene'] for i in items if i['scene']}))[:24]
        lines.append('%-10s %-24s %6d %8d %7d %9d' % (name, scenes, s['total'], s['generate'], s['WARN'], s['SKIP']))
        for key in total:
            total[key] += s[key]
    lines.append('%-10s %-24s %6d %8d %7d %9d' % ('TOTAL', '', total['total'], total['generate'], total['WARN'], total['SKIP']))
    return '\n'.join(lines)


def explain(code):
    severity, message, action = REASONS.get(code, ('warn', code, ''))
    return {'code': code, 'severity': severity, 'message': message, 'action': action}


# --------------------------------------------------------------------------- conferência do take
def analyze_take(path, policy=None):
    """Métricas objetivas de um WAV (decodificado para mono 24 kHz)."""
    raw = audio._decode_raw(Path(path), 24000, 1)
    values = array.array('h')
    values.frombytes(raw)
    if os.sys.byteorder != 'little':
        values.byteswap()
    count = len(values)
    if not count:
        return {'duration': 0.0, 'rms': 0.0, 'peak': 0.0, 'clip_ratio': 0.0}
    square = sum(v * v for v in values)
    peak = max(max(values), -min(values))
    clipped = sum(1 for v in values if v >= 32700 or v <= -32700)
    return {'duration': count / 24000, 'rms': math.sqrt(square / count) / 32768.0,
            'peak': peak / 32768.0, 'clip_ratio': clipped / count}


def judge_take(metrics, window, policy):
    """('OK'|'WARN'|'FAIL', [códigos])."""
    if metrics['duration'] < 0.15 or metrics['rms'] < policy.silent_rms:
        return 'FAIL', ['TAKE_SILENT']
    ratio = metrics['duration'] / window
    if ratio > policy.fit_hard:
        return 'FAIL', ['TAKE_TOO_LONG']
    codes = []
    if ratio > policy.fit_soft:
        codes.append('TAKE_EXTENDED')
    if ratio < policy.short_ratio:
        codes.append('TAKE_SHORT')
    if metrics['clip_ratio'] > policy.clip_ratio:
        codes.append('TAKE_CLIPPED')
    return ('WARN' if codes else 'OK'), codes


# --------------------------------------------------------------------------- motor de voz
def omnivoice_synthesizer(project, report, track_data, policy=None, python_path=None, script_path=None,
                          timeout=1800):
    """Sintetizador padrão: chama o runner OmniVoice local, uma fala por vez."""
    policy = policy or Policy()
    native = (report.get('sources') or {}).get('gxt') or {}

    def synthesize(item, reference, output, cancel_event=None):
        from studio.tts.omnivoice import build_command
        gxt_path = Path(native.get('path') or '')
        if not gxt_path.is_file() or audio.digest(gxt_path) != native.get('sha256'):
            raise ValueError('GXT original inglês mudou desde a indexação nativa.')
        first, last = audio._frame_bounds(item['start'], item['end'], track_data['sample_rate'], track_data['frames'])
        provenance = {
            'source_kind': 'verified_native_cutscene_window',
            'source_path': track_data['source_path'], 'source_sha256': track_data['source_sha256'],
            'archive_path': track_data.get('archive_path'), 'archive_sha256': track_data.get('archive_sha256'),
            'reference_start_frame': first, 'reference_end_frame': last,
            'source_sample_rate': int(track_data['sample_rate']),
            'gxt_stable_id': item['gid'], 'gxt_source_path': str(gxt_path), 'gxt_source_sha256': native['sha256'],
            'timing_source': 'native_cut_caption_window_batch',
            'base_english_gxt_path': str(gxt_path), 'base_english_gxt_sha256': native['sha256'],
            'base_english_text': item['en_text'], 'base_english_gxt_id': item['gid'],
            'reference_transcript_authority': 'NATIVE_GXT_CAPTION_NOT_BLIND_ASR',
            'reference_asr_verification': 'PENDING', 'caption_window_is_alignment_proof': False,
            'segment_seconds': [item['start'], item['end']], 'track': track_data.get('track'),
            'production_approved': False}
        window = item['end'] - item['start']
        request = output.with_suffix('.omnivoice-request.json')
        if request.exists():  # tentativa anterior interrompida: preserva o pedido antigo
            request.rename(request.with_name('%s.falhou-%d.json' % (request.stem, int(time.time()))))
        command = build_command(project, reference['path'], spoken_text(item['en_text']), item['pt_text'], output,
                                provenance=provenance, speed=policy.speed,
                                duration=window if policy.duration_mode == 'janela' else None,
                                python_path=python_path, script_path=script_path, window_seconds=window,
                                seed=policy.seed, request_path=request)
        _run(command, cancel_event, timeout)
    return synthesize


def omnivoice_factory(project, report, policy=None, **kwargs):
    """Fábrica para ``run_track``: recebe os dados da faixa quando ela já foi decodificada."""
    def factory(track_data):
        return omnivoice_synthesizer(project, report, track_data, policy, **kwargs)
    factory.is_factory = True
    return factory


def omnivoice_disponivel(project):
    """(ok, motivo): confere o runtime local antes de iniciar um lote longo."""
    from studio import config
    from studio.tts.omnivoice import _runtime_python
    python = Path(_runtime_python(Path(project).resolve()))
    script = Path(config.APP_DIR) / 'tools' / 'omnivoice_tts.py'
    if not python.is_file():
        return False, 'Python do OmniVoice não encontrado: %s' % python
    if not script.is_file():
        return False, 'tools/omnivoice_tts.py não encontrado.'
    return True, ''


def _run(command, cancel_event, timeout):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_FLAGS, text=True, encoding='utf-8', errors='replace')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill(); proc.communicate()
                    raise CancelledError('Operação cancelada.')
                if time.monotonic() > deadline:
                    proc.kill(); proc.communicate()
                    raise RuntimeError('O motor de voz excedeu o limite de %d s.' % timeout)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        raise
    if proc.returncode:
        tail = ' | '.join((out or '').strip().splitlines()[-3:])
        raise RuntimeError(tail or 'O motor de voz terminou com erro %s.' % proc.returncode)


# --------------------------------------------------------------------------- execução
def batch_dir(project, track_name):
    return Path(project).resolve() / 'workspace' / 'cutscene_studio' / 'batch' / track_name


def _take_name(track_data, item, policy):
    return '%s_%s.wav' % (item['id'], _sha(track_data['source_sha256'], item['start'], item['end'],
                                           item['pt_text'], item['en_text'], policy.synth_signature())[:12])


def _ensure_reference(track_data, item, folder):
    path = folder / 'refs' / ('%s_%s.wav' % (item['id'], track_data['source_sha256'][:8]))
    if not path.is_file():
        audio.export_reference(track_data, item.get('reference_start', item['start']), item.get('reference_end', item['end']), path)
    info = audio.import_replacement(path)
    return {'path': str(path), 'sha256': info['sha256'], 'actor_id': 'original_segment_window',
            'source_kind': 'verified_native_cutscene_window'}


def _ensure_take(track_data, item, folder, policy, synthesize, cancel_event):
    take = folder / 'takes' / _take_name(track_data, item, policy)
    qc_path = take.with_suffix('.qc.json')
    if take.is_file() and qc_path.is_file():
        qc = json.loads(qc_path.read_text(encoding='utf-8'))
        if qc.get('sha256') == audio.digest(take):
            return take, qc, True
    reference = _ensure_reference(track_data, item, folder)
    take.parent.mkdir(parents=True, exist_ok=True)
    part = take.with_name(take.stem + '.part.wav')
    part.unlink(missing_ok=True)
    synthesize(item, reference, part, cancel_event)
    if not part.is_file():
        raise RuntimeError('O motor terminou sem gerar o arquivo (talvez o take tenha ido para quarentena).')
    part.replace(take)
    metrics = analyze_take(take, policy)
    qc = dict(metrics, sha256=audio.digest(take))
    json_atomico(qc_path, qc)
    return take, qc, False


def run_track(project, report, track, items, synthesize, policy=None, cancel_event=None, on_progress=None):
    """Gera, confere e monta uma faixa. Devolve o relatório (também salvo em disco)."""
    policy = policy or Policy()
    project = Path(project).resolve()
    folder = batch_dir(project, track['track'])
    missing_outcome = 'MISSING_PTBR' if policy.background_mode == 'voice_only' else 'KEPT_ORIGINAL'
    results = [dict(item, outcome=missing_outcome, final_reasons=list(item['reasons']), take=None, qc=None, error=None)
               for item in items]
    todo = [r for r in results if r['status'] != 'SKIP']
    result = {'schema': 1, 'track': track['track'], 'policy': policy.__dict__.copy(), 'items': results,
              'background_mode': policy.background_mode, 'missing_ptbr': [],
              'render': None, 'render_info': None, 'draft': None, 'cancelled': False, 'plan_digest': None}
    track_data, replacements, ranges, segments = None, [], [], []
    try:
        if todo or policy.background_mode == 'voice_only':
            track_data = audio.load_track(track, cancel_event, on_progress)
            if todo and getattr(synthesize, 'is_factory', False):
                synthesize = synthesize(track_data)
        for number, row in enumerate(todo, 1):
            audio._check_cancel(cancel_event)
            audio._progress(on_progress, '%s: %s (%d/%d)' % (track['track'], row['key'], number, len(todo)),
                            number - 1, len(todo))
            try:
                audio._frame_bounds(row['start'], row['end'], track_data['sample_rate'], track_data['frames'])
                take, qc, reused = _ensure_take(track_data, row, folder, policy, synthesize, cancel_event)
            except CancelledError:
                raise
            except (ValueError, RuntimeError, OSError) as exc:
                row['final_reasons'].append('GENERATION_FAILED')
                row['error'] = str(exc)
                continue
            verdict, codes = judge_take(qc, row['end'] - row['start'], policy)
            row['take'], row['qc'], row['reused'] = str(take), qc, reused
            row['final_reasons'] = list(dict.fromkeys(row['final_reasons'] + codes))
            if verdict == 'FAIL':
                continue
            replacement = audio.import_replacement(take)
            if replacement['duration'] > row['end'] - row['start']:
                rate = track_data['sample_rate']
                end_frame = round(row['start'] * rate) + math.ceil(replacement['duration'] * rate - 1e-6)
                row['take_end_seconds'] = end_frame / rate
                if end_frame > track_data['frames']:
                    row['final_reasons'].append('TAKE_OUT_OF_TRACK')
                    continue
                row['reference_start'], row['reference_end'] = row['start'], row['end']
                row['caption_end'] = row['end']
                row['end'] = end_frame / rate
                if 'TAKE_EXTENDED' not in row['final_reasons']:
                    row['final_reasons'].append('TAKE_EXTENDED')
                verdict = 'WARN'
            row['outcome'] = 'WARN' if (verdict == 'WARN' or row['status'] == 'WARN') else 'OK'
            replacements.append(replacement)
            ranges.append({'start': row['start'], 'end': row['end']})
            segments.append(row)
        if not policy.allow_overlap:
            blocked = set()
            for index, left in enumerate(segments):
                for right_index, right in enumerate(segments[index + 1:], index + 1):
                    if left['start'] < right['end'] and right['start'] < left['end']:
                        blocked.update((index, right_index))
            for index in blocked:
                segments[index]['outcome'] = missing_outcome
                segments[index]['final_reasons'].append('NATURAL_OVERLAP_REQUIRES_REVIEW')
            approved = [(row, replacement, bounds) for index, (row, replacement, bounds) in enumerate(zip(segments, replacements, ranges))
                        if index not in blocked]
            segments = [entry[0] for entry in approved]
            replacements = [entry[1] for entry in approved]
            ranges = [entry[2] for entry in approved]
        # Clearing one approved window would also erase an overlapping speaker
        # whose take failed or was skipped. Preserve that connected group.
        if policy.allow_overlap and policy.background_mode == 'preserve_original':
            while True:
                kept = [row for row in results if row['outcome'] == 'KEPT_ORIGINAL'
                        and 'DUPLICATE_WINDOW' not in row['reasons']]
                blocked = [row for row in segments if any(
                    row['start'] < other['end'] and other['start'] < row['end'] for other in kept)]
                if not blocked:
                    break
                for row in blocked:
                    row['outcome'] = 'KEPT_ORIGINAL'
                    row['final_reasons'].append('OVERLAP_GROUP_INCOMPLETE')
                approved = [(row, replacement, bounds) for row, replacement, bounds in zip(segments, replacements, ranges)
                            if row['outcome'] != 'KEPT_ORIGINAL']
                segments = [entry[0] for entry in approved]
                replacements = [entry[1] for entry in approved]
                ranges = [entry[2] for entry in approved]
        audio._progress(on_progress, '%s: montando a faixa de revisão…' % track['track'], len(todo), len(todo))
        result['missing_ptbr'] = [{'gid': row['gid'], 'key': row['key'], 'start': row['start'], 'end': row['end'],
                                  'reasons': row['final_reasons']} for row in results if row['outcome'] == missing_outcome]
        if policy.background_mode == 'voice_only':
            for row in segments:
                if any(row['start'] < missing['end'] and missing['start'] < row['end'] for missing in result['missing_ptbr']):
                    row['outcome'] = 'WARN'
                    row['final_reasons'].append('OVERLAP_PARTIAL_PTBR')
        if replacements or policy.background_mode == 'voice_only':
            digest = _sha('background_mix_v3_native_duration', policy.allow_overlap, policy.background_mode, track_data['source_sha256'],
                          [[r['id'], r['take'], r['start'], r['end']] for r in segments], result['missing_ptbr'])
            result['plan_digest'] = digest
            dest = folder / ('%s_revisao_%s.wav' % (track['track'], digest[:10]))
            metadata = dest.with_suffix('.mix.json')
            if not dest.is_file():
                result['render_info'] = audio.render_track(track_data, replacements, ranges, dest, fit=False,
                                   cancel_event=cancel_event, on_progress=on_progress, allow_overlap=policy.allow_overlap,
                                   background_mode=policy.background_mode, missing_ptbr=result['missing_ptbr'])
            elif metadata.is_file():
                result['render_info'] = json.loads(metadata.read_text(encoding='utf-8'))
                if result['render_info'].get('sha256') != audio.digest(dest):
                    raise ValueError('A faixa de revisão em cache foi alterada.')
            else:
                raise ValueError('Faixa de revisão sem comprovante da mistura; preserve-a e escolha outro lote.')
            result['render'] = str(dest)
            result['draft'] = str(_save_draft(project, track_data, segments, replacements, digest,
                                            policy.allow_overlap, policy.background_mode))
    except CancelledError:
        result['cancelled'] = True
        _write_report(folder, result)
        raise
    _write_report(folder, result)
    return result


def _save_draft(project, track_data, rows, replacements, digest, allow_overlap=False, background_mode='voice_only'):
    draft_id = hashlib.sha256((track_data['source_sha256'] + digest).encode()).hexdigest()[:32]
    existing = Path(project) / 'workspace' / 'cutscene_studio' / 'drafts' / (draft_id + '.json')
    if existing.is_file():  # pode já ter sido ajustado à mão no estúdio: não sobrescreve
        return existing
    segments, gxt_rows = [], []
    for row, replacement in zip(rows, replacements):
        snapshot = {'stable_id': row['gid'], 'table': row['table'], 'hash': row['hash'], 'scene': row['scene'],
                    'key': row['key'], 'original_text': row['en_text'], 'comparison_text': row['en_text'],
                    'hypertranslated_text': row['pt_text'], 'authority': 'current editor GXT hypertranslation',
                    'gxt_path': row['gxt_path'], 'original_gxt_path': row['original_gxt_path']}
        reference = _ensure_reference(track_data, row, batch_dir(project, row['track']))
        segments.append({'start': row['start'], 'end': row['end'], 'gxt': snapshot, 'gxt_row_id': None,
                         'actor_id': 'original_segment_window', 'reference': reference,
                         'reference_transcript': spoken_text(row['en_text']), 'replacement': replacement})
        gxt_rows.append(snapshot)
    return audio.save_draft(project, track_data, segments, gxt_rows, draft_id=draft_id, fit=False,
                            allow_overlap=allow_overlap, background_mode=background_mode)


def _write_report(folder, result):
    folder.mkdir(parents=True, exist_ok=True)
    json_atomico(folder / 'report.json', result)


# Exclusões que pedem uma decisão humana. "Sem tradução" ou "sem fala" são rotina, não exceção.
ATTENTION_SKIPS = {'AMBIGUOUS', 'RATE_TOO_HIGH', 'REF_TOO_SHORT', 'OUT_OF_TRACK', 'OVERLAP_REQUIRES_REVIEW'}


def needs_attention(row):
    codes = row['final_reasons']
    if row['outcome'] == 'MISSING_PTBR':
        return True
    if row['outcome'] == 'WARN':
        return True
    return row['outcome'] == 'KEPT_ORIGINAL' and any(
        REASONS.get(c, ('', '', ''))[0] == 'fail' or c in ATTENTION_SKIPS for c in codes)


def exceptions(result):
    """Só o que merece olhar: avisos, falhas e exclusões que dependem de decisão."""
    return [row for row in result['items'] if needs_attention(row)]


def write_summary_csv(project, results, destination=None):
    """Um CSV com todas as falas de todas as faixas processadas (sobrescrito a cada lote)."""
    destination = Path(destination or (Path(project).resolve() / 'workspace' / 'cutscene_studio' / 'batch' / 'resumo.csv'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=';')
        writer.writerow(['faixa', 'chave', 'inicio_s', 'fim_s', 'resultado', 'motivos', 'o_que_fazer',
                         'caracteres_por_s', 'texto_original', 'texto_traduzido', 'take', 'erro'])
        for result in results:
            for row in result['items']:
                codes = row['final_reasons']
                writer.writerow([row['track'], row['key'], row['start'], row['end'], row['outcome'],
                                 ' | '.join(REASONS.get(c, (0, c, ''))[1] for c in codes),
                                 ' | '.join(dict.fromkeys(REASONS.get(c, (0, '', ''))[2] for c in codes if REASONS.get(c, (0, '', ''))[2])),
                                 row['cps'] if row['cps'] is not None else '', row['en_text'], row['pt_text'],
                                 row['take'] or '', row['error'] or ''])
    temporary.replace(destination)
    return destination
