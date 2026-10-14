"""Classic view of source-derived classification and index regeneration."""
import json
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from studio import config
from studio.script.native_index import load_current, merge_records
from studio.ui.dialogs import ProgressoOperacaoDialog

LABELS = {'unknown': 'Desconhecida', 'script': 'SCRIPT', 'cutscene': 'Cutscene',
          'interface_usage': 'Uso de interface (áudio não comprovado)'}
FILTER_CATEGORIES = {'Somente desconhecidas': 'unknown', 'SCRIPT': 'script',
                     'Cutscenes': 'cutscene', 'Uso de interface': 'interface_usage'}
SEARCH_DEBOUNCE_MS = 250
RENDER_CHUNK_SIZE = 80
LOG_DRAIN_LIMIT = 100


def filter_classifications(report, category, search):
    """Filter report rows; called on a worker because indexes can be large."""
    needle = str(search or '').casefold().strip()
    output = []
    for row in report.get('classifications', ()):
        if category and row.get('category') != category:
            continue
        if needle:
            haystack = ' '.join(row.get('names') or ()) + row.get('table', '') + row.get('source_text', '')
            if needle not in haystack.casefold():
                continue
        output.append(row)
    return output


def index_report(report):
    """Build detail and merged-audio lookups once per report, off the UI thread."""
    rows = {row['gxt_stable_id']: row for row in report.get('classifications', ())}
    records = {}
    for record in report.get('records', ()):
        records.setdefault(record['gxt_stable_id'], []).append(record)
    merged = {record['gxt_stable_id']: record for record in merge_records([], report)}
    return rows, records, merged


