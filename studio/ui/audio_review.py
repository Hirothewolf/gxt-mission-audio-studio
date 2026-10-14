"""Per-line audition, voice references and non-destructive effects."""
import threading
import uuid
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from studio.audio.analyzer import analyze_wav
from studio.audio.effects import render_effect
from studio.audio.wav import wav_duracao, classe_delta
from studio.gxt.codec import decode_text_c
from studio.core.utils import mostrar_no_explorer, sha256_arquivo
from studio.audio.source_routing import is_stream_record


class AudioReviewWindow:
    def __init__(self, app):
        self.app = app
        self.store = app.review_store
        self.gxt_context = app.g
        self.tab, self.h = app.sel
        self.line_id = '%s|%08x' % app.sel
        self.text = decode_text_c(app.g.t[self.tab]['cur'][self.h])
        self.info = app.info_script(self.tab, self.h) or {}
        self.original = app.audio_atual
        self.project = app.audio_dublado_atual
        self.installed = app.audio_instalado_atual
        self.original_duration = wav_duracao(self.original) if self.original else None
        try:
            self.mapped_window_seconds = float(self.info.get('duration_seconds'))
        except (TypeError, ValueError):
            self.mapped_window_seconds = None
        self.paths = {}
        self.candidates = {}
        self.analyses = {}
        self.closed = False
        self.cancel_event = threading.Event()
        self.busy = False
        self.generation_busy = False
        self.install_busy = False
        self.generation_job = None
        self.win = tk.Toplevel(app.root)
        self.win.title('Revisão de voz e áudio — %s' % (self.info.get('event_name') or self.line_id))
        self.win.geometry('980x700')
        self.win.minsize(860, 660)
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self.win.bind('<Escape>', lambda e: self.app.parar_audio())
        self._build()
        self.refresh()

    def current_text(self):
        if self.app.g is not self.gxt_context:
            raise ValueError('O arquivo GXT mudou. Abra uma nova revisão desta fala.')
        if self.app.sel == (self.tab, self.h):
            self.app.commit_silencioso()
        return decode_text_c(self.app.g.t[self.tab]['cur'][self.h])

    def _duration_context(self, take_duration=None):
        vanilla = ('%.2fs' % self.original_duration) if self.original_duration else 'indisponível'
        mapped = ('%.2fs' % self.mapped_window_seconds) if self.mapped_window_seconds else 'não mapeada'
        take = ('%.2fs' % take_duration) if take_duration else '—'
        return 'Duração: original base %s · janela mapeada do mod %s · take selecionado %s' % (vanilla, mapped, take)

    def effect_source_text(self, selected):
        """Derivatives retain the transcript attributed to their source take."""
        candidate = self.candidates.get(selected)
        if not candidate:
            raise ValueError('Importe o áudio como take e confira o texto antes de aplicar efeitos. O original pode ser guardado como referência de voz.')
        return self.store.validate_candidate_source(self.line_id, selected)['text']

    def _build(self):
        frame = ttk.Frame(self.win, padding=8)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='%s · %s' % (self.tab, self.info.get('event_name') or '%08X' % self.h),
                  font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=self.text, wraplength=1000).pack(fill='x', pady=(2, 4))
        self.duration_context_var = tk.StringVar(value=self._duration_context())
        ttk.Label(frame, textvariable=self.duration_context_var, foreground='#455a70').pack(anchor='w')
        identity = self.info.get('speaker_identity') or 'Desconhecido'
        status = self.info.get('speaker_identity_status') or 'Sem evidência de identidade'
        status = {'PENDING_LISTENING': 'Identificação pendente de escuta',
                  'MANUAL': 'Identificação manual', 'WIKI_EXACT_TEXT': 'Wiki: texto original correspondente; voz ainda precisa de escuta',
                  'VERIFIED': 'Identidade verificada no catálogo',
                  'VOICE_CONFIRMED': 'Grupo de voz confirmado por você',
                  'VOICE_SUGGESTED': 'Sugestão por semelhança de voz; precisa de escuta',
                  'WIKI_AMBIGUOUS': 'Wiki: mais de uma identidade possível'}.get(status, status)
        kind = {'voice_archetype': 'Tipo de voz', 'group_or_ensemble': 'Grupo', 'role_label': 'Papel'}.get(self.info.get('speaker_kind'))
        if kind:
            identity = '%s: %s' % (kind, identity)
        ttk.Label(frame, text='Identidade do mapeamento: %s · %s' % (identity, status)).pack(anchor='w')
        voice = ttk.Frame(frame)
        voice.pack(fill='x', pady=(3, 4))
        ttk.Label(voice, text='Personagem confirmado por você:').pack(side='left')
        confirmed = self.store.character(self.line_id)
        if not confirmed and self.info.get('speaker_identity_status') in ('VERIFIED', 'VOICE_CONFIRMED'):
            confirmed = self.info.get('speaker_identity') or ''
        self.character_var = tk.StringVar(value=confirmed)
        self.character_box = ttk.Combobox(voice, textvariable=self.character_var, width=25,
                                         values=self.store.characters())
        self.character_box.pack(side='left', padx=6)
        ttk.Button(voice, text='Guardar personagem', command=self.save_character).pack(side='left')
        notebook = ttk.Notebook(frame)
        notebook.pack(fill='both', expand=True)
        self.notebook = notebook
        takes = ttk.Frame(notebook, padding=8)
        refs = ttk.Frame(notebook, padding=8)
        notebook.add(takes, text='Ouvir e revisar takes')
        notebook.add(refs, text='Referências da voz')
        generate_tab = ttk.Frame(notebook, padding=12)
        self.generation_tab = generate_tab
        notebook.add(generate_tab, text='Gerar / Nova tentativa')
        generate = ttk.LabelFrame(generate_tab, text=' OmniVoice · geração por fala ', padding=8)
        generate.pack(fill='x', pady=(0, 8))
        ttk.Label(generate, text='Engine: OmniVoice (runtime local)').pack(side='left', padx=(0, 10))
        self.omni_speed_var = tk.StringVar(value=str(self.app.var_omnivoice_speed.get()))
        self.omni_duration_var = tk.StringVar(value=str(self.app.var_omnivoice_duration.get()))
        from studio.config import CFG
        self.omni_seed_var = tk.StringVar(value=str(CFG.get('omnivoice_seed', 20261009)))
        self.omni_cj_reference_var = tk.BooleanVar(value=bool(self.app.var_omnivoice_cj_reference.get()))
        ttk.Label(generate, text='Velocidade').pack(side='left')
        ttk.Spinbox(generate, textvariable=self.omni_speed_var, from_=.5, to=2.0, increment=.05, width=6).pack(side='left', padx=(3, 10))
        ttk.Label(generate, text='Duração nativa').pack(side='left')
        ttk.Entry(generate, textvariable=self.omni_duration_var, width=8).pack(side='left', padx=(3, 10))
        ttk.Label(generate, text='original / livre / segundos').pack(side='left')
        ttk.Label(generate, text='Seed').pack(side='left', padx=(10, 0))
        ttk.Entry(generate, textvariable=self.omni_seed_var, width=10).pack(side='left', padx=(3, 10))
        self.generate_btn = ttk.Button(generate, text='Gerar take / nova tentativa', command=self.generate_omnivoice_take)
        self.generate_btn.pack(side='right')
        # Keep parameter pairs visible even at the minimum window width.
        generation_controls = generate.winfo_children()
        for widget in generation_controls:
            widget.pack_forget()
        for widget, placement in zip(generation_controls,
                [(0, 0, 6), (1, 0, 1), (1, 1, 1), (1, 2, 1), (1, 3, 1),
                 (1, 4, 2), (2, 0, 1), (2, 1, 1), (2, 3, 3)]):
            row, column, span = placement
            widget.grid(row=row, column=column, columnspan=span, sticky='w', padx=3, pady=3)
        options = ttk.Frame(generate_tab)
        options.pack(fill='x', pady=(3, 0))
        ttk.Checkbutton(options, text='Usar referência CJ aceita (identidade manual obrigatória)',
                        variable=self.omni_cj_reference_var).pack(side='left')
        self.omni_reference_var = tk.StringVar(value='A referência e a transcrição original serão mostradas ao iniciar a geração.')
        ttk.Label(generate_tab, textvariable=self.omni_reference_var, wraplength=970).pack(fill='x', pady=(0, 6))
        columns = ('source', 'duration', 'delta', 'status')
        ttk.Button(takes, text='Gerar novamente…  ·  escolher velocidade, duração e seed',
                   command=self.open_generation_tab).pack(anchor='e', pady=(0, 6))
        self.tree = ttk.Treeview(takes, columns=columns, show='headings', height=6, selectmode='extended')
        for col, title, width in zip(columns, ('Áudio', 'Duração', 'Diferença do original', 'Revisão'), (260, 90, 260, 200)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._selection_changed())
        self.tree.bind('<Double-1>', lambda e: self.play())
        self.tree.bind('<space>', lambda e: (self.play(), 'break')[1])
        b = ttk.Frame(takes)
        b.pack(fill='x', pady=6)
        self.action_buttons = {}
        for index, (label, command) in enumerate([('Ouvir original', lambda: self.app._tocar(self.original, 'original')),
                               ('Ouvir gerado', self.play), ('Comparar A/B', self.compare),
                               ('Parar', self.app.parar_audio), ('Aceitar take', lambda: self.status('approved')),
                               ('Recusar take', lambda: self.status('rejected')),
                               ('Importar WAV', self.import_wav), ('Localizar arquivo', self.locate)]):
            button = ttk.Button(b, text=label, command=command)
            button.grid(row=index // 4, column=index % 4, sticky='ew', padx=(0, 4), pady=2)
            b.columnconfigure(index % 4, weight=1)
            self.action_buttons[label] = button
            if label == 'Aceitar take':
                self.accept_btn = button
            elif label == 'Recusar take':
                self.reject_btn = button
        self.duration_override_var = tk.BooleanVar(value=False)
        self.duration_override_check = ttk.Checkbutton(
            takes, text='Aceitar mesmo excedendo a janela original (override manual de duração)',
            variable=self.duration_override_var)
        self.duration_override_check.pack(anchor='w', pady=(0, 3))
        self.canvas = tk.Canvas(takes, height=56, background='#19222d', highlightthickness=0)
        self.canvas.pack(fill='x', pady=(4, 6))
        self.canvas.bind('<Configure>', lambda e: self.show_analysis())
        self.qa_var = tk.StringVar(value='Selecione um áudio para ver duração, silêncio e clipping.')
        ttk.Label(takes, textvariable=self.qa_var, wraplength=970).pack(fill='x')
        effect = ttk.LabelFrame(takes, text=' Novo take com efeitos (preserva o áudio fonte) ', padding=8)
        effect.pack(fill='x', pady=4)
        self.speed_var = tk.StringVar(value='1.0')
        self.pitch_var = tk.StringVar(value='0')
        self.reverb_var = tk.StringVar(value='0')
        for label, variable, low, high, step in [('Velocidade', self.speed_var, .5, 2, .05),
                                                 ('Tom (semitons)', self.pitch_var, -12, 12, 1),
                                                 ('Ambiência / eco', self.reverb_var, 0, 1, .1)]:
            ttk.Label(effect, text=label).pack(side='left')
            ttk.Spinbox(effect, textvariable=variable, from_=low, to=high, increment=step, width=6).pack(side='left', padx=(4, 12))
        self.effect_btn = ttk.Button(effect, text='Criar take', command=self.effect)
        self.effect_btn.pack(side='left')
        ttk.Label(takes, text='0,8× deixa a fala mais lenta. Tom muda a altura da voz. Ambiência usa reflexões/eco. '
                  'A análise numérica não identifica robotização nem garante qualidade perceptual.', wraplength=970).pack(fill='x')
        self.reference_tree = ttk.Treeview(refs, columns=('name', 'duration', 'emotion'), show='headings', height=7)
        for c, title, width in [('name', 'Referência', 350), ('duration', 'Duração', 100), ('emotion', 'Emoção', 150)]:
            self.reference_tree.heading(c, text=title)
            self.reference_tree.column(c, width=width)
        self.reference_tree.pack(fill='both', expand=True)
        ttk.Label(refs, text='Transcrição do áudio de referência (no idioma realmente falado):').pack(anchor='w', pady=(8, 2))
        self.transcript = tk.Text(refs, height=3, wrap='word')
        self.transcript.pack(fill='x')
        if self.app.c:
            source = self.app.ref(self.tab, self.h)
            if source is not None:
                self.transcript.insert('1.0', decode_text_c(source))
        self.reference_tree.bind('<<TreeviewSelect>>', lambda e: self.select_reference())
        rbuttons = ttk.Frame(refs)
        rbuttons.pack(fill='x', pady=8)
        for label, callback in [('Guardar original como referência', self.add_original_reference),
                                ('Importar referência WAV', self.import_reference),
                                ('Ouvir referência', self.play_reference),
                                ('Remover do perfil', self.remove_reference)]:
            ttk.Button(rbuttons, text=label, command=callback).pack(side='left', padx=(0, 5))
        ttk.Label(refs, text='As referências ficam guardadas com personagem, transcrição e origem. '
                  'Os scripts TTS legados ainda escolhem suas próprias referências internas; '
                  'a seleção manual na síntese requer um adapter compatível.', wraplength=970).pack(fill='x')
        self.state_var = tk.StringVar(value='Pronto')
        ttk.Label(frame, textvariable=self.state_var, font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(4, 0))
        self.message_var = tk.StringVar(value='GXT Save grava só o texto. Aceitar guarda o take no projeto; aplique o áudio ao mod separadamente.')
        ttk.Label(frame, textvariable=self.message_var, wraplength=1000).pack(fill='x', pady=(8, 0))
        self.apply_audio_btn = ttk.Button(frame, text='Aplicar áudio aprovado ao mod…',
                                          command=self.apply_approved_to_mod)
        self.apply_audio_btn.pack(anchor='e', pady=(5, 0))

    def apply_approved_to_mod(self):
        """Explicitly install this line's accepted take into the configured mod."""
        if self.install_busy:
            return
        ga = getattr(self.app, 'jogo', None)
        if ga is None:
            messagebox.showerror('Aplicar áudio', 'O serviço de áudio do ModLoader não está inicializado.', parent=self.win)
            return
        try:
            text = self.current_text()
            approved = self.store.approved_path(self.line_id, text)
            if not approved:
                raise ValueError('A fala não tem um take aceito para o texto atual.')
            stream = is_stream_record(self.info)
            ref = None if stream else ga.ref_para(self.info)
            mod = ga.mod_ativo()
            if stream:
                candidate = next((c for c in self.store.list_candidates(self.line_id, text)
                                  if not c['stale'] and c['status'] == 'approved' and
                                  self.store.resolve_path(c['path']) == approved), None)
                if not candidate or sha256_arquivo(approved) != candidate['sha256']:
                    raise ValueError('O WAV aprovado mudou; revise o take novamente.')
                stream_row = dict(line_id=self.line_id, text=text, path=str(approved),
                                  sha256=candidate['sha256'], info=self.info)
                prompt = ('Aplicar o take aceito à faixa contínua no mod "%s"?\n\n'
                          'A faixa será montada com backup e conferência dos demais trechos. '
                          'O jogo precisa estar fechado.') % mod
            else:
                if not ref:
                    raise ValueError('Esta fala não tem destino de áudio nativo identificado.')
                plan = ga.planejar([{'rotulo': self.info.get('event_name') or self.line_id,
                                     'ref': ref, 'dub': str(approved)}], mod)
                if not plan or plan[0]['problemas']:
                    raise ValueError('; '.join(plan[0]['problemas']) if plan else 'Não foi possível preparar o áudio.')
                prompt = ('Aplicar o take aceito ao slot %s, no mod "%s"?\n\n'
                          'O banco nativo será reconstruído a partir do arquivo atual desse mod, '
                          'todos os demais slots serão conferidos por roundtrip e um backup será guardado. '
                          'O jogo precisa estar fechado.') % (ref, mod)
            if not messagebox.askyesno('Aplicar áudio ao mod', prompt, parent=self.win):
                return
        except (ValueError, OSError) as exc:
            messagebox.showerror('Aplicar áudio', str(exc), parent=self.win)
            return
        self.install_busy = True
        self._sync_review_controls()
        self.message_var.set('Aplicando áudio aprovado ao mod… reconstruindo e conferindo o banco nativo. O take continua salvo no projeto.')
        self._set_state('Aplicando áudio aprovado ao mod…')
        def worker():
            try:
                if stream:
                    from studio.audio.stream_install import install_stream_rows
                    response = install_stream_rows(self.store, [stream_row], ga, mod)
                    reasons = [entry['reason'] for key in ('errors', 'skipped') for entry in response.get(key, [])]
                    result = dict(response, ok=self.line_id in response.get('installed', []),
                                  motivo='; '.join(reasons) or 'A faixa não foi aplicada.')
                else:
                    result = ga.instalar(plan, mod)
                if not isinstance(result, dict):
                    result = {'ok': False, 'motivo': 'O instalador retornou uma resposta inválida.'}
            except Exception as exc:
                result = {'ok': False,
                          'motivo': 'Falha inesperada ao aplicar o áudio (%s). Nenhuma confirmação de instalação foi recebida.' %
                                    (str(exc) or exc.__class__.__name__)}
            def finish():
                self.install_busy = False
                if self.closed:
                    return
                self._sync_review_controls()
                if result.get('ok'):
                    self.message_var.set('Áudio aplicado ao archive nativo %s do mod %s. Backup: %s' %
                                         ('de faixa contínua' if stream else ref[0], mod, result.get('backup', '')))
                    installed_path = result.get('installedpaths', {}).get(self.line_id)
                    if installed_path:
                        self.installed = Path(installed_path)
                    self.app.status('Áudio nativo aplicado ao mod %s.' % mod)
                    self.app.atualizar_linha(self.tab, self.h)
                    if self.app.sel == (self.tab, self.h):
                        self.app._carregar_linha(self.tab, self.h)
                    self.refresh()
                else:
                    self.message_var.set('Áudio não aplicado: %s' % result.get('motivo', 'falha desconhecida'))
                    self._set_state('Aplicação do áudio falhou; o take continua salvo no projeto.')
                    messagebox.showerror('Aplicar áudio', result.get('motivo', 'Falha desconhecida.'), parent=self.win)
            self.app.ui(finish)
        threading.Thread(target=worker, daemon=True).start()

    def selected(self):
        items = self.tree.selection()
        return items[0] if items else None

    def open_generation_tab(self):
        """Open the per-line OmniVoice settings and retry controls."""
        self.notebook.select(self.generation_tab)

    def refresh(self, select=None):
        if self.closed:
            return
        self._set_state('Carregando análise…')
        self.text = self.current_text()
        if hasattr(self, 'duration_context_var'):
            self.duration_context_var.set(self._duration_context())
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        self.candidates.clear()
        sources = [('original', 'Original', self.original, 'Fonte preservada'),
                   ('project', 'Dublado do projeto', self.project, 'Legado / aprovação não registrada'),
                   ('installed', 'Instalado no ModLoader', self.installed, 'Arquivo encontrado · conferir no jogo')]
        for c in self.store.list_candidates(self.line_id, self.text):
            cid = c['id']
            self.candidates[cid] = c
            if c['stale']:
                status = 'Texto alterado'
            elif c.get('metadata', {}).get('duration_override_required') and c['status'] == 'pending':
                status = 'Excede janela · requer aceite manual'
            else:
                status = {'pending': 'Precisa ouvir', 'approved': 'Aprovado no projeto', 'rejected': 'Recusado'}[c['status']]
            sources.append((cid, 'Take %s · %s' % (cid[:6], c['origin']), self.store.resolve_path(c['path']), status))
        for iid, label, path, status in sources:
            if path and Path(path).is_file():
                self.paths[iid] = Path(path)
                duration = wav_duracao(path)
                self.tree.insert('', 'end', iid=iid, values=(label, '%.2fs' % duration if duration else 'Inválido',
                                                            classe_delta(self.original_duration, duration), status))
            elif iid in ('original', 'project', 'installed'):
                self.tree.insert('', 'end', iid=iid, values=(label, '—', '', 'Indisponível'))
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(select if select and self.tree.exists(select) else children[0])
        self.refresh_references()
        paths = dict(self.paths)
        token = getattr(self, 'analysis_token', 0) + 1
        self.analysis_token = token
        def work():
            try:
                results = {k: analyze_wav(v, self.original_duration) for k, v in paths.items()}
            except Exception as exc:
                self.app.ui(lambda error=str(exc): self._analysis_failed(error, token))
                return
            self.app.ui(lambda: self._analyses_ready(results, token))
        threading.Thread(target=work, daemon=True).start()
        self._sync_review_controls()

    def _analysis_failed(self, error, token):
        if not self.closed and token == getattr(self, 'analysis_token', None):
            self.qa_var.set('Erro na análise: ' + error)
            if not self.busy and not self.generation_busy:
                self._set_state('Erro na análise')

    def _analyses_ready(self, results, token=None):
        if not self.closed and (token is None or token == getattr(self, 'analysis_token', None)):
            self.analyses.update(results)
            self.show_analysis()
            if not self.busy and not self.generation_busy:
                self._set_state('Pronto para revisar')

    def _selection_changed(self):
        self.show_analysis()
        self._sync_review_controls()

    def show_analysis(self):
        if self.closed:
            return
        self.canvas.delete('all')
        data = self.analyses.get(self.selected())
        if not data:
            self.qa_var.set('Análise em andamento ou áudio indisponível.')
            return
        if not data.get('valid'):
            self.qa_var.set('WAV inválido: %s' % data.get('error'))
            return
        warnings = '; '.join(data.get('warnings', [])) or 'Sem alertas técnicos nestas métricas'
        from studio.ui.audition import wer_warning
        wer = wer_warning(self.candidates.get(self.selected(), {}))
        if wer:
            warnings += ' · ' + wer
        self.qa_var.set('%.2fs · %s Hz · %s canal(is) · silêncio %.1f%% · clipping %.2f%% · %s' % (
            data['duration'], data['sample_rate'], data['channels'], data['silence_ratio'] * 100,
            data['clipping_ratio'] * 100, warnings))
        self.duration_context_var.set(self._duration_context(data['duration']))
        bins = data.get('waveform', [])
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_line(0, height / 2, width, height / 2, fill='#526075')
        for i, value in enumerate(bins):
            amplitude = max(abs(v) for v in value) if isinstance(value, (list, tuple)) else abs(value)
            x = i * width / max(1, len(bins))
            self.canvas.create_line(x, height / 2 - amplitude * (height / 2 - 5), x, height / 2 + amplitude * (height / 2 - 5), fill='#64d9b1')

    def play(self):
        self.app._tocar(self.paths.get(self.selected()), 'take de revisão')

    def _set_state(self, text):
        if text.startswith('Erro'):
            self.analysis_token = getattr(self, 'analysis_token', 0) + 1
        if not self.closed and hasattr(self, 'state_var'):
            self.state_var.set(text)

    def _generation_log(self, line, run):
        from studio.core.utils import escrever_log
        escrever_log('tts.log', line, base_dir=run)
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        stage = str(event.get('stage', '')).lower() if isinstance(event, dict) else ''
        label = {'loading': 'Carregando modelo…', 'load_model': 'Carregando modelo…',
                 'generating': 'Gerando áudio…', 'synthesis': 'Gerando áudio…',
                 'saving': 'Guardando take…'}.get(stage)
        if label:
            self.app.ui(lambda: self._set_state(label) if self.generation_busy and getattr(self, 'generation_run', None) == run else None)

    def _sync_review_controls(self):
        busy = self.busy or self.generation_busy
        candidate = self.candidates.get(self.selected())
        available = self.selected() in getattr(self, 'paths', {})
        for label, button in getattr(self, 'action_buttons', {}).items():
            enabled = {'Ouvir original': bool(self.original), 'Ouvir gerado': available,
                       'Comparar A/B': available and (bool(self.original) or len(self.tree.selection()) > 1),
                       'Localizar arquivo': available, 'Importar WAV': not busy}.get(label, True)
            button.config(state='normal' if enabled else 'disabled')
        for name in ('accept_btn', 'reject_btn'):
            button = getattr(self, name, None)
            if button:
                button.config(state='normal' if candidate and not candidate.get('stale') and not busy else 'disabled')
        button = getattr(self, 'generate_btn', None)
        if button:
            button.config(text='Cancelar geração' if self.generation_busy else 'Gerar take / nova tentativa',
                          state='disabled' if self.busy else 'normal')
        effect = getattr(self, 'effect_btn', None)
        if effect:
            effect.config(state='normal' if self.busy or (candidate and not candidate.get('stale') and available and not self.generation_busy) else 'disabled')
        candidate = self.candidates.get(self.selected())
        check = getattr(self, 'duration_override_check', None)
        variable = getattr(self, 'duration_override_var', None)
        can_override = bool(candidate and candidate.get('metadata', {}).get('duration_override_required')
                            and not candidate.get('stale'))
        if check:
            check.config(state='normal' if can_override and not busy else 'disabled')
        if variable:
            variable.set(False)
            if can_override:
                self.duration_context_var.set(self._duration_context(candidate.get('metadata', {}).get('duration_seconds')))
        apply_button = getattr(self, 'apply_audio_btn', None)
        if apply_button:
            apply_button.config(state='disabled' if self.install_busy else 'normal')

    def _set_generation_result(self, candidate=None, error=None):
        self.generation_busy = False
        self.generation_job = None
        if self.closed:
            return
        self._sync_review_controls()
        if error:
            from studio.ui.app import _generation_user_message
            error = _generation_user_message(error)
            self._set_state('Erro na geração')
            self.message_var.set(error)
            messagebox.showerror('Geração OmniVoice', error, parent=self.win)
            return
        try:
            if self.app.g is not self.gxt_context:
                self.message_var.set('Take guardado para o GXT que estava aberto na geração; reabra a revisão para conferir. Não foi instalado.')
                return
            self.refresh(candidate['id'] if candidate else None)
        except ValueError:
            self.message_var.set('Take guardado para o texto usado na geração; o GXT mudou. Reabra a revisão para conferir. Não foi instalado.')
            return
        if candidate and candidate.get('metadata', {}).get('duration_override_required'):
            meta = candidate['metadata']
            self.message_var.set('Take preservado para revisão: %.2fs gerados para janela de %.2fs. Marque o override só se quiser aceitar.' % (
                meta['duration_seconds'], meta['window_seconds']))
        else:
            self.message_var.set('Novo take OmniVoice guardado. Ouça antes de aceitar ou recusar.')

    @staticmethod
    def _result_payload(logs):
        for line in reversed(logs or []):
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get('engine') == 'OmniVoice' and value.get('output_path'):
                return value
            if isinstance(value, dict) and value.get('status') in {
                    'UNREVIEWED_WITHIN_WINDOW', 'QUARANTINED_DURATION', 'QUARANTINED_SILENCE'} and value.get('output_path'):
                return value
        return None

    def generate_omnivoice_take(self):
        if self.generation_busy:
            if self.generation_job:
                self.generation_job.cancel()
            self.message_var.set('Cancelando geração; a saída parcial não será importada como take.')
            return
        if self.busy:
            self.message_var.set('Aguarde o efeito de áudio atual terminar antes de gerar.')
            return
        if self.app.g is not self.gxt_context:
            self.message_var.set('O GXT mudou. Feche e abra a revisão desta fala novamente.')
            return
        try:
            from studio.config import CFG, PROJ_ROOT
            from studio.tts.registry import select_engine
            selected_engine = select_engine(root=PROJ_ROOT)
            if getattr(selected_engine, 'id', None) != 'omnivoice':
                raise ValueError('Esta aba gera somente com OmniVoice; selecione OmniVoice no menu principal. Nenhum outro engine será usado como fallback.')
            engine, run, texts = self.app._prepare_generation(self.tab, selected_hash=self.h)
            if getattr(engine, 'id', None) != 'omnivoice':
                raise ValueError('Escolha OmniVoice no menu de engine antes de gerar nesta revisão.')
            snapshot_text = texts[self.h]
            event = self.app.nomes_tabela(self.tab, forcar=True).get(self.h)
            settings = {
                'speed': self.omni_speed_var.get(), 'duration': self.omni_duration_var.get(),
                'seed': int(self.omni_seed_var.get()),
                'use_cj_reference': bool(self.omni_cj_reference_var.get()),
            }
            command = self.app._omnivoice_command_for_selection(
                engine, run, self.tab, self.h, event, snapshot_text, settings=settings)
            request = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
            self.omni_reference_var.set(
                'Referência: %s · ID %s · EN: %s · velocidade %.2f · duração nativa %s · seed %s · áudio original %.2fs' % (
                    request['reference_path'], request.get('provenance', {}).get('reference_audio_id') or
                    request.get('provenance', {}).get('coordinates') or 'origem verificada',
                    request['reference_text'], request['speed'],
                    request.get('duration') if request.get('duration') is not None else 'livre',
                    request['seed'], request['window_seconds']))
            self.generation_snapshot_text = snapshot_text
            self.generation_run = run
            self.generation_busy = True
            self._set_state('Carregando / gerando áudio…')
            self._sync_review_controls()
            self.message_var.set('Gerando novo take em uma pasta própria; nenhuma versão anterior será substituída.')
            from studio.core.utils import escrever_log
            job = self.app.jobs.submit_exclusive(
                command, cwd=PROJ_ROOT,
                on_line=lambda line: self._generation_log(line, run),
                timeout=float(CFG.get('tts_timeout_seconds', 600)))
            self.generation_job = job
        except (ValueError, RuntimeError, OSError, TypeError) as exc:
            self.generation_busy = False
            self._sync_review_controls()
            self._set_state('Erro na geração')
            self.message_var.set(str(exc))
            return

        def wait_for_job():
            result = job.wait()
            candidate, error = None, None
            if result.cancelled:
                error = 'Geração cancelada; nenhum take foi aceito. A pasta do run foi preservada.'
            elif result.timed_out:
                error = 'A geração excedeu o tempo limite. O run foi preservado para diagnóstico.'
            elif result.returncode != 0:
                from studio.ui.app import _generation_failure_detail
                detail = result.error or _generation_failure_detail(result, 'omnivoice')
                error = '%s\nRun preservado: %s' % (detail, self.generation_run)
            else:
                try:
                    payload = self._result_payload(result.logs)
                    if not payload:
                        raise ValueError('O worker não retornou o receipt OmniVoice da tentativa.')
                    output = Path(payload['output_path'])
                    receipt = output.with_suffix('.omnivoice.json')
                    candidate = self.store.import_omnivoice_candidate(
                        self.line_id, output, self.generation_snapshot_text, receipt)
                except (ValueError, OSError, KeyError) as exc:
                    error = '%s\nRun preservado: %s' % (exc, self.generation_run)
            self.app.ui(lambda: self._set_generation_result(candidate, error))
        self.generation_thread = threading.Thread(target=wait_for_job, daemon=True)
        self.generation_thread.start()

    def compare(self):
        paths = [self.paths[k] for k in self.tree.selection() if k in self.paths]
        if len(paths) == 1 and self.original and Path(self.original) != paths[0]:
            paths.insert(0, self.original)
        if len(paths) < 2:
            self.message_var.set('Selecione dois áudios com Ctrl+clique ou um take para comparar com o original.')
            return
        self.app.player.play_sequence(paths, pausa_ms=self.app.var_pausa_ab.get())

    def status(self, status):
        iid = self.selected()
        if iid not in self.candidates:
            self.message_var.set('Importe o WAV legado como take para registrar uma aprovação.')
            return
        try:
            override = bool(self.duration_override_var.get()) if status == 'approved' else False
            if override:
                candidate = self.candidates.get(iid, {})
                metadata = candidate.get('metadata') or {}
                if not metadata.get('duration_override_required'):
                    self.message_var.set('O override só está disponível para quarentena por duração.')
                    return
            self.store.set_status(self.line_id, iid, status, self.current_text(), duration_override=override)
            self.app.limpar_cache_dublado(self.tab)
            self.app.atualizar_linha(self.tab, self.h)
            if self.app.sel == (self.tab, self.h):
                self.app._carregar_linha(self.tab, self.h)
            self.refresh(iid)
            self.message_var.set('Take %s. Nenhum arquivo do GTA foi alterado.' % ('aceito' if status == 'approved' else 'recusado'))
        except (ValueError, OSError) as exc:
            messagebox.showerror('Revisão', str(exc), parent=self.win)

    def import_wav(self):
        source = filedialog.askopenfilename(parent=self.win, title='Importar como novo take', filetypes=[('WAV', '*.wav')])
        if source:
            self._import(source, 'importado')

    def _import(self, source, origin, metadata=None, analysis=None, text=None):
        try:
            candidate = self.store.import_candidate(self.line_id, source, text if text is not None else self.current_text(),
                                                    origin, metadata, analysis)
            self.refresh(candidate['id'])
            self.message_var.set('Novo take guardado. Ouça e aprove quando estiver satisfeito.')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Importar take', str(exc), parent=self.win)

    def locate(self):
        path = self.paths.get(self.selected())
        if path:
            mostrar_no_explorer(path)

    def save_character(self):
        try:
            self.store.assign_character(self.line_id, self.character_var.get())
            self.character_box.config(values=self.store.characters())
            self.refresh_references()
            self.message_var.set('Personagem guardado com identificação manual.')
        except OSError as exc:
            messagebox.showerror('Personagem', str(exc), parent=self.win)

    def refresh_references(self):
        self.reference_tree.delete(*self.reference_tree.get_children())
        self.refs = {r['id']: r for r in self.store.references(self.character_var.get().strip())}
        for rid, r in self.refs.items():
            self.reference_tree.insert('', 'end', iid=rid, values=(r['name'], '%.2fs' % r['duration'], r['emotion']))

    def selected_reference(self):
        s = self.reference_tree.selection()
        return self.refs.get(s[0]) if s else None

    def select_reference(self):
        ref = self.selected_reference()
        if ref:
            self.transcript.delete('1.0', 'end')
            self.transcript.insert('1.0', ref['transcript'])

    def add_original_reference(self):
        if not self.original:
            self.message_var.set('O áudio original não está disponível nesta fala.')
            return
        self._add_reference(self.original, {'stable_audio_id': self.info.get('stable_audio_id'),
                                           'event_name': self.info.get('event_name'), 'line_id': self.line_id, 'source': 'original'})

    def import_reference(self):
        source = filedialog.askopenfilename(parent=self.win, title='Referência de voz WAV', filetypes=[('WAV', '*.wav')])
        if source:
            self._add_reference(source, {'source': 'importado'})

    def _add_reference(self, source, provenance):
        try:
            self.store.add_reference(self.character_var.get(), source, self.transcript.get('1.0', 'end-1c'), provenance)
            self.save_character()
            self.refresh_references()
        except (ValueError, OSError) as exc:
            messagebox.showerror('Referência', str(exc), parent=self.win)

    def play_reference(self):
        ref = self.selected_reference()
        if ref:
            self.app._tocar(self.store.resolve_path(ref['path']), 'referência')

    def remove_reference(self):
        ref = self.selected_reference()
        if ref:
            self.store.remove_reference(self.character_var.get().strip(), ref['id'])
            self.refresh_references()

    def effect(self):
        if self.busy:
            self.cancel_event.set()
            self.effect_btn.config(text='Cancelando…')
            return
        path = self.paths.get(self.selected())
        if not path:
            self.message_var.set('Selecione um áudio disponível como fonte do efeito.')
            return
        try:
            params = {'speed': float(self.speed_var.get().replace(',', '.')),
                      'pitch_semitones': float(self.pitch_var.get().replace(',', '.')),
                      'reverb': float(self.reverb_var.get().replace(',', '.'))}
        except ValueError:
            self.message_var.set('Informe valores numéricos válidos.')
            return
        try:
            source_text = self.effect_source_text(self.selected())
        except (ValueError, OSError) as exc:
            self.message_var.set(str(exc))
            return
        source_id = self.selected()
        source_sha = self.candidates[source_id]['sha256']
        destination = self.store.root / 'workspace' / 'studio_runs' / 'effects' / (uuid.uuid4().hex + '.wav')
        self.cancel_event = threading.Event()
        self.busy = True
        self._set_state('Criando efeito…')
        self._sync_review_controls()
        self.effect_btn.config(text='Cancelar efeito')
        self.message_var.set('Criando novo take em segundo plano…')
        def work():
            try:
                analysis = render_effect(path, destination, cancel_event=self.cancel_event, **params)
                self.store.validate_candidate_source(self.line_id, source_id)
                metadata = dict(params, source_candidate_id=source_id, source_sha256=source_sha)
                self.app.ui(lambda: self._effect_done(path, destination, metadata, analysis, source_text))
            except Exception as exc:
                self.app.ui(lambda error=str(exc): self._effect_error(error))
        threading.Thread(target=work, daemon=True).start()

    def _effect_done(self, source, destination, params, analysis, text):
        self.busy = False
        if not self.closed:
            self.effect_btn.config(text='Criar take')
            self._import(destination, 'efeito', {'source': str(source), 'effects': params}, analysis, text)

    def _effect_error(self, error):
        self.busy = False
        if not self.closed:
            self.effect_btn.config(text='Criar take')
            self._sync_review_controls()
            self._set_state('Erro no efeito')
            self.message_var.set(error)

    def close(self):
        self.closed = True
        if self.generation_job:
            self.generation_job.cancel()
        self.cancel_event.set()
        self.app.player.stop()
        self.win.destroy()
