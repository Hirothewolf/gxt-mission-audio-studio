# -*- coding: utf-8 -*-
"""Janela do lote automático de cutscenes: planejar, gerar tudo e revisar só as exceções."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk

from studio import config
from studio.audio import cutscene_batch as batch
from studio.audio import cutscene_studio as audio
from studio.core.utils import abrir_no_sistema
from studio.gxt.model import Gxt
from studio.script.native_index import load_current

MODOS = {'Seguir o tempo da legenda': 'janela', 'Duração livre': 'livre'}
RESULTADOS = {'OK': 'Substituída', 'WARN': 'Substituída (conferir)', 'KEPT_ORIGINAL': 'Mantida original',
              'MISSING_PTBR': 'Sem take PT-BR (silêncio)'}


class CutsceneBatchWindow:
    """Plano e execução em lote. O estúdio manual continua sendo o editor de exceções."""

    def __init__(self, app, autoload=True):
        self.app = app
        self.project = Path(config.get_project_root()).resolve()
        self.game = config.CFG.get('game_dir')
        self.report, self.tracks, self.plans, self.results = None, {}, {}, {}
        self.closed = False
        self.job = None
        self._lookup = None  # permite injetar textos em testes
        self.cancel_event = threading.Event()
        self.messages = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cutscene-batch')
        self.win = tk.Toplevel(app.root)
        self.win.title('Lote automático de cutscenes')
        self.win.geometry('1060x700')
        self.win.minsize(860, 560)
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        if autoload:
            self._load()
        self.win.after(150, self._poll)

    # ------------------------------------------------------------------ interface
    def _build(self):
        w = self.win
        w.columnconfigure(0, weight=1)
        w.rowconfigure(2, weight=1)
        ttk.Label(w, wraplength=1000, justify='left', padding=(12, 10, 12, 4), text=(
            'A janela de cada fala vem das legendas (.cut) do jogo, a referência de voz é o trecho original dessa '
            'legenda e o texto vem do GXT aberto no editor. Nada de diarização ou tempos manuais. O que não passar '
            'na conferência automática aparece em “Exceções”. No modo somente PT-BR, falas sem take ficam em silêncio.')).grid(row=0, column=0, sticky='ew')
        opt = ttk.Frame(w, padding=(12, 0, 12, 6))
        opt.grid(row=1, column=0, sticky='ew')
        ttk.Label(opt, text='Duração da fala:').pack(side='left')
        self.mode_var = tk.StringVar(value=next(iter(MODOS)))
        box = ttk.Combobox(opt, textvariable=self.mode_var, values=list(MODOS), state='readonly', width=26)
        box.pack(side='left', padx=(4, 14))
        ttk.Label(opt, text='Velocidade:').pack(side='left')
        self.speed_var = tk.StringVar(value='1.0')
        ttk.Entry(opt, textvariable=self.speed_var, width=5).pack(side='left', padx=(4, 14))
        ttk.Button(opt, text='Atualizar plano', command=self._replan).pack(side='left')
        self.background_var = tk.StringVar(value=next(iter(audio.BACKGROUND_MODES)))
        ttk.Combobox(opt, textvariable=self.background_var, values=list(audio.BACKGROUND_MODES),
                     state='readonly', width=42).pack(side='left', padx=8)
        pan = ttk.PanedWindow(w, orient='vertical')
        pan.grid(row=2, column=0, sticky='nsew', padx=12)
        top = ttk.LabelFrame(pan, text='Faixas (selecione uma ou várias)', padding=4)
        cols = (('faixa', 'Faixa', 90), ('cena', 'Cena', 150), ('falas', 'Falas', 55), ('geraveis', 'Geráveis', 70),
                ('avisos', 'Avisos', 60), ('excluidas', 'Excluídas', 75), ('estado', 'Estado', 330))
        self.tracks_tree = ttk.Treeview(top, columns=[c[0] for c in cols], show='headings', height=9,
                                        selectmode='extended')
        for key, title, width in cols:
            self.tracks_tree.heading(key, text=title)
            self.tracks_tree.column(key, width=width, anchor='w' if key in ('faixa', 'cena', 'estado') else 'e')
        sb = ttk.Scrollbar(top, orient='vertical', command=self.tracks_tree.yview)
        self.tracks_tree.configure(yscrollcommand=sb.set)
        self.tracks_tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tracks_tree.bind('<<TreeviewSelect>>', lambda _e: self._show_details())
        pan.add(top, weight=1)
        bottom = ttk.LabelFrame(pan, text='Exceções da faixa selecionada: só o que merece ouvir ou decidir', padding=4)
        cols2 = (('chave', 'Chave', 120), ('inicio', 'Início', 60), ('fim', 'Fim', 60), ('resultado', 'Resultado', 150),
                 ('motivo', 'Motivo', 360), ('acao', 'O que fazer', 230))
        self.exc_tree = ttk.Treeview(bottom, columns=[c[0] for c in cols2], show='headings', height=9)
        for key, title, width in cols2:
            self.exc_tree.heading(key, text=title)
            self.exc_tree.column(key, width=width, anchor='w')
        sb2 = ttk.Scrollbar(bottom, orient='vertical', command=self.exc_tree.yview)
        self.exc_tree.configure(yscrollcommand=sb2.set)
        self.exc_tree.pack(side='left', fill='both', expand=True)
        sb2.pack(side='right', fill='y')
        pan.add(bottom, weight=1)
        self.exc_tree.bind('<<TreeviewSelect>>', lambda _e: self._show_exception())
        self.detail_var = tk.StringVar(value='')
        ttk.Label(w, textvariable=self.detail_var, foreground='#555', padding=(12, 4, 12, 0), wraplength=1000,
                  justify='left').grid(row=3, column=0, sticky='ew')
        bar = ttk.Frame(w, padding=(12, 6))
        bar.grid(row=4, column=0, sticky='ew')
        self.btn_run_sel = ttk.Button(bar, text='Gerar faixas selecionadas', command=lambda: self._start(False))
        self.btn_run_sel.pack(side='left')
        self.btn_run_all = ttk.Button(bar, text='Gerar todas com falas geráveis', command=lambda: self._start(True))
        self.btn_run_all.pack(side='left', padx=6)
        self.btn_cancel = ttk.Button(bar, text='Cancelar', command=self._cancel, state='disabled')
        self.btn_cancel.pack(side='left')
        ttk.Button(bar, text='Abrir estúdio manual…', command=self.app.abrir_estudio_cutscenes).pack(side='right')
        ttk.Button(bar, text='Abrir pasta dos resultados', command=self._open_folder).pack(side='right', padx=6)
        ttk.Button(bar, text='Resumo CSV', command=self._export_csv).pack(side='right')
        self.progress = ttk.Progressbar(w, mode='determinate')
        self.progress.grid(row=5, column=0, sticky='ew', padx=12)
        self.status = tk.StringVar(value='Carregando o índice nativo…')
        ttk.Label(w, textvariable=self.status, padding=(12, 2, 12, 8)).grid(row=6, column=0, sticky='ew')

    # ------------------------------------------------------------------ plano
    def _policy(self):
        try:
            speed = float(self.speed_var.get().replace(',', '.'))
        except ValueError:
            raise ValueError('A velocidade precisa ser um número (ex.: 1.0).')
        if not 0.5 <= speed <= 2.0:
            raise ValueError('A velocidade precisa ficar entre 0,5 e 2,0.')
        variable = getattr(self, 'background_var', None)
        background_mode = audio.BACKGROUND_MODES[variable.get()] if variable else 'voice_only'
        return batch.Policy(duration_mode=MODOS[self.mode_var.get()], speed=speed, background_mode=background_mode)

    def _load(self):
        if not self.game or not Path(self.game).is_dir():
            self.status.set('Defina a pasta do GTA San Andreas em Jogo → Guia do projeto para carregar as cutscenes.')
            return
        def catalog(cancel_event=None, on_progress=None):
            report = load_current(self.project, self.game)
            if not report:
                raise ValueError('Índice nativo atual indisponível. Atualize-o em Jogo → Guia do projeto → Análise do SCM.')
            return report, audio.list_tracks(self.project, report, cancel_event, on_progress)
        self._submit('catalog', catalog)

    def _apply_catalog(self, report, tracks):
        self.report = report
        self.tracks = {row['track']: row for row in tracks}
        self._replan()

    def _replan(self):
        if not self.report:
            return
        try:
            policy = self._policy()
        except ValueError as exc:
            messagebox.showerror('Lote', str(exc), parent=self.win)
            return
        target = getattr(self.app, 'g', None)
        if self._lookup is not None:
            lookup = self._lookup
        else:
            native_info = (self.report.get('sources') or {}).get('gxt') or {}
            native = None
            try:
                if native_info.get('path') and audio.digest(native_info['path']) == native_info.get('sha256'):
                    native = Gxt(native_info['path'])
            except (OSError, ValueError):
                native = None
            lookup = batch.make_text_lookup(native, target)
        paths = (str(getattr(target, 'path', '') or ''), str(getattr(getattr(self.app, 'c', None), 'path', '') or ''))
        plans = batch.plan_all(self.report, lookup, None, policy, paths)
        self.plans = {name: items for name, items in plans.items() if name in self.tracks}
        self.tracks_tree.delete(*self.tracks_tree.get_children())
        for name, items in sorted(self.plans.items()):
            s = batch.summarize(items)
            scenes = ', '.join(sorted({i['scene'] for i in items if i['scene']}))
            self.tracks_tree.insert('', 'end', iid=name, values=(
                name, scenes, s['total'], s['generate'], s['WARN'], s['SKIP'], self._state_text(name)))
        total = sum(batch.summarize(i)['generate'] for i in self.plans.values())
        note = '' if target is not None else ' Abra o GXT do mod: sem ele nenhuma fala tem tradução.'
        self.status.set('%d faixas com legendas; %d falas prontas para gerar.%s' % (len(self.plans), total, note))
        self._show_details()

    def _state_text(self, name):
        result = self.results.get(name)
        if not result:
            return 'Não gerada'
        counts = {k: 0 for k in ('OK', 'WARN', 'KEPT_ORIGINAL', 'MISSING_PTBR')}
        for row in result['items']:
            counts[row['outcome']] += 1
        exc = len(batch.exceptions(result))
        return '%d substituídas · %d p/ conferir · %d sem PT-BR · %d exceções%s' % (
            counts['OK'] + counts['WARN'], counts['WARN'], counts['MISSING_PTBR'], exc,
            ' · CANCELADA' if result.get('cancelled') else '')

    def _show_details(self):
        self.exc_tree.delete(*self.exc_tree.get_children())
        selected = self.tracks_tree.selection()
        if len(selected) != 1:
            self.detail_var.set('')
            return
        name = selected[0]
        result = self.results.get(name)
        if result:
            rows = batch.exceptions(result)
            for index, row in enumerate(rows):
                codes = row['final_reasons']
                self.exc_tree.insert('', 'end', iid='%d' % index, values=(
                    row['key'], '%.2f' % row['start'], '%.2f' % row['end'], RESULTADOS[row['outcome']],
                    ' | '.join(batch.explain(c)['message'] for c in codes) + ((' — ' + row['error']) if row['error'] else ''),
                    ' | '.join(dict.fromkeys(batch.explain(c)['action'] for c in codes if batch.explain(c)['action']))))
            self.detail_var.set('Faixa de revisão: %s\nRascunho (abre no estúdio manual): %s' % (
                result['render'] or '(nada foi substituído)', result['draft'] or '—'))
        else:
            for index, row in enumerate(self.plans.get(name, [])):
                attention = row['status'] == 'WARN' or any(c in batch.ATTENTION_SKIPS for c in row['reasons'])
                if attention:
                    codes = row['reasons']
                    self.exc_tree.insert('', 'end', iid='%d' % index, values=(
                        row['key'], '%.2f' % row['start'], '%.2f' % row['end'],
                        'Excluída no plano' if row['status'] == 'SKIP' else 'Gerar com aviso',
                        ' | '.join(batch.explain(c)['message'] for c in codes),
                        ' | '.join(dict.fromkeys(batch.explain(c)['action'] for c in codes if batch.explain(c)['action']))))
            self.detail_var.set('Prévia do plano: o que pede atenção antes mesmo de gerar.')

    def _show_exception(self):
        selected = self.exc_tree.selection()
        if selected:
            chave, _i, _f, resultado, motivo, acao = self.exc_tree.item(selected[0], 'values')
            self.detail_var.set('%s — %s\nMotivo: %s\nO que fazer: %s' % (chave, resultado, motivo, acao))

    # ------------------------------------------------------------------ execução
    def _start(self, everything):
        if self.job:
            return
        if not self.plans:
            self.status.set('Nada para gerar: o índice não foi carregado.')
            return
        try:
            policy = self._policy()
        except ValueError as exc:
            messagebox.showerror('Lote', str(exc), parent=self.win)
            return
        names = batch.select_tracks(self.plans) if everything else [n for n in self.tracks_tree.selection()
                                                                      if batch.summarize(self.plans[n])['generate']]
        if not names:
            messagebox.showinfo('Lote', 'Nenhuma faixa selecionada tem falas geráveis (falta tradução no GXT aberto?).',
                                parent=self.win)
            return
        ok, reason = batch.omnivoice_disponivel(self.project)
        if not ok:
            messagebox.showerror('OmniVoice indisponível', reason, parent=self.win)
            return
        falas = sum(batch.summarize(self.plans[n])['generate'] for n in names)
        if not messagebox.askyesno('Gerar lote', (
                '%d faixa(s), %d fala(s).\n\nCada fala é gerada pelo OmniVoice local e conferida automaticamente; '
                'pode demorar. Falas já geradas com o mesmo texto são reaproveitadas. Nada é gravado no jogo nem no '
                'GXT.\n\nContinuar?') % (len(names), falas), parent=self.win):
            return
        self.cancel_event = threading.Event()
        self.btn_run_sel.state(['disabled']); self.btn_run_all.state(['disabled']); self.btn_cancel.state(['!disabled'])
        self.progress.configure(value=0, maximum=max(1, falas))
        self.job = self.executor.submit(self._worker, names, policy)

    def _worker(self, names, policy):
        factory = batch.omnivoice_factory(self.project, self.report, policy)
        done = 0
        for name in names:
            if self.cancel_event.is_set():
                break
            items = self.plans[name]

            def on_progress(count, total, message, base=done):
                self.messages.put(('progress', base + count, message))
            try:
                result = batch.run_track(self.project, self.report, self.tracks[name], items, factory, policy,
                                         self.cancel_event, on_progress)
            except batch.CancelledError:
                self.messages.put(('cancelled', name))
                break
            except (ValueError, RuntimeError, OSError) as exc:
                self.messages.put(('track_error', name, str(exc)))
                continue
            done += batch.summarize(items)['generate']
            self.messages.put(('track_done', name, result))
        self.messages.put(('finished', None))

    def _submit(self, kind, function):
        if self.job:
            return
        self.cancel_event = threading.Event()

        def run():
            try:
                self.messages.put((kind, function(cancel_event=self.cancel_event), None))
            except Exception as exc:  # limite da thread: qualquer erro vira mensagem, nunca trava a janela
                self.messages.put(('error', str(exc) or exc.__class__.__name__))
        self.job = self.executor.submit(run)

    def _cancel(self):
        self.cancel_event.set()
        self.status.set('Cancelando após a fala em andamento…')

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == 'catalog':
                    self.job = None
                    self._apply_catalog(*message[1])
                elif kind == 'error':
                    self.job = None
                    self.status.set(message[1])
                elif kind == 'progress':
                    self.progress.configure(value=message[1])
                    self.status.set(message[2])
                elif kind == 'track_done':
                    self.results[message[1]] = message[2]
                    self._refresh_row(message[1])
                elif kind == 'track_error':
                    self.status.set('%s: %s' % (message[1], message[2]))
                elif kind == 'cancelled':
                    self.status.set('Lote cancelado. O que já foi gerado fica guardado e é reaproveitado.')
                elif kind == 'finished':
                    self.job = None
                    self.btn_run_sel.state(['!disabled']); self.btn_run_all.state(['!disabled'])
                    self.btn_cancel.state(['disabled'])
                    if self.results:
                        batch.write_summary_csv(self.project, list(self.results.values()))
                    if not self.cancel_event.is_set():
                        self.status.set('Lote concluído. Resumo em workspace/cutscene_studio/batch/resumo.csv.')
        except queue.Empty:
            pass
        self.win.after(150, self._poll)

    def _refresh_row(self, name):
        if self.tracks_tree.exists(name):
            self.tracks_tree.set(name, 'estado', self._state_text(name))
        self._show_details()

    def _open_folder(self):
        folder = self.project / 'workspace' / 'cutscene_studio' / 'batch'
        folder.mkdir(parents=True, exist_ok=True)
        abrir_no_sistema(folder)

    def _export_csv(self):
        if not self.results:
            messagebox.showinfo('Resumo', 'Gere ao menos uma faixa antes de exportar o resumo.', parent=self.win)
            return
        path = batch.write_summary_csv(self.project, list(self.results.values()))
        self.status.set('Resumo salvo em %s' % path)

    def close(self):
        if self.job and not messagebox.askyesno('Lote em andamento', 'Cancelar o lote e fechar?', parent=self.win):
            return
        self.cancel_event.set()
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.win.destroy()
