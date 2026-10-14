"""Compact UI for confirming and installing existing AI takes in batches."""
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from studio.core.utils import json_atomico
from studio.gxt.codec import decode_text_c
from studio.script.mapping import consultar_script_info
from studio.projects.bulk_audio import collect_ai_rows, approve_rows, install_rows
from studio.audio.wav import wav_duracao
from studio.audio.source_routing import is_stream_record


STATE_LABELS = {'APPROVED': 'Aprovado', 'PENDING': 'Confirmar', 'LEGACY': 'AI antigo · vínculo manual',
                'STALE': 'Texto mudou', 'REJECTED': 'Recusado'}


class BulkAudioReviewWindow:
    def __init__(self, app):
        self.app, self.store, self.gxt = app, app.review_store, app.g
        self.busy = False
        self.rows = []
        self.win = tk.Toplevel(app.root)
        self.win.title('Áudios AI — confirmar e aplicar em lote')
        self.win.geometry('1060x610')
        self.win.minsize(820, 470)
        self.win.transient(app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(self.win, padding=10); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Áudios AI já existentes', font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Confirmar guarda a escolha no projeto. Aplicar grava os aprovados no mod ativo, com backup.',
                  wraplength=980).pack(anchor='w', pady=(2, 8))
        top = ttk.Frame(frame); top.pack(fill='x')
        ttk.Label(top, text='Mostrar:').pack(side='left')
        self.scope = tk.StringVar(value='Lista atual (missão/filtros)')
        self.scope_box = ttk.Combobox(top, textvariable=self.scope, state='readonly', width=32,
                                    values=['Lista atual (missão/filtros)', 'Todo o GXT'])
        self.scope_box.pack(side='left', padx=5)
        self.scope_box.bind('<<ComboboxSelected>>', lambda event: self.scan())
        self.reload_btn = ttk.Button(top, text='Atualizar lista', command=self.scan)
        self.reload_btn.pack(side='left')
        self.counter = tk.StringVar(value='Carregando…')
        ttk.Label(top, textvariable=self.counter).pack(side='right')
        table = ttk.Frame(frame); table.pack(fill='both', expand=True, pady=8)
        columns = ('label', 'text', 'take', 'duration', 'route', 'state')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='extended')
        for col, title, width in zip(columns, ['Fala', 'Texto atual', 'Take', 'Duração', 'Destino', 'Situação'],
                                     [150, 330, 110, 70, 115, 170]):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=55, stretch=col in ('text', 'state'))
        y = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        x = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky='nsew'); y.grid(row=0, column=1, sticky='ns')
        x.grid(row=1, column=0, sticky='ew'); table.rowconfigure(0, weight=1); table.columnconfigure(0, weight=1)
        self.tree.bind('<Double-1>', lambda event: self.review_selected())
        self.allow_legacy = tk.BooleanVar(value=False)
        self.override_duration = tk.BooleanVar(value=False)
        self.legacy_check = ttk.Checkbutton(frame, variable=self.allow_legacy,
            text='Incluir AI antigos sem registro: confirmo manualmente que correspondem ao texto atual')
        self.legacy_check.pack(anchor='w')
        self.duration_check = ttk.Checkbutton(frame, variable=self.override_duration,
            text='Aceitar também diferenças de duração (sem cortar nem acelerar o WAV)')
        self.duration_check.pack(anchor='w', pady=(2, 7))
        actions = ttk.Frame(frame); actions.pack(fill='x')
        self.buttons = []
        for text, command in [('Ouvir selecionado', self.listen), ('Revisar selecionado', self.review_selected),
                              ('Confirmar selecionados', lambda: self.confirm(False)),
                              ('Confirmar todos da lista', lambda: self.confirm(True)),
                              ('Aplicar aprovados ao mod…', self.install)]:
            button = ttk.Button(actions, text=text, command=command)
            button.pack(side='left', padx=(0, 5)); self.buttons.append(button)
        self.status = tk.StringVar(value='Pronto')
        ttk.Label(frame, textvariable=self.status, wraplength=990).pack(fill='x', pady=(8, 0))
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x', pady=(5, 0))
        self.scan()

    def current_context(self):
        return self.app.g is self.gxt and self.app.review_store is self.store

    def set_busy(self, busy, text):
        self.busy = busy; self.status.set(text)
        for button in self.buttons + [self.reload_btn, self.legacy_check, self.duration_check]:
            button.configure(state='disabled' if busy else 'normal')
        self.scope_box.configure(state='disabled' if busy else 'readonly')
        if busy: self.progress.start(12)
        else: self.progress.stop()

    def close(self):
        if self.busy:
            messagebox.showinfo('Operação em andamento', 'Aguarde o término da operação antes de fechar esta janela.', parent=self.win)
        else:
            self.win.destroy()

    def scan(self):
        if self.busy: return
        if not self.current_context():
            messagebox.showinfo('Projeto mudou', 'Feche esta janela e abra Áudios AI novamente para usar o projeto atual.', parent=self.win)
            return
        if self.scope.get() == 'Todo o GXT':
            pairs = [(tab, key) for tab in self.gxt.order for key in self.gxt.t[tab]['cur']]
        else:
            pairs = []
            for iid in self.app.tv.get_children():
                tab, separator, key = iid.partition('|')
                if separator:
                    pairs.append((tab, int(key, 16)))
        pairs = iter(pairs); contexts = []
        self.set_busy(True, 'Localizando áudios AI do projeto…')

        def chunk():
            if not self.win.winfo_exists(): return
            if not self.current_context():
                self.set_busy(False, 'Projeto mudou; reabra a janela.'); return
            for _ in range(100):
                try: tab, key = next(pairs)
                except StopIteration:
                    self.work(lambda: collect_ai_rows(self.store, contexts), self.render,
                              'Conferindo takes e vínculos…', already_busy=True)
                    return
                text = decode_text_c(self.gxt.t[tab]['cur'][key])
                info = consultar_script_info(self.app.script_db, tab, key) if self.app.script_db else None
                event = info.get('event_name') if info else None
                path = self.app.achar_audio_dublado(tab, key, event)
                line_id = '%s|%08x' % (tab, key)
                if not path and line_id not in self.store.data['lines']: continue
                contexts.append(dict(line_id=line_id, tab=tab, key=key, text=text,
                    label='%s · %s' % (tab, event or '%08X' % key), legacy_path=path, info=info,
                    ref=self.app.jogo.ref_para(info) if self.app.jogo and info else None))
            self.win.after(1, chunk)
        self.win.after(1, chunk)

    def render(self, rows):
        self.rows = rows
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            duration = wav_duracao(row['path']) if row.get('path') else None
            ref = row.get('ref')
            self.tree.insert('', 'end', iid=row['line_id'], values=(row['label'], row['text'],
                Path(row['path']).name if row.get('path') else '—', '%.2fs' % duration if duration else '—',
                '%s B%d/S%d' % tuple(ref) if ref else 'Montagem de faixa', STATE_LABELS[row['state']]))
        counts = Counter(r['state'] for r in rows)
        self.counter.set('%d áudios · %d aprovados · %d a confirmar · %d antigos' %
                         (len(rows), counts['APPROVED'], counts['PENDING'], counts['LEGACY']))
        self.status.set('Lista pronta. “Texto mudou” e “Recusado” serão preservados. Duplo clique abre a revisão individual.')

    def work(self, operation, callback, message, already_busy=False):
        if self.busy and not already_busy: return
        self.set_busy(True, message)
        def worker():
            try: result, error = operation(), None
            except Exception as exc: result, error = None, str(exc) or type(exc).__name__
            def finish():
                if not self.win.winfo_exists(): return
                self.set_busy(False, 'Operação concluída.' if not error else 'A operação não foi concluída.')
                if error:
                    messagebox.showerror('Áudios AI', error, parent=self.win)
                else: callback(result)
            self.app.ui(finish)
        threading.Thread(target=worker, daemon=True).start()

    def selected_rows(self):
        selected = set(self.tree.selection())
        return [r for r in self.rows if r['line_id'] in selected]

    def listen(self):
        rows = self.selected_rows()
        if rows and rows[0].get('path'):
            self.app._tocar(rows[0]['path'], 'AI em revisão de lote')

    def review_selected(self):
        if self.busy or not self.current_context(): return
        if not self.app.commit(): return
        rows = self.selected_rows()
        if rows:
            row = rows[0]; self.app.sel = (row['tab'], row['key'])
            self.app._carregar_linha(row['tab'], row['key'])
            self.app.abrir_revisao_audio()

    def validate_current(self, rows):
        if not self.current_context(): raise ValueError('Projeto ou GXT mudou. Reabra esta janela.')
        if not self.app.commit(): raise ValueError('Não foi possível aplicar o texto em edição. Confira a fala aberta.')
        changed = [r for r in rows if decode_text_c(self.gxt.t[r['tab']]['cur'][r['key']]) != r['text']]
        if changed: raise ValueError('Há textos alterados desde a listagem. Atualize a lista antes de confirmar ou aplicar.')

    def on_progress(self, done, total, label):
        def show():
            if self.win.winfo_exists() and self.busy:
                self.status.set('%d/%d · %s' % (done, total, label))
        self.app.ui(show)

    def record_result(self, kind, rows, result):
        dest = self.store.directory / 'batches' / ('%s_%s_%s.json' %
               (datetime.now().strftime('%Y%m%d_%H%M%S'), kind, uuid.uuid4().hex[:8]))
        json_atomico(dest, dict(operation=kind, line_ids=[r['line_id'] for r in rows], result=result))
        return dest

    def done(self, kind, rows, result):
        try: report = self.record_result(kind, rows, result)
        except OSError as exc:
            report = 'Falha ao gravar relatório: %s' % exc
        count = len(result.get('approved' if kind == 'confirm' else 'installed', []))
        skipped = len(result['skipped']); errors = result['errors']
        title = 'Confirmação concluída' if kind == 'confirm' else 'Aplicação ao mod concluída'
        detail = ('%d áudios %s · %d preservados/pulados · %d erros.\n\nRelatório: %s' %
                  (count, 'confirmados no projeto' if kind == 'confirm' else 'aplicados ao mod', skipped, len(errors), report))
        if errors:
            detail += '\n\n' + '\n'.join(e['reason'] for e in errors[:8])
        if kind == 'confirm': detail += '\n\nPara gravar no mod, use “Aplicar aprovados ao mod”.'
        messagebox.showinfo(title, detail, parent=self.win)
        self.app.recarregar_audios_dublados()
        self.scan()

    def confirm(self, all_rows):
        if self.busy: return
        rows = list(self.rows) if all_rows else self.selected_rows()
        if not rows: return
        try: self.validate_current(rows)
        except ValueError as exc:
            messagebox.showerror('Atualize a lista', str(exc), parent=self.win); return
        legacy, duration = self.allow_legacy.get(), self.override_duration.get()
        if not messagebox.askyesno('Confirmar áudios AI',
                'Confirmar os takes elegíveis das %d falas desta lista?\n\n'
                'AI antigos sem vínculo: %s. Diferenças de duração: %s.\n'
                'Textos desatualizados, recusados e WAVs inválidos serão preservados. '
                'Isso aprova no projeto; a aplicação ao mod é separada.' %
                (len(rows), 'incluir com seu aceite manual' if legacy else 'não incluir',
                 'aceitar' if duration else 'manter em revisão'), parent=self.win): return
        self.work(lambda: approve_rows(self.store, rows, legacy, duration, self.on_progress),
                  lambda result: self.done('confirm', rows, result), 'Confirmando takes existentes…')

    def install(self):
        if self.busy: return
        rows = [r for r in self.rows if r['state'] == 'APPROVED']
        if not rows:
            messagebox.showinfo('Sem aprovados', 'Confirme os áudios primeiro e atualize a lista.', parent=self.win); return
        try: self.validate_current(rows)
        except ValueError as exc:
            messagebox.showerror('Atualize a lista', str(exc), parent=self.win); return
        game_audio = self.app.jogo
        mod = game_audio.mod_ativo()
        eligible = sum(bool(r.get('ref')) or is_stream_record(r.get('info')) for r in rows)
        if not messagebox.askyesno('Aplicar áudios aprovados ao mod',
                'Aplicar até %d falas aprovadas ao mod “%s”?\n\nFeche o GTA antes de continuar. '
                'Cada banco será reconstruído uma vez, com backup e conferência dos demais slots. '
                'As faixas CUTSCENE/BEATS serão montadas preservando os demais trechos. O GXT não será alterado.' %
                (eligible, mod), parent=self.win): return
        self.work(lambda: install_rows(self.store, rows, game_audio, mod, self.on_progress),
                  lambda result: self.done('install', rows, result), 'Preparando aplicação dos bancos ao mod…')


def open_bulk_audio_review(app):
    if not app.g or not app.review_store or not getattr(app, 'jogo', None):
        messagebox.showinfo('Áudios AI', 'Abra um GXT e configure o projeto de áudio primeiro.', parent=app.root)
        return
    return BulkAudioReviewWindow(app)