class NativeSourcesWindow:
    def __init__(self, app):
        self.app = app
        self.root = Path(config.get_project_root()).resolve()
        self.game = config.CFG.get('game_dir')
        self.game = str(Path(self.game).resolve()) if self.game else None
        self.closed = False
        self.job = None
        self.job_kind = 'index'
        self.output = queue.Queue()
        self.ui_queue = queue.Queue()
        self.progress = None
        self.report = None
        self.rows = {}
        self.rows_by_id = {}
        self.records_by_id = {}
        self.merged_records_by_id = {}
        self._load_generation = 0
        self._filter_generation = 0
        self._listen_generation = 0
        self._render_after = None
        self._search_after = None
        self._render_rows = []
        self._render_offset = 0
        self._render_started = 0.0
        self._job_poll_after = None
        self._ui_poll_after = None
        self.win = tk.Toplevel(app.root)
        self.win.title('Fontes do jogo — SCRIPT, cutscenes e legendas')
        self.win.geometry('1120x750')
        self.win.minsize(900, 620)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Vínculos lidos do jogo base', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='SCM + AudioEvents + GXT para SCRIPT; cuts.img + TrakLkup para cenas. '
                  'Formatação/colorização não determina ausência de áudio.', wraplength=1060).pack(anchor='w', pady=6)
        controls = ttk.Frame(outer)
        controls.pack(fill='x', pady=6)
        ttk.Button(controls, text='Gerar índice do jogo base', command=self.build).pack(side='left')
        ttk.Button(controls, text='Atualizar relatório', command=self.refresh).pack(side='left', padx=6)
        studio_button = getattr(app, 'abrir_estudio_cutscenes', None)
        if callable(studio_button):
            ttk.Button(controls, text='Abrir estúdio de cutscenes', command=studio_button).pack(side='left', padx=(0, 6))
        self.filter = tk.StringVar(value='Somente desconhecidas')
        combo = ttk.Combobox(controls, textvariable=self.filter, state='readonly', width=30,
                             values=['Somente desconhecidas', 'SCRIPT', 'Cutscenes', 'Uso de interface', 'Todas'])
        combo.pack(side='left', padx=6)
        combo.bind('<<ComboboxSelected>>', lambda e: self._schedule_filter())
        self.search = tk.StringVar()
        ttk.Entry(controls, textvariable=self.search, width=25).pack(side='left')
        self.search.trace_add('write', lambda *args: self._schedule_filter())
        self.status = tk.StringVar(value='Preparando relatório…')
        ttk.Label(outer, textvariable=self.status, wraplength=1060).pack(anchor='w', pady=5)
        self.tree = ttk.Treeview(outer, columns=('table', 'key', 'category', 'text'), show='headings', height=16)
        for column, label, width in [('table', 'Tabela', 100), ('key', 'Chave', 160), ('category', 'Fonte/uso', 220), ('text', 'Legenda original', 590)]:
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=70)
        self.tree.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(outer, orient='horizontal', command=self.tree.xview)
        scrollbar.pack(fill='x')
        self.tree.configure(xscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self.details)
        self.text = tk.Text(outer, height=7, wrap='word', state='disabled')
        self.text.pack(fill='x', pady=(8, 0))
        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=6)
        ttk.Button(actions, text='Extrair somente CUTSCENE original', command=self.extract).pack(side='left')
        ttk.Button(actions, text='Ouvir intervalo da legenda selecionada', command=self.listen).pack(side='left', padx=8)
        self._ui_poll_after = self.win.after(50, self._drain_ui)
        self.refresh()

    def _context_matches(self):
        try:
            current_root = Path(config.get_project_root()).resolve()
            game = config.CFG.get('game_dir')
            current_game = str(Path(game).resolve()) if game else None
            return current_root == self.root and current_game == self.game
        except (OSError, TypeError, ValueError):
            return False

    def _schedule_filter(self):
        if self.closed or not self.report:
            return
        self._filter_generation += 1
        generation = self._filter_generation
        if self._search_after:
            try:
                self.win.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self.status.set('Pesquisando relatório…')
        self._search_after = self.win.after(SEARCH_DEBOUNCE_MS,
                                            lambda: self._start_filter(generation))

    def _start_filter(self, generation):
        self._search_after = None
        if self.closed or generation != self._filter_generation or not self.report:
            return
        report = self.report
        category = FILTER_CATEGORIES.get(self.filter.get())
        search = self.search.get()
        started = time.monotonic()

        def work():
            try:
                rows = filter_classifications(report, category, search)
                self.ui_queue.put(('filter', generation, rows, time.monotonic() - started, None))
            except Exception as exc:
                self.ui_queue.put(('filter', generation, None, time.monotonic() - started, str(exc)))

        threading.Thread(target=work, name='native-source-search', daemon=True).start()

    def _begin_render(self, generation, rows, elapsed):
        if generation != self._filter_generation:
            return
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self._render_rows = rows[:500]
        self._render_offset = 0
        self._render_started = time.monotonic()
        self._render_count = len(rows)
        self._filter_elapsed = elapsed
        self._render_generation = generation
        self._render_chunk()

    def _render_chunk(self):
        if self.closed or self._render_generation != self._filter_generation:
            return
        stop = min(self._render_offset + RENDER_CHUNK_SIZE, len(self._render_rows))
        for row in self._render_rows[self._render_offset:stop]:
            gid = row['gxt_stable_id']
            self.rows[gid] = row
            self.tree.insert('', 'end', iid=gid, values=(row['table'], ', '.join(row.get('names') or ()) or gid.split(':')[1],
                                                        LABELS.get(row['category'], 'Desconhecida'), row['source_text']))
        self._render_offset = stop
        if stop < len(self._render_rows):
            self.status.set('Preparando resultados: %d/%d entradas…' % (stop, len(self._render_rows)))
            self._render_after = self.win.after(1, self._render_chunk)
            return
        report = self.report
        scm = report.get('scm', {}) if report else {}
        elapsed = time.monotonic() - self._render_started + self._filter_elapsed
        self.status.set('%d entradas no filtro · exibindo até 500 · %d ms. '
                        'Leitura SCM: %d/%d segmentos completos. Uso de interface não prova ausência de voz.' %
                        (self._render_count, round(elapsed * 1000), scm.get('complete_segments', 0), scm.get('segments', 0)))
        self._render_after = None

    def _drain_ui(self):
        if self.closed:
            return
        for _ in range(LOG_DRAIN_LIMIT):
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == 'loaded':
                _, generation, report, indexes, elapsed, error = item
                if generation != self._load_generation:
                    continue
                if not self._context_matches():
                    self.status.set('O projeto ou jogo selecionado mudou; atualize o relatório para esta instalação.')
                    continue
                if error:
                    self.status.set('Índice indisponível: %s' % error)
                    continue
                self.report = report
                if indexes:
                    self.rows_by_id, self.records_by_id, self.merged_records_by_id = indexes
                if not report:
                    self.status.set('Nenhum índice atual para esta instalação. Gere o índice; os arquivos do jogo serão apenas lidos.')
                    self.tree.delete(*self.tree.get_children())
                    continue
                self.status.set('Relatório preparado em %.2fs · filtrando…' % elapsed)
                self._schedule_filter()
            elif kind == 'filter':
                _, generation, rows, elapsed, error = item
                if generation != self._filter_generation:
                    continue
                if error:
                    self.status.set('Falha ao pesquisar: %s' % error)
                else:
                    self._begin_render(generation, rows, elapsed)
            elif kind == 'listen':
                _, generation, gid, path, error = item
                if generation != self._listen_generation or not self._context_matches():
                    continue
                if error:
                    messagebox.showerror('Intervalo indisponível', error, parent=self.win)
                elif self.tree.selection() != (gid,):
                    self.status.set('A seleção mudou enquanto o intervalo era preparado; nenhum áudio foi aberto.')
                elif path:
                    self.app._tocar(path, 'CUTSCENE original — intervalo da legenda')
                else:
                    messagebox.showinfo('Intervalo indisponível',
                                        'Extraia CUTSCENE original e confirme que o FFmpeg está disponível para preparar este intervalo.',
                                        parent=self.win)
        self._ui_poll_after = self.win.after(50, self._drain_ui)

    def extract(self):
        if self.job or self.app.jobs.busy or not self.game:
            return
        executable = config.APP_DIR / 'tools/SAAT/saat_stream.exe'
        if not executable.is_file():
            messagebox.showerror('SAAT', 'Exportador de streams indisponível.', parent=self.win)
            return
        self.job_kind = 'extract'
        self.progress = ProgressoOperacaoDialog(self.win, 'Extraindo CUTSCENE para o cache do projeto')
        command = [sys.executable, '-u', str(config.APP_DIR / 'tools/extract_native_cutscene.py'),
                   '--game', self.game, '--project', str(self.root), '--saat', str(executable)]
        self.job = self.app.jobs.submit(command, cwd=config.APP_DIR, on_line=self.output.put, timeout=600)
        self._job_poll_after = self.win.after(100, self.poll)

    def listen(self):
        selected = self.tree.selection()
        if not selected or not self.report:
            return
        gid = selected[0]
        record = self.merged_records_by_id.get(gid)
        if not record or record.get('pak') != 'CUTSCENE':
            messagebox.showinfo('Intervalo indisponível', 'Selecione uma legenda vinculada a CUTSCENE.', parent=self.win)
            return
        if record.get('cutscene_ambiguous'):
            messagebox.showinfo('Mais de uma cena', 'Esta chave tem vários intervalos possíveis. Os vínculos permanecem explícitos nos detalhes.', parent=self.win)
            return
        self._listen_generation += 1
        generation = self._listen_generation
        report, root = self.report, self.root
        self.status.set('Preparando intervalo e verificando a faixa…')

        def work():
            try:
                from studio.audio.cutscene import resolve_window
                path = resolve_window(record, root, report)
                self.ui_queue.put(('listen', generation, gid, path, None))
            except Exception as exc:
                self.ui_queue.put(('listen', generation, gid, None, str(exc)))

        threading.Thread(target=work, name='native-cutscene-window', daemon=True).start()

    def refresh(self):
        current_root = Path(config.get_project_root()).resolve()
        game = config.CFG.get('game_dir')
        current_game = str(Path(game).resolve()) if game else None
        if current_root != self.root or current_game != self.game:
            self.root, self.game = current_root, current_game
            self.report = None
            self.rows = {}
            self.rows_by_id = {}
            self.records_by_id = {}
            self.merged_records_by_id = {}
            self.tree.delete(*self.tree.get_children())
        self._load_generation += 1
        generation = self._load_generation
        self._filter_generation += 1
        # Stop searches/details from continuing to use a report while a newer
        # installation snapshot is being validated.
        self.report = None
        self.rows = {}
        self.rows_by_id = {}
        self.records_by_id = {}
        self.merged_records_by_id = {}
        self.tree.delete(*self.tree.get_children())
        if self._search_after:
            try:
                self.win.after_cancel(self._search_after)
            except tk.TclError:
                pass
            self._search_after = None
        root, game = self.root, self.game
        started = time.monotonic()
        self.status.set('Preparando relatório do jogo base…')

        def work():
            try:
                report = load_current(root, game)
                indexes = index_report(report) if report else None
                self.ui_queue.put(('loaded', generation, report, indexes,
                                   time.monotonic() - started, None))
            except Exception as exc:
                self.ui_queue.put(('loaded', generation, None, None,
                                   time.monotonic() - started, str(exc)))

        threading.Thread(target=work, name='native-source-load', daemon=True).start()

    def details(self, event=None):
        selected = self.tree.selection()
        row = self.rows.get(selected[0]) if selected else None
        if not row:
            return
        gid = row['gxt_stable_id']
        records = self.records_by_id.get(gid, ())
        lines = [gid, LABELS.get(row['category'], 'Desconhecida'), row['source_text']]
        for record in records:
            lines.append('%s · %s' % (record['stable_audio_id'], record['evidence_method']))
            if record['pak'] == 'CUTSCENE':
                stream = record.get('stream') or {}
                lines.append('%s: início %.3fs, duração da legenda %.3fs. Faixa: %s. '
                             'Este intervalo é de legenda; não é segmentação fonética.' %
                             (record['cut_metadata']['file'], record['caption_start_ms']/1000,
                              record['caption_duration_ms']/1000, stream.get('track', 'não comprovada')))
        for use in row.get('uses', ())[:20]:
            lines.append('SCM @%d · %s · %s' % (use['offset'], use['segment'], use['command']))
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', '\n'.join(lines))
        self.text.configure(state='disabled')

    def build(self):
        if self.job:
            return
        if not self.game:
            messagebox.showerror('Jogo base', 'Configure primeiro a pasta do GTA San Andreas.', parent=self.win)
            return
        if self.app.jobs.busy:
            messagebox.showinfo('Tarefa em andamento', 'Aguarde ou cancele a tarefa atual antes de gerar o índice.', parent=self.win)
            return
        self.job_kind = 'index'
        self.progress = ProgressoOperacaoDialog(self.win, 'Indexando arquivos do jogo base')
        command = [sys.executable, '-u', str(config.APP_DIR / 'tools/build_native_index.py'),
                   '--game', self.game, '--project', str(self.root)]
        self.job = self.app.jobs.submit(command, cwd=config.APP_DIR, on_line=self.output.put, timeout=600)
        self._job_poll_after = self.win.after(100, self.poll)

    def poll(self):
        if self.closed:
            return
        if self.progress.cancel_event.is_set():
            self.job.cancel()
        for _ in range(LOG_DRAIN_LIMIT):
            try:
                line = self.output.get_nowait()
            except queue.Empty:
                break
            self.progress._registrar_log(line)
            try:
                event = json.loads(line)
                if event.get('event') == 'progress':
                    self.progress.atualizar(event['done'], event['total'], event['message'])
                    self.progress.lbl_detalhes.configure(text='Etapa %d de %d' % (event['done'], event['total']))
            except (ValueError, TypeError, KeyError):
                pass
        if not self.job.done:
            self._job_poll_after = self.win.after(100, self.poll)
            return
        result = self.job.wait(0)
        completed_kind = self.job_kind
        self.progress.fechar()
        self.job = self.progress = None
        self._job_poll_after = None
        if result.returncode or result.cancelled or result.error:
            message = ('Extração cancelada ou falhou. O cache válido anterior foi preservado.'
                       if completed_kind == 'extract' else
                       'Indexação cancelada ou falhou. O índice concluído anterior foi preservado.')
            if result.error:
                message += ' ' + result.error
            self.status.set(message)
            messagebox.showerror('Índice não concluído', '\n'.join(result.logs[-6:]), parent=self.win)
            return
        if self.job_kind == 'extract':
            self.status.set('CUTSCENE original extraído no cache. Selecione uma legenda de cena para ouvir seu intervalo.')
            return
        if not self._context_matches():
            self.status.set('O projeto ou jogo selecionado mudou; o índice foi atualizado no projeto original.')
            return
        from studio.script.mapping import recarregar_script_cache
        self.app.script_db = recarregar_script_cache()
        self.app.original_audio_cache.clear()
        self.app.preencher_tabelas()
        self.app.atualizar_linhas()
        self.refresh()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._load_generation += 1
        self._filter_generation += 1
        self._listen_generation += 1
        for identifier in (self._search_after, self._render_after, self._job_poll_after, self._ui_poll_after):
            if identifier:
                try:
                    self.win.after_cancel(identifier)
                except tk.TclError:
                    pass
        if self.job:
            self.job.cancel()
        if self.progress:
            self.progress.fechar()
        self.win.destroy()
