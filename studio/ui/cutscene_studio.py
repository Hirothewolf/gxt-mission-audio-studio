"""Waveform editor for reversible, time-aligned CUTSCENE replacements."""
from __future__ import annotations

import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from studio import config
from studio.audio import cutscene_studio as audio
from studio.script.native_index import load_current
from studio.gxt.model import Gxt
from studio.gxt.codec import decode_text_c


class CutsceneStudioWindow:
    """A non-destructive cutscene editor attached to the main app."""

    def __init__(self, app):
        self.app = app
        self.project = Path(config.get_project_root()).resolve()
        self.game = config.CFG.get('game_dir')
        self.report = None
        self.native_gxt = None
        self.closed = False
        self.track_rows = {}
        self.gxt_rows = {}
        self.track_data = None
        self.track = None
        self.segments = []
        self.current_reference = None
        self.current_reference_transcript = ''
        self.current_reference_row_id = None
        self.reference_pending = False
        self.current_actor_id = None
        self.current_gxt_row_id = None
        self.reference_target_row_id = None
        self.draft_saved = False
        self.edit_revision = 0
        self.selected_segment = None
        self.job = None
        self.job_kind = None
        self.tts_job = None
        self.job_target_segment = None
        self.pending_draft = None
        self.cancel_event = threading.Event()
        self.messages = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cutscene-studio')
        self.drag_handle = None
        self.draft_id = uuid.uuid4().hex
        self.models = []
        self.win = tk.Toplevel(app.root)
        self.win.title('Estúdio de edição de CUTSCENE')
        self.win.geometry('1180x780')
        self.win.minsize(900, 620)
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self._load_tracks()
        self.win.after(100, self._poll)

    def _build(self):
        outer = ttk.Frame(self.win, padding=10)
        outer.pack(fill='both', expand=True)
        # Pack the status area first at the bottom so a short screen never
        # hides cancellation/progress behind the expanding editor panes.
        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x', pady=(6, 0))
        self.status = tk.StringVar(value='')
        ttk.Label(footer, textvariable=self.status, wraplength=1250).pack(anchor='w', pady=(0, 2))
        progress_row = ttk.Frame(footer)
        progress_row.pack(fill='x')
        self.progress = ttk.Progressbar(progress_row, mode='indeterminate', length=300)
        self.progress.pack(side='left', fill='x', expand=True)
        ttk.Button(progress_row, text='Cancelar operação', command=self._cancel_job).pack(side='right')
        self.log_frame = ttk.LabelFrame(footer, text='Progresso e detalhes', padding=5)
        self.log_visible = False
        ttk.Button(progress_row, text='Detalhes ▸', command=self._toggle_log).pack(side='right', padx=6)
        self.log = tk.Text(self.log_frame, height=3, wrap='word', state='disabled')
        self.log.pack(fill='x', pady=(4, 0))
        ttk.Label(outer, text='CUTSCENE · edição de trechos com horário fixo',
                  font=('Segoe UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='Os horários de legenda são apenas uma referência. Escolha os limites na forma de onda; o original do jogo permanece intacto.',
                  wraplength=1250).pack(anchor='w', pady=(2, 8))
        ttk.Label(outer, text='O trecho pode conter música e efeitos; a substituição troca todo o som selecionado.',
                  wraplength=1250).pack(anchor='w', pady=(0, 6))

        trackbar = ttk.Frame(outer)
        trackbar.pack(fill='x', pady=4)
        ttk.Label(trackbar, text='Faixa original:').pack(side='left')
        # track_var always holds the raw track name; track_display is what the
        # searchable combobox shows (track + scene + caption count).
        self.track_var = tk.StringVar()
        self.track_display = tk.StringVar()
        self.track_labels = {}
        self.track_var.trace_add('write', lambda *_a: self.track_display.set(
            self.track_labels.get(self.track_var.get(), self.track_var.get())))
        self.track_box = ttk.Combobox(trackbar, textvariable=self.track_display, width=44)
        self.track_box.pack(side='left', padx=6)
        self.track_box.bind('<<ComboboxSelected>>', self._track_picked)
        self.track_box.bind('<Return>', self._track_picked)
        self.track_box.bind('<KeyRelease>', self._filter_tracks)
        ttk.Button(trackbar, text='◀', width=3, command=lambda: self._step_track(-1)).pack(side='left')
        ttk.Button(trackbar, text='▶', width=3, command=lambda: self._step_track(1)).pack(side='left', padx=(2, 6))
        ttk.Button(trackbar, text='Ouvir faixa original', command=self._preview_original).pack(side='left', padx=6)
        self.source_status = tk.StringVar(value='Verificando índice nativo e cache original…')
        ttk.Label(trackbar, textvariable=self.source_status).pack(side='left', padx=10)

        self.wave = tk.Canvas(outer, height=145, background='#111827', highlightthickness=1,
                              highlightbackground='#334155')
        self.wave.pack(fill='x', pady=(5, 3))
        self.wave.bind('<Configure>', lambda _e: self._draw_waveform())
        self.wave.bind('<Button-1>', self._drag_start)
        self.wave.bind('<B1-Motion>', self._drag_move)
        self.wave.bind('<ButtonRelease-1>', self._drag_end)
        self.ruler = tk.Canvas(outer, height=28, background='#f1f5f9', highlightthickness=0)
        self.ruler.pack(fill='x')
        self.ruler.bind('<Configure>', lambda _e: self._draw_waveform())

        rangebar = ttk.Frame(outer)
        rangebar.pack(fill='x', pady=(5, 8))
        ttk.Label(rangebar, text='Início (s):').pack(side='left')
        self.start_var = tk.StringVar(value='0.000')
        self.start_entry = ttk.Entry(rangebar, textvariable=self.start_var, width=12)
        self.start_entry.pack(side='left', padx=4)
        ttk.Label(rangebar, text='Fim (s):').pack(side='left', padx=(10, 0))
        self.end_var = tk.StringVar(value='1.000')
        self.end_entry = ttk.Entry(rangebar, textvariable=self.end_var, width=12)
        self.end_entry.pack(side='left', padx=4)
        ttk.Button(rangebar, text='Atualizar seleção', command=self._draw_waveform).pack(side='left', padx=4)
        ttk.Button(rangebar, text='Adicionar trecho', command=self._add_segment).pack(side='left', padx=8)
        ttk.Button(rangebar, text='Atualizar trecho selecionado', command=self._update_segment).pack(side='left', padx=4)
        ttk.Button(rangebar, text='Exportar referência WAV', command=self._export_reference).pack(side='left')
        ttk.Button(rangebar, text='Ouvir seleção', command=self._listen_range).pack(side='left', padx=4)

        content = ttk.Panedwindow(outer, orient='horizontal')
        content.pack(fill='both', expand=True)
        left = ttk.Frame(content, padding=(0, 0, 6, 0))
        right = ttk.Frame(content, padding=(6, 0, 0, 0))
        content.add(left, weight=3)
        content.add(right, weight=2)
        ttk.Label(left, text='Legendas da cena · texto original e texto atual do GXT').pack(anchor='w')
        self.gxt_tree = ttk.Treeview(left, columns=('timing', 'key', 'original', 'target'),
                                     show='headings', height=5, selectmode='browse')
        for col, label, width in [('timing', 'Tempo da legenda', 105), ('key', 'Chave GXT', 120),
                                  ('original', 'GXT original', 220),
                                  ('target', 'Hipertradução aberta', 245)]:
            self.gxt_tree.heading(col, text=label)
            self.gxt_tree.column(col, width=width, minwidth=70, stretch=(col != 'key'))
        self.gxt_tree.pack(fill='both', expand=True)
        gxt_xscroll = ttk.Scrollbar(left, orient='horizontal', command=self.gxt_tree.xview)
        gxt_xscroll.pack(fill='x')
        self.gxt_tree.configure(xscrollcommand=gxt_xscroll.set)
        self.gxt_tree.bind('<<TreeviewSelect>>', self._gxt_selected)
        self.gxt_tree.bind('<Double-1>', self._use_caption_range)
        ttk.Label(left, text='Texto de referência (transcrição revisada pelo usuário):').pack(anchor='w', pady=(8, 2))
        self.reference_text = tk.Text(left, height=2, wrap='word')
        self.reference_text.pack(fill='x')
        ttk.Label(left, text='Texto de reposição · a hipertradução aberta é a autoridade. Edite somente se necessário:').pack(anchor='w', pady=(7, 2))
        self.target_text = tk.Text(left, height=3, wrap='word')
        self.target_text.pack(fill='x')
        self.reference_text.bind('<KeyRelease>', lambda _e: self._mark_dirty())
        self.target_text.bind('<KeyRelease>', lambda _e: self._mark_dirty())

        tools = ttk.Notebook(right)
        tools.pack(fill='both', expand=True)
        segments_tab = ttk.Frame(tools, padding=6)
        voice_tab = ttk.Frame(tools, padding=6)
        tools.add(segments_tab, text='Trechos do rascunho')
        tools.add(voice_tab, text='Voz de referência e geração')
        self.segment_tree = ttk.Treeview(segments_tab, columns=('range', 'key', 'voice', 'replacement'), show='headings', height=8)
        for col, label, width in [('range', 'Início → fim', 105), ('key', 'Vínculo GXT', 110),
                                  ('voice', 'Voz escolhida', 95), ('replacement', 'WAV de reposição', 125)]:
            self.segment_tree.heading(col, text=label)
            self.segment_tree.column(col, width=width, minwidth=65)
        self.segment_tree.pack(fill='both', expand=True)
        self.segment_tree.bind('<<TreeviewSelect>>', self._segment_selected)
        buttons = ttk.Frame(segments_tab)
        buttons.pack(fill='x', pady=6)
        ttk.Button(buttons, text='Importar WAV…', command=self._import_replacement).grid(row=0, column=0, sticky='ew', padx=2, pady=2)
        ttk.Button(buttons, text='Remover trecho', command=self._remove_segment).grid(row=0, column=1, sticky='ew', padx=2, pady=2)
        ttk.Button(buttons, text='Salvar rascunho', command=self._save_draft).grid(row=1, column=0, sticky='ew', padx=2, pady=2)
        ttk.Button(buttons, text='Abrir rascunho…', command=self._open_draft).grid(row=1, column=1, sticky='ew', padx=2, pady=2)
        ttk.Button(buttons, text='Exportar e ouvir faixa completa', command=self._render_mix).grid(row=2, column=0, columnspan=2, sticky='ew', padx=2, pady=2)
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.fit_var = tk.BooleanVar(value=False)
        self.overlap_var = tk.BooleanVar(value=True)
        self.background_var = tk.StringVar(value=next(iter(audio.BACKGROUND_MODES)))
        background_box = ttk.Combobox(segments_tab, textvariable=self.background_var,
                                     values=list(audio.BACKGROUND_MODES), state='readonly', width=46)
        background_box.pack(anchor='w')
        background_box.bind('<<ComboboxSelected>>', lambda _event: self._mark_dirty())
        ttk.Checkbutton(segments_tab, text='Permitir falas simultâneas (misturar os WAVs)',
                        variable=self.overlap_var, command=self._mark_dirty).pack(anchor='w')
        ttk.Checkbutton(segments_tab, text='Ajustar WAV que excede o intervalo',
                        variable=self.fit_var, command=self._mark_dirty).pack(anchor='w')
        ttk.Label(segments_tab, text='O ajuste acelera o áudio para caber no intervalo selecionado.',
                  wraplength=380).pack(anchor='w')
        self.fit_info = tk.StringVar(value='')
        ttk.Label(segments_tab, textvariable=self.fit_info, wraplength=380).pack(anchor='w', pady=4)
        self.tts_frame = ttk.LabelFrame(voice_tab, text='Qwen local com referência explícita', padding=7)
        self.tts_frame.pack(fill='both', expand=True, pady=4)
        tts_top = ttk.Frame(self.tts_frame)
        tts_top.pack(fill='x')
        ttk.Label(tts_top, text='Modelo:').grid(row=0, column=0, sticky='w')
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(tts_top, textvariable=self.model_var, state='readonly', width=12)
        self.model_box.grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Label(tts_top, text='Voz original:').grid(row=1, column=0, sticky='w')
        self.reference_actor_var = tk.StringVar()
        self.reference_actor_box = ttk.Combobox(tts_top, textvariable=self.reference_actor_var,
                                                 state='readonly', width=25)
        self.reference_actor_box.grid(row=1, column=1, sticky='ew', padx=4)
        ttk.Button(tts_top, text='Usar referência', command=self._use_bank_reference).grid(row=2, column=0, sticky='ew', pady=4)
        ttk.Button(tts_top, text='Usar seleção', command=self._export_reference).grid(row=2, column=1, sticky='ew', pady=4)
        ttk.Button(tts_top, text='Gerar este trecho com Qwen', command=self._generate_tts).grid(row=3, column=0, columnspan=2, sticky='ew', pady=4)
        from studio.tts.timing import initial_timing_settings
        initial_speed, initial_duration = initial_timing_settings(config.CFG)
        self.omni_speed_var = tk.StringVar(value=str(initial_speed))
        self.omni_duration_var = tk.StringVar(value=str(initial_duration))
        ttk.Label(tts_top, text='Omni speed / duration nativa:').grid(row=5, column=0, sticky='w', pady=(8, 2))
        omni_params = ttk.Frame(tts_top)
        omni_params.grid(row=5, column=1, sticky='ew', pady=(8, 2))
        ttk.Entry(omni_params, textvariable=self.omni_speed_var, width=6).pack(side='left')
        ttk.Entry(omni_params, textvariable=self.omni_duration_var, width=8).pack(side='left', padx=4)
        ttk.Label(tts_top, text='Duração: original, livre ou segundos; original segue a janela selecionada.',
                  wraplength=380).grid(row=7, column=0, columnspan=2, sticky='w')
        ttk.Button(tts_top, text='Gerar este trecho com OmniVoice (fala original selecionada)',
                   command=self._generate_omnivoice).grid(row=6, column=0, columnspan=2, sticky='ew', pady=4)
        ttk.Button(tts_top, text='Ouvir referência aplicada', command=self._preview_voice_reference).grid(row=4, column=0, columnspan=2, sticky='ew')
        tts_top.columnconfigure(1, weight=1)
        self.tts_status = tk.StringVar(value='A transcrição da voz de referência deve ser revisada pelo usuário.')
        ttk.Label(self.tts_frame, textvariable=self.tts_status, wraplength=380).pack(anchor='w', pady=(4, 0))
        ttk.Label(self.tts_frame, text='Confira por escuta; a referência fica presa à legenda selecionada.',
                  wraplength=380).pack(anchor='w', pady=(3, 0))

        self._load_models()
        self._load_reference_bank()

    def _load_reference_bank(self):
        try:
            from studio.audio.reference_bank import load_reference_bank
            self.reference_bank_actors = load_reference_bank(self.project)
        except (ImportError, OSError, ValueError):
            self.reference_bank_actors = []
        names = [row['actor_id'] for row in self.reference_bank_actors]
        self.reference_actor_box.configure(values=names)
        self.reference_actor_var.set('')

    def _selected_segment_matches_gxt_row(self, row_id, row):
        if self.selected_segment is None or self.selected_segment >= len(self.segments):
            return False
        if not row_id:
            return True
        segment = self.segments[self.selected_segment]
        owned_row_id = segment.get('gxt_row_id')
        if owned_row_id:
            return owned_row_id == row_id
        gxt = segment.get('gxt') or {}
        return bool(row and gxt.get('stable_id') and gxt['stable_id'] == row.get('gid'))

    def _use_bank_reference(self):
        if self.job or self.tts_job:
            self.tts_status.set('Aguarde ou cancele a operação atual antes de trocar a referência.')
            return
        actor_id = self.reference_actor_var.get().strip()
        try:
            from studio.audio.reference_bank import reference_for_actor
            reference = reference_for_actor(self.project, actor_id)
        except (OSError, ValueError) as exc:
            self.tts_status.set(str(exc))
            return
        transcript = reference['reference_transcript']
        self.current_reference = reference
        self.current_reference_transcript = transcript
        self.current_actor_id = actor_id
        selected = self.gxt_tree.selection()
        row_id = selected[0] if selected else None
        row = self.gxt_rows.get(row_id) if row_id else None
        target_segment = (self.selected_segment
                          if self._selected_segment_matches_gxt_row(row_id, row) else None)
        if target_segment is not None:
            segment = self.segments[target_segment]
            owned_row = segment.get('gxt_row_id')
            row_id = owned_row or row_id
        self.current_reference_row_id = row_id
        self.reference_pending = True
        self.reference_text.delete('1.0', 'end')
        self.reference_text.insert('1.0', transcript)
        if target_segment is not None:
            segment = self.segments[target_segment]
            segment['reference'] = reference
            segment['reference_transcript'] = transcript
            segment['actor_id'] = actor_id
            segment['gxt_row_id'] = row_id or segment.get('gxt_row_id')
            self.reference_pending = False
        self._mark_dirty()
        self._render_segments()
        self.tts_status.set('Referência original revisada aplicada explicitamente a %s.' % actor_id)

    def _preview_voice_reference(self):
        reference = None
        if self.selected_segment is not None and self.selected_segment < len(self.segments):
            reference = self.segments[self.selected_segment].get('reference')
        reference = reference or self.current_reference
        if not reference or not Path(reference.get('path', '')).is_file():
            self.tts_status.set('Selecione ou aplique uma referência antes de ouvir.')
            return
        label = 'Referência original — %s' % (reference.get('actor_id') or 'seleção manual')
        self.app._tocar(Path(reference['path']), label)

    def _load_tracks(self):
        def load_catalog(cancel_event=None, on_progress=None):
            report = load_current(self.project, self.game)
            if not report:
                raise ValueError('Índice nativo atual indisponível. Atualize o índice pela janela Fontes do jogo.')
            return report, audio.list_tracks(self.project, report, cancel_event, on_progress)
        self._submit('tracks', load_catalog)

    def _load_models(self):
        try:
            from studio.tts.cutscene import local_models
            self.models = local_models(self.project)
        except (ImportError, OSError, ValueError):
            self.models = []
        names = [str(row.get('label')) for row in self.models]
        self.model_box.configure(values=names)
        if names:
            self.model_var.set(names[0])
        else:
            self.tts_status.set('O adaptador Qwen local não está disponível neste ambiente.')

    def _submit(self, kind, function, *args, **kwargs):
        if self.job or self.tts_job:
            self.status.set('Aguarde a operação atual ou cancele-a antes de iniciar outra.')
            return False
        self.job_target_segment = kwargs.pop('_target_segment', None)
        self.job_kind = kind
        self.cancel_event = threading.Event()
        self.progress.start(10)
        self.status.set('Trabalhando em segundo plano…')
        self._log('Iniciando: ' + kind)
        def on_progress(*event):
            self.messages.put(('progress', event))
        if 'cancel_event' in function.__code__.co_varnames:
            kwargs.setdefault('cancel_event', self.cancel_event)
        if 'on_progress' in function.__code__.co_varnames:
            kwargs.setdefault('on_progress', on_progress)
        self.job = self.executor.submit(function, *args, **kwargs)
        return True

    def _poll(self):
        if self.closed:
            return
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                self._log(' · '.join(str(part) for part in payload if part not in (0, 1, None)))
            elif kind == 'log':
                self._log(payload)
        if self.tts_job and self.tts_job.done:
            result = self.tts_job.wait(0)
            self.tts_job = None
            if result.returncode or result.cancelled or result.timed_out or result.error:
                failed_kind = self.job_kind
                self.progress.stop()
                self.job_kind = None
                if result.cancelled:
                    message = 'Geração cancelada.'
                elif result.timed_out:
                    message = 'Geração OmniVoice excedeu o tempo limite.' if failed_kind == 'omnivoice' else 'Geração Qwen excedeu o tempo limite.'
                else:
                    from studio.ui.app import _generation_failure_detail
                    engine_id = 'omnivoice' if failed_kind == 'omnivoice' else 'qwen'
                    message = ('OmniVoice' if failed_kind == 'omnivoice' else 'Qwen') + ' falhou: ' + _generation_failure_detail(result, engine_id)
                self.tts_status.set(message)
                self.status.set(message)
            elif self.job_kind == 'omnivoice' and not self._tts_output.is_file():
                quarantine = self._tts_output.parent / 'quarantine' / self._tts_output.name
                receipt = quarantine.with_suffix('.omnivoice.json')
                detail = 'Take OmniVoice foi preservado em quarentena: ' + str(quarantine)
                if receipt.is_file():
                    try:
                        import json
                        info = json.loads(receipt.read_text(encoding='utf-8'))
                        if info.get('status') == 'QUARANTINED_DURATION':
                            detail = ('A fala excedeu a janela (%.2f s produzidos; %.2f s disponíveis). '
                                      'WAV preservado em quarentena: %s. Ajuste speed/duration nativos; '
                                      'nenhum trecho foi cortado ou alongado.' %
                                      (float(info.get('duration_seconds') or 0),
                                       float(info.get('window_seconds') or 0), quarantine))
                        else:
                            detail += ' ' + str(info.get('status', ''))
                    except (OSError, ValueError):
                        pass
                self.progress.stop()
                self.job_kind = None
                self.tts_status.set(detail)
                self.status.set(detail)
            elif self.job_kind in ('tts', 'omnivoice'):
                generation_kind = self.job_kind
                self.job_kind = 'tts_import'
                target = self._segment_index(self.job_target_segment)
                if target is None:
                    self.progress.stop()
                    self.job_kind = None
                    self.status.set('O trecho associado à geração não existe mais; WAV salvo em ' + str(self._tts_output))
                else:
                    reference_path = self.segments[target]['reference']['path']
                    expected_reference_hash = self._tts_reference_sha256
                    output_path = self._tts_output
                    self._pending_engine = 'OmniVoice' if generation_kind == 'omnivoice' else 'Qwen'
                    def verify_and_import():
                        if audio.digest(reference_path) != expected_reference_hash:
                            raise ValueError('WAV de referência mudou durante a geração; take não associado.')
                        item = audio.import_replacement(output_path)
                        item['engine'] = self._pending_engine
                        return item
                    self.job = self.executor.submit(verify_and_import)
        if self.job and self.job.done():
            future, kind = self.job, self.job_kind
            self.job = self.job_kind = None
            self.progress.stop()
            try:
                result = future.result()
            except Exception as exc:
                self._log('ERRO: ' + str(exc))
                self.status.set(str(exc))
                if kind in ('tts', 'tts_import'):
                    self.tts_status.set('Geração interrompida: %s' % exc)
            else:
                self._job_done(kind, result)
        self.win.after(100, self._poll)

    def _job_done(self, kind, result):
        if kind == 'tracks':
            self.report, tracks = result
            self.track_rows = {row['track']: row for row in tracks}
            names = sorted(self.track_rows)
            self.track_labels = self._build_track_labels(names)
            self.track_box.configure(values=[self.track_labels[n] for n in names])
            self.track_var.set(names[0] if names else '')
            self.source_status.set('%d faixas originais verificadas no cache' % len(names))
            if names:
                self._select_track()
            return
        if kind == 'load':
            self.track_data = result
            self.status.set('%s carregada completa · %.2f s · %d Hz · %d canais' %
                            (self.track['track'], result['duration'], result['sample_rate'], result['channels']))
            self.start_var.set('0.000')
            self.end_var.set('%.3f' % min(1.0, result['duration']))
            self._draw_waveform()
            self._render_gxt_rows()
            if self.pending_draft:
                self._restore_draft(self.pending_draft)
                self.pending_draft = None
            return
        if kind == 'reference':
            transcript = getattr(
                self, '_reference_transcript_job', self.reference_text.get('1.0', 'end-1c').strip())
            target_row_id = getattr(self, 'reference_target_row_id', None)
            target = self._segment_index(self.job_target_segment)
            if self.job_target_segment and target is None:
                self.status.set('A referência exportada não foi associada: o trecho alvo foi removido.')
                self.tts_status.set('Referência salva, mas o trecho alvo não existe mais; aplique-a novamente se necessário.')
                return
            if target is not None:
                self.segments[target]['reference'] = result
                self.segments[target]['reference_transcript'] = transcript
                self.segments[target]['actor_id'] = result.get('actor_id')
            selected_rows = self.gxt_tree.selection() if hasattr(self, 'gxt_tree') else ()
            active_row_id = selected_rows[0] if selected_rows else None
            context_current = (active_row_id == target_row_id and
                               (target is None or self.selected_segment == target))
            if context_current:
                self.current_reference = result
                self.current_reference_transcript = transcript
                self.current_reference_row_id = target_row_id
                self.current_actor_id = result.get('actor_id')
                self.reference_pending = target is None
                if hasattr(self, 'reference_actor_var'):
                    self.reference_actor_var.set(self.current_actor_id or '')
            self._mark_dirty()
            self.status.set('Referência exportada: ' + result['path'])
            self.tts_status.set('Referência exportada: %s (%.2f s).' % (Path(result['path']).name, result['duration']))
            self._render_segments()
            return
        if kind == 'range_preview':
            self.app._tocar(result['path'], 'Seleção CUTSCENE')
            self.status.set('Reproduzindo seleção exportada para prévia.')
            return
        if kind == 'preview':
            self.app._tocar(result['path'], 'CUTSCENE original completa')
            self.status.set('Reproduzindo faixa original completa em WAV de prévia.')
            return
        if kind == 'render':
            self.app._tocar(result['path'], 'CUTSCENE com trechos substituídos')
            self.status.set('Prévia exportada (ganho %.3f; %d falas sem take PT-BR) e em reprodução: %s' %
                            (result.get('mix_gain', 1.0), len(result.get('missing_ptbr', [])), result['path']))
            return
        if kind == 'replacement':
            target = self._segment_index(self.job_target_segment)
            if target is None:
                return
            self.segments[target]['replacement'] = result
            self._mark_dirty()
            self._render_segments()
            self.status.set('WAV de reposição importado e assinado: ' + Path(result['path']).name)
            return
        if kind == 'draft':
            self.status.set('Rascunho salvo: ' + str(result))
            self._log('Rascunho DRAFT salvo. Nenhum arquivo do jogo foi alterado.')
            self.draft_saved = self.edit_revision == self._save_revision
            return
        if kind == 'tts_import':
            target = self._segment_index(self.job_target_segment)
            if target is None:
                self.status.set('WAV de Qwen pronto; selecione o trecho desejado e importe o take salvo.')
                self.tts_status.set('WAV gerado: %s' % Path(result['path']).name)
                return
            self.segments[target]['replacement'] = result
            self._mark_dirty()
            self._render_segments()
            self.tts_status.set('WAV gerado localmente com referência explícita: %s' % Path(result['path']).name)
            self.status.set('WAV de reposição gerado e associado ao trecho selecionado.')
            available = self.segments[target]['end'] - self.segments[target]['start']
            if result['duration'] < available:
                self.fit_info.set('O WAV é %.2f s menor que o intervalo; o restante será silêncio.' % (available - result['duration']))
            elif result['duration'] > available:
                self.fit_info.set('Ative “Ajustar WAV que excede o intervalo” para exportar esta reposição.')
            if self.segments[target].get('replacement', {}).get('engine') == 'OmniVoice':
                self.fit_var.set(False)
                self.fit_info.set('OmniVoice não ajusta duração; revergere o take com speed/duration nativos se precisar.')
            return
        if kind == 'draft_open':
            source_track = result.get('source', {}).get('track')
            if source_track not in self.track_rows:
                self.status.set('A faixa deste rascunho não consta no cache original atual.')
                return
            self.pending_draft = result
            self.draft_id = result['draft_id']
            self.track_var.set(source_track)
            self._select_track()
            return

    def _build_track_labels(self, names):
        scenes = {}
        counts = {}
        for record in (self.report or {}).get('records', []):
            track = (record.get('stream') or {}).get('track')
            if record.get('pak') != 'CUTSCENE' or not track:
                continue
            if record.get('scene'):
                scenes.setdefault(track, set()).add(str(record['scene']))
            if record.get('gxt_stable_id'):
                counts[track] = counts.get(track, 0) + 1
        labels = {}
        for name in names:
            parts = [name]
            if scenes.get(name):
                parts.append('/'.join(sorted(scenes[name])))
            parts.append('%d legendas' % counts[name] if counts.get(name) else 'sem legendas')
            labels[name] = ' · '.join(parts)
        return labels

    def _filter_tracks(self, event=None):
        if event is not None and event.keysym in ('Return', 'Up', 'Down', 'Escape', 'Tab'):
            return
        text = self.track_display.get().strip().lower()
        names = sorted(self.track_rows)
        matches = [self.track_labels.get(n, n) for n in names
                   if text in self.track_labels.get(n, n).lower()]
        self.track_box.configure(values=matches or [self.track_labels.get(n, n) for n in names])

    def _track_picked(self, _event=None):
        text = self.track_display.get().strip().lower()
        names = sorted(self.track_rows)
        exact = [n for n in names if self.track_labels.get(n, n).lower() == text or n.lower() == text]
        found = exact or [n for n in names if text and text in self.track_labels.get(n, n).lower()]
        if not found:
            self.status.set('Nenhuma faixa corresponde a "%s".' % self.track_display.get())
            self.track_var.set(self.track['track'] if self.track else '')
            return
        self.track_var.set(found[0])
        self.track_box.configure(values=[self.track_labels.get(n, n) for n in names])
        self.track_box.selection_clear()
        self._select_track()

    def _step_track(self, delta):
        names = sorted(self.track_rows)
        if not names:
            return
        current = self.track['track'] if self.track else self.track_var.get()
        index = names.index(current) if current in names else -1
        self.track_var.set(names[(index + delta) % len(names)])
        self._select_track()

    def _select_track(self):
        if self.job or self.tts_job:
            if self.track:
                self.track_var.set(self.track['track'])
            self.status.set('Aguarde ou cancele a operação atual antes de mudar de faixa.')
            return
        row = self.track_rows.get(self.track_var.get())
        if not row:
            return
        if self.track and (self.segments or self.current_reference) and not self.draft_saved:
            self.track_var.set(self.track['track'])
            self.status.set('Salve o rascunho antes de trocar de faixa; há alterações sem salvar.')
            return
        self.track = row
        self.track_data = None
        self.segments = []
        self.current_reference = None
        self.current_reference_transcript = ''
        self.current_reference_row_id = None
        self.reference_pending = False
        self.current_actor_id = None
        self.current_gxt_row_id = None
        if hasattr(self, 'reference_actor_var'):
            self.reference_actor_var.set('')
        self.draft_saved = False
        self.selected_segment = None
        self._render_segments()
        self.gxt_rows = self._make_gxt_rows(row['track'])
        self._submit('load', audio.load_track, row)

    def _make_gxt_rows(self, track_name):
        if not self.report:
            return {}
        records = self.report.get('records', [])
        native_source = self.report.get('sources', {}).get('gxt') or {}
        native_path = Path(native_source.get('path') or '')
        if native_path.is_file() and audio.digest(native_path) == native_source.get('sha256'):
            try:
                self.native_gxt = Gxt(native_path)
            except (OSError, ValueError):
                self.native_gxt = None
        else:
            self.native_gxt = None
        linked = {}
        for record in records:
            if record.get('pak') != 'CUTSCENE' or (record.get('stream') or {}).get('track') != track_name:
                continue
            gid = record.get('gxt_stable_id')
            if not gid:
                continue
            caption = self.report.get('classifications', [])
            classification = next((item for item in caption if item.get('gxt_stable_id') == gid), {})
            table, crc = gid.split(':', 1)
            hash_value = int(crc, 16)
            original = classification.get('source_text', '')
            game_original = self._gxt_text(getattr(self.app, 'c', None), table, hash_value)
            native_text = ''
            if self.native_gxt and table in self.native_gxt.t and hash_value in self.native_gxt.t[table]['hashes']:
                native_text = decode_text_c(self.native_gxt.t[table]['orig'][hash_value])
            target = self._gxt_text(getattr(self.app, 'g', None), table, hash_value)
            row_id = '%s|%s|%d' % (gid, record.get('scene') or '', record.get('caption_start_ms', 0))
            linked[row_id] = {'gid': gid, 'table': table, 'hash': crc.upper(),
                           'scene': record.get('scene'), 'key': record.get('event_name') or crc,
                           'start': record.get('caption_start_ms', 0) / 1000,
                           'duration': record.get('caption_duration_ms', 0) / 1000,
                           'original': original,
                           'comparison': game_original,
                           'native_source_text': native_text,
                           'native_gxt_path': str(native_path) if native_text else '',
                           'native_gxt_sha256': native_source.get('sha256') if native_text else '',
                           'hypertranslation': target,
                           'gxt_path': str(getattr(getattr(self.app, 'g', None), 'path', '') or ''),
                           'original_gxt_path': str(getattr(getattr(self.app, 'c', None), 'path', '') or '')}
            linked[row_id]['report_record'] = record
        return linked

    @staticmethod
    def _gxt_text(gxt, table, hash_value):
        try:
            from studio.gxt.codec import decode_text_c
            return decode_text_c(gxt.t[table]['cur'][hash_value])
        except (AttributeError, KeyError, TypeError):
            return ''

    def _render_gxt_rows(self):
        self.gxt_tree.delete(*self.gxt_tree.get_children())
        for gid, row in self.gxt_rows.items():
            self.gxt_tree.insert('', 'end', iid=gid,
                values=('%.2f + %.2fs' % (row['start'], row['duration']), row['key'],
                        row['original'], row['hypertranslation']))

    def _gxt_selected(self, _event=None):
        selected = self.gxt_tree.selection()
        row_id = selected[0] if selected else None
        row = self.gxt_rows.get(row_id) if row_id else None
        if not row:
            return
        previous_row_id = getattr(self, 'current_gxt_row_id', None)
        if (self.selected_segment is not None and
                not self._selected_segment_matches_gxt_row(row_id, row)):
            self.selected_segment = None
            if hasattr(self, 'segment_tree'):
                selected_segments = self.segment_tree.selection()
                if selected_segments:
                    self.segment_tree.selection_remove(*selected_segments)
        if self.current_reference and getattr(self, 'current_reference_row_id', None) != row_id:
            self.current_reference = None
            self.current_reference_transcript = ''
            self.current_reference_row_id = None
            self.reference_pending = False
            self.current_actor_id = None
        self.current_gxt_row_id = row_id
        if hasattr(self, 'reference_actor_var'):
            actor_id = ''
            if self._selected_segment_matches_gxt_row(row_id, row):
                actor_id = self.segments[self.selected_segment].get('actor_id') or ''
            elif self.current_reference and self.current_reference_row_id == row_id:
                actor_id = self.current_actor_id or ''
            elif previous_row_id == row_id:
                actor_id = self.reference_actor_var.get()
            self.reference_actor_var.set(actor_id)
        if not self.current_reference:
            self.reference_text.delete('1.0', 'end')
            self.reference_text.insert('1.0', row.get('comparison', row.get('original', '')))
        self.target_text.delete('1.0', 'end')
        self.target_text.insert('1.0', row['hypertranslation'])

    def _use_caption_range(self, _event=None):
        selected = self.gxt_tree.selection()
        row = self.gxt_rows.get(selected[0]) if selected else None
        if row and self.track_data:
            start = max(0.0, row['start'])
            end = min(self.track_data['duration'], start + row['duration'])
            self.start_var.set('%.3f' % start)
            self.end_var.set('%.3f' % end)
            self._draw_waveform()

    def _current_bounds(self):
        if not self.track_data:
            raise ValueError('Carregue uma faixa completa primeiro.')
        try:
            return float(self.start_var.get().replace(',', '.')), float(self.end_var.get().replace(',', '.'))
        except ValueError:
            raise ValueError('Início e fim devem ser valores numéricos.')

    def _add_segment(self):
        if self.job or self.tts_job:
            return
        try:
            start, end = self._current_bounds()
            audio._frame_bounds(start, end, self.track_data['sample_rate'], self.track_data['frames'])
        except ValueError as exc:
            messagebox.showerror('Intervalo', str(exc), parent=self.win)
            return
        if not self._allow_overlap() and any(start < item['end'] and item['start'] < end for item in self.segments):
            messagebox.showerror('Intervalos sobrepostos', 'Este trecho cruza um intervalo que já está no rascunho.', parent=self.win)
            return
        selected = self.gxt_tree.selection()
        row_id = selected[0] if selected else None
        gxt_row = self.gxt_rows.get(row_id) if row_id else None
        text = self.target_text.get('1.0', 'end-1c').strip()
        owns_reference = bool(self.current_reference and self.reference_pending and
                              (self.current_reference_row_id is None or self.current_reference_row_id == row_id))
        record = {'id': uuid.uuid4().hex, 'start': start, 'end': end,
                  'gxt': self._gxt_snapshot(gxt_row, text), 'gxt_row_id': row_id,
                  'reference': self.current_reference if owns_reference else None,
                  'actor_id': self.current_actor_id if owns_reference else None,
                  'replacement': None,
                  'reference_transcript': (self.current_reference_transcript if owns_reference else '') or
                    self.reference_text.get('1.0', 'end-1c').strip()}
        self.segments.append(record)
        if owns_reference:
            self.reference_pending = False
        self._mark_dirty()
        self.selected_segment = len(self.segments) - 1
        self._render_segments()

    def _gxt_snapshot(self, row, hypertext):
        if not row:
            return None
        return {'stable_id': row['gid'], 'table': row['table'], 'hash': row['hash'],
                'scene': row['scene'], 'key': row['key'], 'original_text': row['original'],
                'comparison_text': row['comparison'], 'hypertranslated_text': hypertext,
                'authority': 'current editor GXT hypertranslation',
                'gxt_path': row['gxt_path'], 'original_gxt_path': row['original_gxt_path']}

    def _render_segments(self):
        self.segment_tree.delete(*self.segment_tree.get_children())
        for index, row in enumerate(self.segments):
            gxt = row.get('gxt') or {}
            replacement = row.get('replacement')
            self.segment_tree.insert('', 'end', iid=str(index), values=(
                '%.3f → %.3f' % (row['start'], row['end']), gxt.get('stable_id', 'sem vínculo'),
                row.get('actor_id') or 'manual' if row.get('reference') else '—',
                Path(replacement['path']).name if replacement else 'pendente'))
        if self.selected_segment is not None and self.selected_segment < len(self.segments):
            self.segment_tree.selection_set(str(self.selected_segment))

    def _segment_selected(self, _event=None):
        if self.job or self.tts_job:
            if self.selected_segment is not None and self.segment_tree.exists(str(self.selected_segment)):
                self.segment_tree.selection_set(str(self.selected_segment))
            return
        selected = self.segment_tree.selection()
        if selected:
            self.selected_segment = int(selected[0])
            row = self.segments[self.selected_segment]
            self.start_var.set('%.3f' % row['start'])
            self.end_var.set('%.3f' % row['end'])
            self._draw_waveform()
            if row.get('gxt'):
                gid = row['gxt']['stable_id']
                row_id = next((key for key, item in self.gxt_rows.items() if item['gid'] == gid), None)
                if row_id and self.gxt_tree.exists(row_id):
                    self.gxt_tree.selection_set(row_id)
                self.target_text.delete('1.0', 'end')
                self.target_text.insert('1.0', row['gxt'].get('hypertranslated_text', ''))
            self.current_reference = row.get('reference')
            self.current_reference_transcript = row.get('reference_transcript', '')
            self.current_reference_row_id = row.get('gxt_row_id')
            self.reference_pending = False
            self.current_actor_id = row.get('actor_id')
            if hasattr(self, 'reference_actor_var'):
                self.reference_actor_var.set(row.get('actor_id') or '')
            self.reference_text.delete('1.0', 'end')
            self.reference_text.insert('1.0', self.current_reference_transcript)

    def _remove_segment(self):
        if self.job or self.tts_job:
            return
        if self.selected_segment is None:
            return
        self.segments.pop(self.selected_segment)
        self._mark_dirty()
        self.selected_segment = None
        self._render_segments()
        self._draw_waveform()

    def _update_segment(self):
        if self.job or self.tts_job or self.selected_segment is None:
            return
        try:
            start, end = self._current_bounds()
            audio._frame_bounds(start, end, self.track_data['sample_rate'], self.track_data['frames'])
        except (ValueError, TypeError) as exc:
            messagebox.showerror('Intervalo', str(exc), parent=self.win)
            return
        for index, item in enumerate(self.segments):
            if not self._allow_overlap() and index != self.selected_segment and start < item['end'] and item['start'] < end:
                messagebox.showerror('Intervalos sobrepostos', 'O novo intervalo cruza outro trecho do rascunho.', parent=self.win)
                return
        self.segments[self.selected_segment]['start'] = start
        self.segments[self.selected_segment]['end'] = end
        self._mark_dirty()
        self._render_segments()
        self._draw_waveform()

    def _import_replacement(self):
        if self.selected_segment is None:
            messagebox.showinfo('Trecho', 'Selecione ou adicione um trecho antes de importar o WAV de reposição.', parent=self.win)
            return
        path = filedialog.askopenfilename(parent=self.win, title='WAV de reposição já gerado',
                                         filetypes=[('WAV', '*.wav')])
        if path:
            segment = dict(self.segments[self.selected_segment])
            fit = bool(self.fit_var.get())
            track_data = self.track_data
            def load_replacement(cancel_event=None, on_progress=None):
                item = audio.import_replacement(path)
                audio.validate_replacements(track_data, [item],
                    [{'start': segment['start'], 'end': segment['end']}], fit)
                return item
            self._submit('replacement', load_replacement, _target_segment=segment['id'])

    def _export_reference(self):
        if self.job or self.tts_job:
            self.status.set('Aguarde ou cancele a operação atual antes de exportar outra referência.')
            return
        if not self.track_data:
            return
        try:
            start, end = self._current_bounds()
            track = self.track
            destination = self.project / 'workspace' / 'cutscene_studio' / 'references' / (
                '%s_%s.wav' % (track['track'], uuid.uuid4().hex))
        except ValueError as exc:
            messagebox.showerror('Referência', str(exc), parent=self.win)
            return
        target = (self.segments[self.selected_segment]['id']
                  if self.selected_segment is not None else None)
        selected_gxt = self.gxt_tree.selection()
        selected_row_id = selected_gxt[0] if selected_gxt else None
        selected_row = self.gxt_rows.get(selected_row_id) if selected_row_id else None
        target_segment = (self.selected_segment
                          if self._selected_segment_matches_gxt_row(selected_row_id, selected_row) else None)
        if target_segment is not None:
            self.reference_target_row_id = (self.segments[target_segment].get('gxt_row_id') or selected_row_id)
        else:
            self.reference_target_row_id = selected_row_id
        target = self.segments[target_segment]['id'] if target_segment is not None else None
        self._submit('reference', audio.export_reference, self.track_data, start, end, destination,
                     _target_segment=target)
        if self.job_kind == 'reference':
            self._reference_transcript_job = self.reference_text.get('1.0', 'end-1c').strip()

    def _listen_range(self):
        if not self.track_data:
            return
        try:
            start, end = self._current_bounds()
            destination = self.project / 'workspace' / 'cutscene_studio' / 'previews' / (
                '%s_selection_%s.wav' % (self.track['track'], uuid.uuid4().hex))
        except ValueError as exc:
            messagebox.showerror('Prévia', str(exc), parent=self.win)
            return
        self._submit('range_preview', audio.export_reference, self.track_data, start, end, destination)

    def _preview_original(self):
        if not self.track_data:
            return
        destination = self.project / 'workspace' / 'cutscene_studio' / 'previews' / (
            '%s_original_%s.wav' % (self.track['track'], uuid.uuid4().hex))
        self._submit('preview', audio.export_reference, self.track_data, 0,
                     self.track_data['duration'], destination)

    def _generate_tts(self):
        if self.job or self.tts_job:
            self.status.set('Aguarde a operação atual antes de iniciar a geração Qwen.')
            return
        if self.selected_segment is None or not self.models:
            self.tts_status.set('Selecione um trecho e um modelo Qwen instalado.')
            return
        segment = self.segments[self.selected_segment]
        reference = segment.get('reference')
        if not reference or not Path(reference['path']).is_file():
            self.tts_status.set('Exporte uma referência de voz e associe-a a este trecho antes de gerar.')
            return
        expected_hash = reference.get('sha256')
        if not expected_hash:
            self.tts_status.set('A referência deste trecho não tem hash salvo; escolha novamente uma referência íntegra.')
            return
        try:
            if audio.digest(reference['path']) != expected_hash:
                self.tts_status.set('O WAV de referência mudou desde sua seleção; escolha novamente uma referência íntegra.')
                return
        except OSError as exc:
            self.tts_status.set('Não foi possível conferir a referência: %s' % exc)
            return
        reference_actor = reference.get('actor_id')
        segment_actor = segment.get('actor_id')
        if reference_actor and segment_actor and reference_actor != segment_actor:
            self.tts_status.set('A voz escolhida (%s) diverge do personagem salvo no trecho (%s); revise a associação.' %
                                (reference_actor, segment_actor))
            return
        selected_gxt = self.gxt_tree.selection() if hasattr(self, 'gxt_tree') else ()
        if segment.get('gxt_row_id') and selected_gxt and selected_gxt[0] != segment['gxt_row_id']:
            self.tts_status.set('A legenda selecionada difere do trecho; selecione a legenda vinculada antes de gerar.')
            return
        source_text = self.reference_text.get('1.0', 'end-1c').strip()
        target_text = self.target_text.get('1.0', 'end-1c').strip()
        if not source_text or not target_text:
            self.tts_status.set('Preencha a transcrição revisada da referência e o texto de reposição.')
            return
        segment['reference_transcript'] = source_text
        if segment.get('gxt'):
            segment['gxt']['hypertranslated_text'] = target_text
        model = next((item for item in self.models if str(item.get('label')) == self.model_var.get()), None)
        if not model:
            return
        from studio.tts.cutscene import build_command, python_path
        python = python_path(self.project)
        if not python:
            self.tts_status.set('Runtime Python do Qwen não encontrado.')
            return
        output = self.project / 'workspace' / 'cutscene_studio' / 'generated' / (uuid.uuid4().hex + '.wav')
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            command = build_command(self.project, model['label'], Path(reference['path']), source_text,
                                    target_text, output, device='auto',
                                    reference_sha256=expected_hash)
        except (OSError, ValueError) as exc:
            self.tts_status.set(str(exc))
            return
        self._tts_output = output
        self.job_target_segment = segment['id']
        self._tts_reference_sha256 = reference['sha256']
        self.job_kind = 'tts'
        self.progress.start(10)
        self.tts_status.set('Gerando em segundo plano com modelo local e referência explícita…')
        self.tts_job = self.app.jobs.submit(command, cwd=config.APP_DIR,
            on_line=lambda line: self.messages.put(('log', line)), timeout=1800)

    def _generate_omnivoice(self):
        """Clone only from the exact manually selected window in this native track."""
        if self.job or self.tts_job:
            self.tts_status.set('Aguarde a operação atual antes de iniciar OmniVoice.')
            return
        if not self.track_data or self.selected_segment is None:
            self.tts_status.set('Carregue a faixa e crie/selecione um trecho manual vinculado a uma legenda.')
            return
        segment = self.segments[self.selected_segment]
        row_id = segment.get('gxt_row_id')
        row = self.gxt_rows.get(row_id)
        if not row or not row.get('native_source_text') or not row.get('native_gxt_path'):
            self.tts_status.set('Legenda sem texto inglês GXT base ou sem arquivo GXT original carregado.')
            return
        if self.gxt_tree.selection() and self.gxt_tree.selection()[0] != row_id:
            self.tts_status.set('Selecione a legenda original vinculada a este trecho antes de gerar.')
            return
        if self.track_data.get('archive_path') != self.report.get('sources', {}).get('stream', {}).get('path') or \
                self.track_data.get('archive_sha256') != self.report.get('sources', {}).get('stream', {}).get('sha256'):
            self.tts_status.set('A faixa não corresponde ao arquivo CUTSCENE original indexado.')
            return
        source_path = Path(self.track_data.get('source_path', '')).resolve()
        try:
            source_path.relative_to((self.project / '.cache' / 'cutscene_original').resolve())
        except ValueError:
            self.tts_status.set('A origem não é uma faixa CUTSCENE do cache nativo atual.')
            return
        target_text = self.target_text.get('1.0', 'end-1c').strip()
        reference_text = str(row['native_source_text']).strip()
        if not target_text or not reference_text:
            self.tts_status.set('Texto PT-BR e transcrição da legenda inglesa base são obrigatórios.')
            return
        try:
            start, end = float(segment['start']), float(segment['end'])
            selected_start, selected_end = self._current_bounds()
            tolerance = 0.5 / int(self.track_data['sample_rate'])
            if abs(selected_start-start) > tolerance or abs(selected_end-end) > tolerance:
                raise ValueError('Ajuste manualmente a waveform e clique “Atualizar trecho selecionado” para confirmar os limites da referência.')
            first, last = audio._frame_bounds(start, end, self.track_data['sample_rate'],
                                               self.track_data['frames'])
            reference_path = (self.project / 'workspace' / 'cutscene_studio' / 'references' /
                              ('omnivoice_' + uuid.uuid4().hex + '.wav'))
            ref = audio.export_reference(self.track_data, start, end, reference_path)
            from studio.tts.omnivoice import build_command, sha256, spoken_text
            gxt_path = Path(row['native_gxt_path']).resolve()
            if not gxt_path.is_file() or audio.digest(gxt_path) != row.get('native_gxt_sha256'):
                raise ValueError('GXT original inglês mudou desde a indexação nativa.')
            spoken_reference = spoken_text(reference_text)
            if not spoken_reference:
                raise ValueError('A legenda GXT base contém apenas tags, sem transcrição falada.')
            try:
                speed = float(self.omni_speed_var.get().replace(',', '.'))
                duration_value = self.omni_duration_var.get().strip()
            except ValueError:
                raise ValueError('Velocidade precisa ser numérica; duração aceita original, livre ou segundos.')
            output = (self.project / 'workspace' / 'cutscene_studio' / 'generated' /
                      ('omnivoice_' + uuid.uuid4().hex + '.wav'))
            provenance = {
                'source_kind': 'verified_native_cutscene_window',
                'source_path': str(source_path), 'source_sha256': self.track_data['source_sha256'],
                'archive_path': self.track_data.get('archive_path'),
                'archive_sha256': self.track_data.get('archive_sha256'),
                'reference_start_frame': first, 'reference_end_frame': last,
                'source_sample_rate': int(self.track_data['sample_rate']),
                'gxt_stable_id': row['gid'], 'gxt_source_path': str(gxt_path),
                'gxt_source_sha256': row['native_gxt_sha256'], 'timing_source': 'user_selected_waveform_segment',
                'base_english_gxt_path': str(gxt_path),
                'base_english_gxt_sha256': row['native_gxt_sha256'],
                'base_english_text': reference_text, 'base_english_gxt_id': row['gid'],
                'reference_transcript_authority': 'NATIVE_GXT_CAPTION_NOT_BLIND_ASR',
                'reference_asr_verification': 'PENDING',
                'caption_window_is_alignment_proof': False,
                'segment_seconds': [start, end], 'track': self.track_data.get('track'),
                'production_approved': False,
            }
            command = build_command(self.project, reference_path, spoken_reference, target_text, output,
                                    provenance=provenance, speed=speed, duration=duration_value,
                                    window_seconds=end-start)
        except (OSError, ValueError, TypeError) as exc:
            self.tts_status.set(str(exc))
            return
        segment['reference'] = {'path': str(reference_path), 'sha256': ref['sha256'],
                                'actor_id': 'original_segment_window', 'source_kind': provenance['source_kind']}
        segment['reference_transcript'] = spoken_reference
        self._pending_engine = 'OmniVoice'
        self._tts_output = output
        self.job_target_segment = segment['id']
        self._tts_reference_sha256 = ref['sha256']
        self.job_kind = 'omnivoice'
        self.progress.start(10)
        caveat = ' Referência curta (<3 s); resultado pode variar.' if ref['duration'] < 3 else ''
        self.tts_status.set('OmniVoice | trecho original=%s (identidade não verificada) | GXT=%s | janela manual %.3f–%.3f s | EN: %s%s' %
                            (source_path, row['gid'], start, end, spoken_reference, caveat))
        self.tts_job = self.app.jobs.submit(command, cwd=config.APP_DIR,
            on_line=lambda line: self.messages.put(('log', line)), timeout=1800)

    def _save_draft(self):
        if not self.track_data:
            messagebox.showinfo('Rascunho', 'Carregue uma faixa antes de salvar o rascunho.', parent=self.win)
            return
        if self.job or self.tts_job:
            self.status.set('Aguarde a operação atual antes de salvar o rascunho.')
            return
        if self.selected_segment is not None:
            selected = self.segments[self.selected_segment]
            selected['reference_transcript'] = self.reference_text.get('1.0', 'end-1c').strip()
            if selected.get('gxt'):
                selected['gxt']['hypertranslated_text'] = self.target_text.get('1.0', 'end-1c').strip()
        segments = [{key: segment.get(key) for key in ('start', 'end', 'gxt', 'gxt_row_id', 'actor_id',
                                                        'reference', 'replacement', 'reference_transcript')}
                    for segment in self.segments]
        gxt_rows = []
        for segment in segments:
            if segment.get('gxt'):
                gxt_rows.append(segment['gxt'])
        self._save_revision = self.edit_revision
        draft_fit = bool(self.fit_var.get()) and not any(
            (segment.get('replacement') or {}).get('engine') == 'OmniVoice' for segment in segments)
        if self.fit_var.get() and not draft_fit:
            self.fit_info.set('Ajuste pós-geração desativado porque o rascunho contém take OmniVoice.')
        self._submit('draft', audio.save_draft, self.project, self.track_data, segments, gxt_rows,
                     draft_id=self.draft_id, fit=draft_fit, allow_overlap=self._allow_overlap(),
                     background_mode=self._background_mode(),
                     reference=self.current_reference,
                     reference_transcript=self.reference_text.get('1.0', 'end-1c').strip(),
                     reference_row_id=self.current_reference_row_id)

    def _open_draft(self):
        if (self.segments or self.current_reference) and not self.draft_saved:
            self.status.set('Salve o rascunho atual antes de abrir outro.')
            return
        if not self.report:
            messagebox.showerror('Rascunho', 'Índice nativo atual indisponível.', parent=self.win)
            return
        path = filedialog.askopenfilename(parent=self.win, title='Abrir rascunho CUTSCENE',
            initialdir=self.project / 'workspace' / 'cutscene_studio' / 'drafts',
            filetypes=[('Rascunho CUTSCENE', '*.json')])
        if path:
            self._submit('draft_open', audio.load_draft, path, self.project, self.report)

    def _restore_draft(self, draft):
        self.draft_id = draft['draft_id']
        self.fit_var.set(bool(draft.get('fit_oversized')))
        self.overlap_var.set(bool(draft.get('allow_overlap')))
        self.background_var.set(next(label for label, mode in audio.BACKGROUND_MODES.items()
                                     if mode == draft.get('background_mode', 'preserve_original')))
        self.edit_revision += 1
        self.draft_saved = True
        self.segments = []
        for item in draft.get('segments', []):
            self.segments.append({'id': uuid.uuid4().hex,
                'start': item['start_seconds'], 'end': item['end_seconds'],
                'gxt': item.get('gxt'), 'gxt_row_id': item.get('gxt_row_id'),
                'actor_id': item.get('actor_id'), 'reference': item.get('reference'),
                'replacement': item.get('replacement'),
                'reference_transcript': item.get('reference_transcript', '')})
        self.selected_segment = 0 if self.segments else None
        authoring = draft.get('reference_authoring') or {}
        self.current_reference = authoring.get('audio')
        self.current_reference_transcript = authoring.get('transcript', '')
        self.current_reference_row_id = authoring.get('gxt_row_id')
        self.reference_pending = bool(self.current_reference)
        self.current_actor_id = (self.current_reference or {}).get('actor_id')
        if hasattr(self, 'reference_actor_var'):
            self.reference_actor_var.set(self.current_actor_id or '')
        self.reference_text.delete('1.0', 'end')
        self.reference_text.insert('1.0', self.current_reference_transcript)
        self.target_text.delete('1.0', 'end')
        if self.selected_segment is not None:
            selected = self.segments[0]
            self.start_var.set('%.3f' % selected['start'])
            self.end_var.set('%.3f' % selected['end'])
            self.current_reference = selected.get('reference')
            self.current_reference_transcript = selected.get('reference_transcript', '')
            self.current_reference_row_id = selected.get('gxt_row_id')
            self.current_actor_id = selected.get('actor_id')
            self.reference_text.delete('1.0', 'end')
            self.reference_text.insert('1.0', self.current_reference_transcript)
            gxt = selected.get('gxt') or {}
            self.target_text.delete('1.0', 'end')
            self.target_text.insert('1.0', gxt.get('hypertranslated_text', ''))
            gid = gxt.get('stable_id')
            row_id = next((key for key, item in self.gxt_rows.items() if item['gid'] == gid), None)
            if row_id:
                self.gxt_tree.selection_set(row_id)
        self._render_segments()
        self._draw_waveform()
        self.status.set('Rascunho carregado e hashes de origem conferidos.')

    def _segment_index(self, identity):
        if not identity:
            return None
        return next((index for index, row in enumerate(self.segments) if row.get('id') == identity), None)

    def _mark_dirty(self):
        self.edit_revision += 1
        self.draft_saved = False

    def _allow_overlap(self):
        variable = getattr(self, 'overlap_var', None)
        return bool(variable and variable.get())

    def _background_mode(self):
        variable = getattr(self, 'background_var', None)
        return audio.BACKGROUND_MODES[variable.get()] if variable else 'voice_only'

    def _render_mix(self):
        if not self.track_data:
            return
        background_mode = self._background_mode()
        if background_mode == 'preserve_original' and (not self.segments or any(not segment.get('replacement') for segment in self.segments)):
            messagebox.showinfo('Faixa completa', 'Adicione trechos e associe um WAV de reposição para cada um.', parent=self.win)
            return
        destination = self.project / 'workspace' / 'cutscene_studio' / 'renders' / (
            '%s_%s.wav' % (self.track['track'], uuid.uuid4().hex))
        approved = [segment for segment in self.segments if segment.get('replacement')]
        missing = [{'start': segment['start'], 'end': segment['end'], 'gxt': segment.get('gxt')}
                   for segment in self.segments if not segment.get('replacement')]
        replacements = [segment['replacement'] for segment in approved]
        ranges = [{'start': segment['start'], 'end': segment['end']} for segment in approved]
        fit = bool(self.fit_var.get()) and not any(item.get('engine') == 'OmniVoice' for item in replacements)
        if self.fit_var.get() and not fit:
            self.fit_info.set('Ajuste pós-geração desativado porque o rascunho contém take OmniVoice.')
        self._submit('render', audio.render_track, self.track_data, replacements, ranges, destination,
                     fit=fit, allow_overlap=self._allow_overlap(), background_mode=background_mode,
                     missing_ptbr=missing)

    def _draw_waveform(self):
        canvas = self.wave
        canvas.delete('all')
        ruler = self.ruler
        ruler.delete('all')
        if not self.track_data:
            canvas.create_text(15, 20, anchor='nw', fill='#cbd5e1', text='Carregue uma faixa para exibir sua forma de onda.')
            return
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        duration = self.track_data['duration']
        mid = height / 2
        peaks = self.track_data['waveform']
        step = max(1, width // max(1, len(peaks)))
        for index, peak in enumerate(peaks):
            x = int(index * width / len(peaks))
            amp = max(1, peak * (height * .45))
            canvas.create_line(x, mid - amp, x, mid + amp, fill='#38bdf8', width=1)
        try:
            start, end = self._current_bounds()
        except ValueError:
            start, end = 0, 0
        sx, ex = start / duration * width, end / duration * width
        canvas.create_rectangle(sx, 0, ex, height, fill='#f59e0b', stipple='gray25', outline='#fbbf24', width=2)
        canvas.create_line(sx, 0, sx, height, fill='#fde68a', width=2)
        canvas.create_line(ex, 0, ex, height, fill='#fde68a', width=2)
        for segment in self.segments:
            x1, x2 = segment['start'] / duration * width, segment['end'] / duration * width
            canvas.create_line(x1, 0, x2, 0, fill='#4ade80', width=6)
        ticks = max(1, int(round(duration / 10)))
        for second in range(0, int(duration) + 1, ticks):
            x = second / max(duration, 1) * width
            if x <= width:
                ruler.create_line(x, 0, x, 8, fill='#334155')
                ruler.create_text(x, 18, text='%ds' % second, fill='#334155', anchor='n')

    def _drag_start(self, event):
        if not self.track_data:
            return
        try:
            start, end = self._current_bounds()
        except ValueError:
            start, end = 0, 1
        duration = self.track_data['duration']
        x1 = start / duration * max(1, self.wave.winfo_width())
        x2 = end / duration * max(1, self.wave.winfo_width())
        self.drag_handle = 'start' if abs(event.x - x1) <= abs(event.x - x2) else 'end'
        self._drag_move(event)

    def _drag_move(self, event):
        if not self.drag_handle or not self.track_data:
            return
        duration = self.track_data['duration']
        value = max(0, min(duration, event.x / max(1, self.wave.winfo_width()) * duration))
        try:
            start, end = self._current_bounds()
        except ValueError:
            start, end = 0, duration
        if self.drag_handle == 'start':
            start = min(value, end - 1 / self.track_data['sample_rate'])
            self.start_var.set('%.3f' % max(0, start))
        else:
            end = max(value, start + 1 / self.track_data['sample_rate'])
            self.end_var.set('%.3f' % min(duration, end))
        self._draw_waveform()

    def _drag_end(self, _event):
        self.drag_handle = None

    def _log(self, line):
        self.log.configure(state='normal')
        self.log.insert('end', str(line) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _toggle_log(self):
        if self.log_visible:
            self.log_frame.pack_forget()
            self.log_visible = False
        else:
            self.log_frame.pack(fill='x')
            self.log_visible = True

    def _cancel_job(self):
        if self.tts_job:
            self.tts_job.cancel()
            self.tts_status.set('Cancelando geração Qwen…')
        if self.job:
            self.cancel_event.set()
        if not self.job and not self.tts_job:
            self.status.set('Nenhuma operação ativa.')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        if self.tts_job:
            self.tts_job.cancel()
        try:
            self.app.parar_audio()
        except Exception:
            pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.win.destroy()


__all__ = ['CutsceneStudioWindow']
