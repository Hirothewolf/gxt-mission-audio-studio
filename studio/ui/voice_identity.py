"""Classic Tk interface for listening to and confirming voice identity groups."""
from __future__ import annotations

import json
import queue
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from studio import config
from studio.ui.dialogs import ProgressoOperacaoDialog


ALL_SCRIPT_SCOPE = 'Somente SCRIPT (todos)'


def _text(value, fallback='—'):
    return str(value) if value not in (None, '') else fallback


def _score_text(item):
    """Show model scores as raw scores; they are not calibrated probabilities."""
    score = item.get('score')
    margin = item.get('margin')
    parts = []
    if isinstance(score, (int, float)):
        parts.append('score %.4f' % score)
    if isinstance(margin, (int, float)):
        parts.append('margem %.4f' % margin)
    return ' · '.join(parts) if parts else 'sem score disponível'


def _status_text(item):
    status = str(item.get('status') or '').upper()
    reason = str(item.get('reason') or '').casefold()
    if status in ('SHORT_CLIP', 'TOO_SHORT', 'UNKNOWN_SHORT_CLIP') or reason == 'too_short':
        return 'Trecho curto demais para sugerir identidade'
    if status in ('UNKNOWN', 'NO_MATCH', 'UNMATCHED'):
        return 'Sem sugestão confiável; precisa de escuta'
    if status in ('SUGGESTED', 'SUGGESTION'):
        return 'Sugestão por voz; revise por escuta'
    if status in ('CONFIRMED', 'MANUAL'):
        return 'Personagem confirmado manualmente'
    if status == 'KNOWN':
        return 'Referência já identificada'
    if status == 'AMBIGUOUS':
        return 'Sugestão ambígua; precisa ouvir'
    if status == 'UNANALYSABLE':
        return 'Áudio sem qualidade para sugerir identidade'
    if status == 'MULTI_SPEAKER':
        return 'Várias vozes declaradas; identifique cada trecho separadamente'
    if status == 'SEED_CONFLICT':
        return 'Identificações conflitantes; isso não prova várias vozes'
    return _text(item.get('status'), 'Sugestão por voz; revise por escuta')


def _group_status_text(group):
    status = str(group.get('status') or '').upper()
    if status == 'SUGGESTED':
        return 'Sugestão por voz; revise por escuta'
    if status == 'UNLABELED':
        return 'Sem identidade; compare por escuta'
    if status in ('CONFIRMED', 'MANUAL'):
        return 'Personagem confirmado'
    return _text(group.get('status'), 'Precisa ouvir')


class VoiceIdentityWindow:
    """Listen to source WAVs grouped by voice and save explicit identity choices."""

    def __init__(self, app):
        self.app = app
        self.project_root = Path(config.get_project_root()).resolve()
        self.gxt_context = app.g
        self.store = self._make_store()
        self.report = None
        self.groups = {}
        self.items = {}
        self.current_group = None
        self.closed = False
        self.busy = False
        self.job = None
        self.progress = None
        self._job_output = queue.Queue()
        self._has_progress_event = False
        self._poll_id = None
        self.win = tk.Toplevel(app.root)
        self.win.title('Identidade de voz — grupos para escuta')
        self.win.geometry('1120x730')
        self.win.minsize(900, 590)
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self.win.bind('<Escape>', lambda _e: self.app.parar_audio())
        self._build()
        self._refresh_runtime()
        self.refresh_report()

    @staticmethod
    def _make_store():
        from studio.audio.voice_identity import VoiceIdentityStore
        return VoiceIdentityStore(config.get_project_root())

    def _context_is_current(self):
        try:
            return (not self.closed and self.app.g is self.gxt_context and
                    Path(config.get_project_root()).resolve() == self.project_root)
        except (OSError, RuntimeError):
            return False

    def _build(self):
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Agrupe as falas sugeridas pela voz e confirme cada personagem depois de ouvir os WAVs originais.',
                  wraplength=1060).pack(anchor='w', pady=(0, 8))

        scope = ttk.LabelFrame(outer, text=' Escopo da análise ', padding=8)
        scope.pack(fill='x', pady=(0, 8))
        self.scope_values = self._scope_values()
        current = self._current_mission()
        default = current if current in self.scope_values else ''
        self.scope_var = tk.StringVar(value=default)
        ttk.Label(scope, text='Missão:').pack(side='left')
        self.scope_box = ttk.Combobox(scope, textvariable=self.scope_var, state='readonly', width=32,
                                      values=self.scope_values)
        self.scope_box.pack(side='left', padx=6)
        self.analyze_btn = ttk.Button(scope, text='Analisar missão', command=self.analyze)
        self.analyze_btn.pack(side='left', padx=(4, 6))
        self.setup_btn = ttk.Button(scope, text='Preparar reconhecimento local', command=self.prepare_runtime)
        self.setup_btn.pack(side='left')
        self.runtime_var = tk.StringVar(value='Verificando reconhecimento local…')
        ttk.Label(scope, textvariable=self.runtime_var, wraplength=500).pack(side='left', padx=(12, 0), fill='x', expand=True)

        body = ttk.Panedwindow(outer, orient='horizontal')
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body, padding=(0, 0, 6, 0))
        right = ttk.Frame(body, padding=(6, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=2)
        ttk.Label(left, text='Grupos de voz').pack(anchor='w', pady=(0, 4))
        self.group_tree = ttk.Treeview(left, columns=('identity', 'status', 'count'), show='headings', height=18,
                                       selectmode='browse')
        for col, title, width in [('identity', 'Sugestão', 150), ('status', 'Estado', 150), ('count', 'Falas', 60)]:
            self.group_tree.heading(col, text=title)
            self.group_tree.column(col, width=width, stretch=(col != 'count'))
        self.group_tree.column('count', width=60, anchor='center')
        self.group_tree.pack(fill='both', expand=True)
        self.group_tree.bind('<<TreeviewSelect>>', lambda _e: self._select_group())

        self.group_title_var = tk.StringVar(value='Selecione um grupo para ouvir as falas.')
        ttk.Label(right, textvariable=self.group_title_var, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 4))
        self.member_tree = ttk.Treeview(right, columns=('audio', 'suggestion', 'score', 'status'),
                                        show='headings', height=15, selectmode='browse')
        for col, title, width in [('audio', 'Áudio original do jogo', 250),
                                  ('suggestion', 'Sugestão', 150),
                                  ('score', 'Score do modelo', 170),
                                  ('status', 'Revisão', 235)]:
            self.member_tree.heading(col, text=title)
            self.member_tree.column(col, width=width, stretch=(col in ('audio', 'status')))
        self.member_tree.pack(fill='both', expand=True)
        member_scroll = ttk.Scrollbar(right, orient='horizontal', command=self.member_tree.xview)
        member_scroll.pack(fill='x')
        self.member_tree.configure(xscrollcommand=member_scroll.set)
        self.member_tree.bind('<Double-1>', lambda _e: self.play_selected())
        controls = ttk.Frame(right)
        controls.pack(fill='x', pady=6)
        ttk.Button(controls, text='Ouvir WAV original', command=self.play_selected).pack(side='left', padx=(0, 5))
        ttk.Button(controls, text='Parar', command=self.app.parar_audio).pack(side='left')

        confirm = ttk.LabelFrame(right, text=' Confirmação manual ', padding=8)
        confirm.pack(fill='x', pady=(4, 0))
        ttk.Label(confirm, text='Personagem:').pack(side='left')
        self.character_var = tk.StringVar()
        self.character_box = ttk.Combobox(confirm, textvariable=self.character_var, width=34)
        self.character_box.pack(side='left', padx=6)
        self.confirm_btn = ttk.Button(confirm, text='Confirmar personagem do grupo', command=self.confirm_group)
        self.confirm_btn.pack(side='left')
        ttk.Label(right, text='A sugestão vem da voz, não da legenda. Escute o áudio antes de confirmar; score e margem não são probabilidades.',
                  wraplength=700).pack(anchor='w', pady=(6, 0))

        self.history_var = tk.StringVar(value='Nenhuma análise salva ainda.')
        ttk.Label(outer, textvariable=self.history_var, wraplength=1050).pack(anchor='w', pady=(8, 0))

    def _current_mission(self):
        try:
            mission = self.app.tabela_atual()
            if mission and mission != '*' and self.app.g and mission in self.app.g.t:
                return mission
        except (AttributeError, KeyError, TypeError):
            pass
        return None

    def _scope_values(self):
        try:
            missions = list(self.app.g.order) if self.app.g else []
        except (AttributeError, TypeError):
            missions = list(self.app.g.t) if self.app.g else []
        return missions + [ALL_SCRIPT_SCOPE]

    def _refresh_runtime(self):
        try:
            from studio.audio.voice_runtime import runtime_status
            status = runtime_status(self.project_root)
            self.runtime_status = status
            ready = status.get('ready', False) if isinstance(status, dict) else bool(getattr(status, 'ready', False))
            if isinstance(status, dict):
                detail = status.get('reason') or ('Modelo e runtime locais disponíveis.' if ready else 'Runtime local indisponível.')
                can_setup = bool(status.get('can_setup'))
            else:
                detail = getattr(status, 'reason', 'Modelo e runtime locais disponíveis.' if ready else 'Runtime local indisponível.')
                can_setup = bool(getattr(status, 'can_setup', False))
            self.runtime_var.set('Reconhecimento local pronto. ' + str(detail) if ready else
                                 'Reconhecimento local indisponível. ' + str(detail))
            self.analyze_btn.configure(state='normal' if ready else 'disabled')
            if ready:
                self.setup_btn.pack_forget()
            else:
                if not self.setup_btn.winfo_manager():
                    self.setup_btn.pack(side='left')
                self.setup_btn.configure(state='normal' if can_setup else 'disabled')
        except Exception as exc:
            self.runtime_status = {'ready': False, 'reason': str(exc)}
            self.runtime_var.set('Reconhecimento local indisponível: %s' % exc)
            self.analyze_btn.configure(state='disabled')
            if not self.setup_btn.winfo_manager():
                self.setup_btn.pack(side='left')
            self.setup_btn.configure(state='disabled')

    def refresh_report(self):
        if not self._context_is_current():
            self.history_var.set('O GXT ou a pasta do projeto mudou. Feche esta janela e abra novamente para atualizar o contexto.')
            return
        try:
            report = self.store.latest()
        except Exception as exc:
            self.history_var.set('Não foi possível ler o histórico de identidade: %s' % exc)
            return
        self.report = report
        self.groups = {}
        self.items = {}
        if not report:
            self._render_groups()
            self.history_var.set('Nenhuma análise salva. Abrir esta janela não inicia reconhecimento nem baixa modelos.')
            return
        self.items = {str(item.get('audio_id') or item.get('id')): item for item in report.get('items', [])}
        self.groups = {str(group.get('id')): group for group in report.get('groups', [])}
        self._render_groups()
        self.history_var.set('Análise %s · %d grupos · %d áudios' %
                             (_text(report.get('report_id') or report.get('id')), len(self.groups), len(self.items)))

    def _render_groups(self):
        self.group_tree.delete(*self.group_tree.get_children())
        self.member_tree.delete(*self.member_tree.get_children())
        if not self.report:
            self.group_title_var.set('Nenhum grupo disponível. Execute uma análise local quando o runtime estiver pronto.')
            return
        for gid, group in self.groups.items():
            identity = _text(group.get('confirmed_identity') or group.get('identity'), 'Identidade não sugerida')
            status = _group_status_text(group)
            count = group.get('count', len(group.get('item_ids', [])))
            self.group_tree.insert('', 'end', iid=gid, values=(identity, status, count))
        if self.groups:
            self.group_tree.selection_set(next(iter(self.groups)))
            self._select_group()
        else:
            self.group_title_var.set('A análise não encontrou grupos para escuta.')

    def _select_group(self):
        selection = self.group_tree.selection()
        if not selection:
            return
        gid = selection[0]
        group = self.groups.get(gid)
        if not group:
            return
        self.current_group = gid
        self.member_tree.delete(*self.member_tree.get_children())
        ids = group.get('item_ids') or []
        for index, aid in enumerate(ids):
            item = self.items.get(str(aid))
            if not item:
                continue
            iid = str(item.get('audio_id') or item.get('id') or ('member-%d' % index))
            name = Path(str(item.get('path') or '')).name or _text(item.get('audio_id'))
            status = ('Personagem confirmado manualmente' if group.get('status') == 'CONFIRMED'
                      else _status_text(item))
            self.member_tree.insert('', 'end', iid=iid, values=(name,
                _text(group.get('confirmed_identity') or item.get('suggested_identity')),
                _score_text(item), status))
        self.group_title_var.set('%s · %s falas · %s' %
                                 (_text(group.get('confirmed_identity') or group.get('identity'), 'Grupo sem sugestão'),
                                  len(ids), _group_status_text(group)))
        self.character_var.set(_text(group.get('confirmed_identity') or group.get('identity'), ''))
        self.confirm_btn.configure(state='disabled' if group.get('status') == 'CONFIRMED' else 'normal')
        names = {str(item.get(key)).strip() for item in self.items.values()
                 for key in ('known_identity', 'suggested_identity') if item.get(key)}
        names.update(str(row.get('identity')).strip() for row in self.groups.values() if row.get('identity'))
        try:
            names.update(self.store.characters())
        except (AttributeError, TypeError):
            pass
        self.character_box.configure(values=sorted(names, key=str.casefold))
        children = self.member_tree.get_children()
        if children:
            self.member_tree.selection_set(children[0])

    def play_selected(self):
        selection = self.member_tree.selection()
        if not selection:
            return
        item = self.items.get(selection[0])
        if not item:
            return
        path = Path(item.get('path', ''))
        if not path.is_absolute():
            path = self.project_root / path
        if not path.is_file():
            messagebox.showerror('Áudio original', 'O WAV original não está disponível:\n%s' % path, parent=self.win)
            return
        self.app._tocar(path, 'original do jogo')

    def confirm_group(self):
        if not self._context_is_current():
            messagebox.showerror('Contexto alterado', 'O GXT ou projeto mudou. Abra novamente antes de confirmar.', parent=self.win)
            return
        if not self.report or not self.current_group:
            return
        character = self.character_var.get().strip()
        if not character:
            messagebox.showwarning('Personagem', 'Informe o nome do personagem que você reconheceu.', parent=self.win)
            return
        try:
            self.store.assign_group(self.report.get('report_id') or self.report.get('id'),
                                    self.current_group, character)
        except Exception as exc:
            messagebox.showerror('Confirmação não salva', str(exc), parent=self.win)
            self.refresh_report()
            return
        self.refresh_report()
        if self.group_tree.exists(self.current_group):
            group = self.groups.get(self.current_group, {})
            self.group_tree.item(self.current_group, values=(character, 'Personagem confirmado', group.get('count', '')))
            self.group_tree.selection_set(self.current_group)
            self.group_title_var.set('%s · personagem confirmado' % character)
        self.history_var.set('Personagem confirmado: %s. O original e a revisão de falas continuam preservados.' % character)
        refresh = getattr(self.app, 'refresh_voice_identities', None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:
                self.history_var.set('Personagem confirmado: %s. A lista principal não atualizou: %s' % (character, exc))

    def _selected_tables(self):
        choice = self.scope_var.get()
        if not choice:
            raise ValueError('Escolha uma missão conhecida antes de analisar. A opção de todos os SCRIPT é optativa.')
        if choice == ALL_SCRIPT_SCOPE:
            return None
        if choice not in self.scope_values or choice == '*':
            raise ValueError('Escolha uma missão válida da lista.')
        return [choice]

    def analyze(self):
        if self.busy:
            return
        if not self._context_is_current():
            messagebox.showerror('Contexto alterado', 'O GXT ou a pasta do projeto mudou. Abra novamente a janela.', parent=self.win)
            return
        ready = self.runtime_status.get('ready', False) if isinstance(self.runtime_status, dict) else bool(getattr(self.runtime_status, 'ready', False))
        if not ready:
            messagebox.showinfo('Reconhecimento local', 'Prepare o runtime local antes de analisar. Nenhuma análise será iniciada agora.', parent=self.win)
            return
        try:
            tables = self._selected_tables()
            from studio.audio.voice_runtime import build_analysis_command
            command = build_analysis_command(self.project_root, tables=tables)
            self._start_job(command, 'Reconhecimento de vozes')
        except Exception as exc:
            messagebox.showerror('Análise de vozes', str(exc), parent=self.win)

    def prepare_runtime(self):
        if self.busy:
            return
        try:
            from studio.audio.voice_runtime import build_setup_command
            command = build_setup_command(self.project_root)
            self._start_job(command, 'Preparando reconhecimento local')
        except Exception as exc:
            messagebox.showerror('Reconhecimento local', str(exc), parent=self.win)

    def _start_job(self, command, title):
        self.busy = True
        self.job_kind = title
        self._job_output = queue.Queue()
        self._has_progress_event = False
        self._last_job_line = ''
        self.progress = ProgressoOperacaoDialog(self.win, title)
        self.progress.atualizar(0, 0, 'Aguardando o processo local…')
        self.job = self.app.jobs.submit(command, cwd=self.project_root, on_line=self._queue_job_line,
                                        timeout=60 * 60)
        self._poll_id = self.win.after(150, self._poll_job)

    def _queue_job_line(self, line):
        """ProcessJob callback: transfer output only; all Tk work stays in _poll_job."""
        self._job_output.put(str(line))

    def _drain_job_output(self):
        lines = []
        while True:
            try:
                lines.append(self._job_output.get_nowait())
            except queue.Empty:
                break
        for line in lines:
            self._last_job_line = line
            self.progress._registrar_log(line)
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict) or event.get('event') != 'progress':
                continue
            self._has_progress_event = True
            self.progress.lbl_status.config(text=event.get('message') or 'Analisando áudios locais…')
            done, total = event.get('done'), event.get('total')
            if (isinstance(done, (int, float)) and not isinstance(done, bool) and
                    isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0):
                self.progress.pb.stop()
                self.progress._indeterminate = False
                self.progress.pb.configure(mode='determinate', maximum=100,
                                           value=min(100, max(0, done * 100 / total)))
                self.progress.lbl_detalhes.config(text='%s / %s áudios' % (done, total))

    def _poll_job(self):
        self._poll_id = None
        if self.closed or not self.job or not self.progress:
            return
        if self.progress.cancel_event.is_set():
            self.job.cancel()
        self._drain_job_output()
        if not self.job.done:
            self._poll_id = self.win.after(150, self._poll_job)
            return
        self._drain_job_output()
        result = self.job.wait(0)
        progress = self.progress
        self.progress = None
        self.job = None
        self.busy = False
        progress.fechar()
        if not self._context_is_current():
            self.history_var.set('A operação terminou, mas o GXT ou projeto mudou; o resultado antigo não foi exibido.')
            return
        if result.cancelled:
            self.history_var.set('Operação cancelada. Nenhum resultado novo foi carregado.')
        elif result.returncode != 0:
            detail = result.error or (result.logs[-1] if result.logs else self._last_job_line or 'O processo terminou com erro.')
            messagebox.showerror('Reconhecimento local', detail, parent=self.win)
        elif self.job_kind.startswith('Preparando'):
            self._refresh_runtime()
            self.history_var.set('Preparação local finalizada. Confira o estado acima; a análise não começou automaticamente.')
        else:
            self.refresh_report()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._poll_id:
            try:
                self.win.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        if self.job:
            self.job.cancel()
        if self.progress:
            self.progress.fechar()
            self.progress = None
        self.app.parar_audio()
        self.win.destroy()
