"""Browse physical audio slots independently of GXT caption coverage."""
import hashlib
import json
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import studio.config as config
from studio.audio.inventory import Inventory, build_inventory
from studio.audio.library_context import inventory_roots, read_manifest, base_texts, gxt_entries, SCOPE_PROFILES
from studio.script.mapping import PAKS
from studio.script.speakers import resolve_speaker
from studio.core.utils import mostrar_no_explorer
from studio.ui.dialogs import ProgressoOperacaoDialog


STATUSES = {'Todos': None, 'Com vínculo GXT': 'mapped_caption', 'Sem vínculo confirmado': 'unlinked',
            'Sem correspondência no catálogo': 'no_gxt_match', 'Ambíguos': 'ambiguous_caption',
            'Sem coordenadas': 'unclassified', 'WAV inválido': 'invalid'}
LABELS = {'mapped_caption': 'Vínculo de catálogo', 'no_gxt_match': 'Sem vínculo GXT',
          'missing_gxt_entry': 'Chave ausente no GXT', 'ambiguous_caption': 'Vínculo ambíguo',
          'unclassified': 'Sem coordenadas', 'invalid': 'WAV inválido'}
KINDS = {'named_character': 'Personagem', 'group_or_ensemble': 'Grupo', 'voice_archetype': 'Tipo de voz',
         'role_label': 'Papel', 'user_override': 'Manual'}
SOURCES = {'Todas': None, 'Extraído no projeto': 'project_original', 'Original do jogo': 'original_cache',
           'WAVs no ModLoader': 'mod', 'Extraído do mod': 'mod_cache', 'Pastas adicionadas': 'custom'}
SCOPES = {'Missões — somente SCRIPT': 'script', 'Pedestres — somente SPC': 'npc_spcs',
          'Falas — SCRIPT + SPC': 'dialogue', 'Avançado — todos os SFX, incluindo efeitos': 'all_sfx'}


def annotation_key(record):
    identity = record.get('stable_audio_id') or hashlib.sha256(str(record['path']).encode()).hexdigest()
    path_hash = hashlib.sha256(str(Path(record['path']).resolve()).encode()).hexdigest()[:16]
    return 'AUDIO|%s|%s|%s' % (record['source'], identity, path_hash)


def row_identity(record, store=None, voice_identity=None):
    manual = store.character(annotation_key(record)) if store else ''
    if manual:
        return {'identity': manual, 'status': 'manual', 'speaker_kind': 'user_override'}
    if store and record.get('status') == 'mapped_caption' and len(record.get('links', [])) == 1:
        link = record['links'][0]
        manual = store.character('%s|%08x' % (link['table'], link['hash']))
        if manual:
            return {'identity': manual, 'status': 'manual', 'speaker_kind': 'user_override'}
    if voice_identity and voice_identity.get('identity'):
        return dict(voice_identity, speaker_kind='named_character')
    results = []
    for link in record.get('links', []):
        text = link.get('base_text')
        if text and link.get('method') != 'ambiguous':
            results.append(resolve_speaker(link.get('table'), text))
    identities = {r.get('identity') for r in results if r.get('status') == 'matched'}
    if len(identities) == 1 and record['status'] == 'mapped_caption':
        return next(r for r in results if r.get('identity') in identities)
    return {'identity': None, 'status': 'unknown', 'speaker_kind': None}


class AudioLibraryWindow:
    def __init__(self, app):
        self.app, self.root = app, config.get_project_root()
        self.store = app.review_store
        self.path = self.root / 'workspace' / 'studio_index' / 'audio.sqlite'
        self.inventory = Inventory(self.path)
        self.closed, self.busy, self.offset = False, False, 0
        self.cancel_event = threading.Event()
        self.rows = {}
        self.win = tk.Toplevel(app.root)
        self.win.title('Biblioteca de áudio — com ou sem legenda')
        self.win.geometry('1120x760+20+20')
        self.win.minsize(920, 650)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        if self.path.is_file():
            try:
                if self.inventory.count(source='original_cache'):
                    self.source.set('Original do jogo')
            except Exception:
                pass
            self.refresh()
        else:
            self.status.set('Clique em Atualizar biblioteca para catalogar os arquivos extraídos.')

    def _build(self):
        frame = ttk.Frame(self.win, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Biblioteca de áudio — escolha o que quer trabalhar', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Um vínculo de catálogo não comprova que a legenda seja falada no áudio. '
                  'Sem vínculo não significa necessariamente ausência de legenda.', wraplength=1050).pack(fill='x', pady=(4, 8))
        scope_frame = ttk.Frame(frame)
        scope_frame.pack(fill='x', pady=(0, 8))
        ttk.Label(scope_frame, text='Escopo da indexação:').pack(side='left', padx=(0, 6))
        self.scope = tk.StringVar(value=next(iter(SCOPES)))
        scope_box = ttk.Combobox(scope_frame, textvariable=self.scope, values=list(SCOPES), state='readonly', width=48)
        scope_box.pack(side='left')
        scope_box.bind('<<ComboboxSelected>>', lambda e: self.change_scope())
        ttk.Label(scope_frame, text='NPCs e efeitos são opcionais. STREAMS não é indexado.').pack(side='left', padx=10)
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        ttk.Button(actions, text='Atualizar biblioteca', command=self.rebuild).pack(side='left')
        ttk.Button(actions, text='Extrair outros bancos…', command=self.app.popup_extrair_arquivos).pack(side='left', padx=6)
        self.status = tk.StringVar()
        ttk.Label(actions, textvariable=self.status, wraplength=750).pack(side='left', padx=8)
        filters = ttk.Frame(frame)
        filters.pack(fill='x', pady=8)
        self.pak = tk.StringVar(value='Todos')
        self.source = tk.StringVar(value='Todas')
        self.state = tk.StringVar(value='Todos')
        self.search = tk.StringVar()
        for title, variable, values, width in [('Arquivo', self.pak, ['Todos'] + list(PAKS), 12),
                                               ('Origem', self.source, list(SOURCES), 18),
                                               ('Vínculo', self.state, list(STATUSES), 24)]:
            ttk.Label(filters, text=title).pack(side='left', padx=(0, 4))
            box = ttk.Combobox(filters, textvariable=variable, values=values, state='readonly', width=width)
            box.pack(side='left', padx=(0, 10))
            box.bind('<<ComboboxSelected>>', lambda e: self.search_now())
        search = ttk.Entry(filters, textvariable=self.search, width=22)
        search.pack(side='left', fill='x', expand=True)
        search.bind('<Return>', lambda e: self.search_now())
        ttk.Button(filters, text='Buscar', command=self.search_now).pack(side='left', padx=(4, 0))
        cols = ('id', 'source', 'duration', 'state', 'speaker')
        self.tree = ttk.Treeview(frame, columns=cols, show='headings', height=12)
        for c, name, width in zip(cols, ('ID do áudio', 'Origem', 'Duração', 'Vínculo com GXT', 'Identidade / tipo de voz'), (210, 200, 70, 180, 260)):
            self.tree.heading(c, text=name)
            self.tree.column(c, width=width)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.details())
        self.tree.bind('<Double-1>', lambda e: self.play())
        page = ttk.Frame(frame)
        page.pack(fill='x', pady=6)
        ttk.Button(page, text='Anterior', command=lambda: self.move(-200)).pack(side='left')
        ttk.Button(page, text='Próxima', command=lambda: self.move(200)).pack(side='left', padx=4)
        self.page_label = ttk.Label(page)
        self.page_label.pack(side='left', padx=8)
        ttk.Button(page, text='Ouvir', command=self.play).pack(side='right')
        ttk.Button(page, text='Parar', command=self.app.parar_audio).pack(side='right', padx=4)
        ttk.Button(page, text='Localizar arquivo', command=self.locate).pack(side='right')
        self.text = tk.Text(frame, height=6, wrap='word', state='disabled', font=('Segoe UI', 9))
        self.text.pack(fill='both', pady=4)
        voice = ttk.Frame(frame)
        voice.pack(fill='x', pady=6)
        ttk.Label(voice, text='Personagem confirmado por você:').pack(side='left')
        self.character = tk.StringVar()
        ttk.Entry(voice, textvariable=self.character, width=25).pack(side='left', padx=6)
        ttk.Button(voice, text='Guardar identificação', command=self.assign).pack(side='left')
        ttk.Button(voice, text='Guardar como referência', command=self.reference).pack(side='left', padx=6)
        ttk.Label(frame, text='Transcrição da referência (preencha ao ouvir, se o áudio não tiver legenda conhecida):').pack(anchor='w')
        self.transcript = tk.Text(frame, height=2, wrap='word')
        self.transcript.pack(fill='x')
        ttk.Label(frame, text='Atualize a biblioteca depois de editar ou trocar o GXT. Identidades da wiki se referem ao texto original; '
                  'o personagem da voz precisa de conferência por escuta.', wraplength=1050).pack(fill='x', pady=(5, 0))

    def filters(self):
        return {'pak': None if self.pak.get() == 'Todos' else self.pak.get(),
                'source': SOURCES[self.source.get()],
                'status': STATUSES[self.state.get()], 'search': self.search.get()}

    def search_now(self):
        self.offset = 0
        self.refresh()

    def change_scope(self):
        self.pak.set('Todos')
        self.offset = 0
        self.refresh()

    def selected_paks(self):
        return SCOPE_PROFILES[SCOPES[self.scope.get()]]

    def move(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh()

    def refresh(self):
        if self.closed or not self.path.is_file():
            return
        try:
            if not self.context_current():
                self.clear_stale()
                return
            total = self.inventory.count(**self.filters())
            if self.offset >= total:
                self.offset = max(0, ((total - 1) // 200) * 200)
            rows = self.inventory.query(**self.filters(), offset=self.offset, limit=200)
            summary = self.inventory.summary()
        except Exception as exc:
            self.status.set('Não foi possível ler a biblioteca: %s' % exc)
            return
        self.tree.delete(*self.tree.get_children())
        self.rows = {str(r['id']): r for r in rows}
        for iid, row in self.rows.items():
            identity = row_identity(row, self.store, self.app.voice_identity_for(row))
            who = identity.get('identity')
            who = '%s: %s' % (KINDS.get(identity.get('speaker_kind'), 'Wiki'), who) if who else 'Desconhecido'
            if identity.get('status') == 'VOICE_SUGGESTED':
                who += ' (sugestão)'
            elif identity.get('status') == 'VOICE_CONFIRMED':
                who += ' (confirmado por voz)'
            self.tree.insert('', 'end', iid=iid, values=(row.get('stable_audio_id') or Path(row['path']).name,
                             row['label'], '%.2fs' % row['duration'] if row['duration'] else '—', LABELS[row['status']], who))
        self.page_label.config(text='%d–%d de %d' % (self.offset + 1 if rows else 0, self.offset + len(rows), total))
        self.status.set('%d arquivos (inclui cópias) · %d slots ligados · %d arquivos sem vínculo · %d inválidos' % (
            summary['sound_count'], summary['mapped_unique'], summary['no_link'], summary['invalid']))

    def context_current(self):
        base = [{'table': t, 'hash': h, 'text': text} for (t, h), text in base_texts(self.app).items()]
        scope_matches = self.inventory.summary().get('scope_paks') == sorted(self.selected_paks())
        return scope_matches and self.inventory.context_matches(base, gxt_entries(self.app.g))['matches']

    def clear_stale(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.text.config(state='normal')
        self.text.delete('1.0', 'end')
        self.text.config(state='disabled')
        self.transcript.delete('1.0', 'end')
        self.character.set('')
        self.page_label.config(text='')
        self.status.set('GXT ou escopo diferente do índice salvo. Clique em Atualizar biblioteca para catalogar somente sua seleção.')

    def selected(self):
        if not self.app.commit():
            return None
        if not self.context_current():
            self.clear_stale()
            return None
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def details(self):
        row = self.selected()
        if not row:
            return
        identity = row_identity(row, self.store, self.app.voice_identity_for(row))
        lines = [str(row['path']), 'Estado: %s' % LABELS[row['status']]]
        if identity.get('identity'):
            lines.append('%s: %s · %s' % (KINDS.get(identity.get('speaker_kind'), 'Wiki'), identity['identity'], identity['status']))
            if identity.get('source_url'):
                lines.append('Fonte: %s · identidade do texto; escuta/contexto da missão não comprovados.' % identity['source_url'])
        for link in row['links']:
            lines.extend(['GXT: %s · método: %s' % (link['gxt_stable_id'], link['method']),
                          'Original: %s' % (link.get('base_text') or 'não encontrado'),
                          'Mod: %s' % (link.get('mod_text') or 'não encontrado')])
        if not row['links']:
            lines.append('Sem legenda associada. Ouça e preencha uma transcrição/referência manualmente.')
        self.text.config(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', '\n'.join(lines))
        self.text.config(state='disabled')
        self.character.set(self.store.character(annotation_key(row)) if self.store else '')
        self.transcript.delete('1.0', 'end')
        if row['status'] == 'mapped_caption' and row['source'] == 'original_cache' and len(row['links']) == 1:
            self.transcript.insert('1.0', row['links'][0].get('base_text') or '')

    def play(self):
        row = self.selected()
        if row:
            self.app._tocar(Path(row['path']), 'biblioteca de áudio')

    def locate(self):
        row = self.selected()
        if row:
            mostrar_no_explorer(row['path'])

    def assign(self):
        row = self.selected()
        if not row or not self.store:
            return
        try:
            self.store.assign_character(annotation_key(row), self.character.get())
            self.refresh()
        except (ValueError, OSError) as exc:
            messagebox.showerror('Identificação', str(exc), parent=self.win)

    def reference(self):
        row = self.selected()
        if not row or not self.store:
            return
        try:
            self.store.add_reference(self.character.get(), row['path'], self.transcript.get('1.0', 'end-1c'),
                                     {'stable_audio_id': row.get('stable_audio_id'), 'source': row['source'],
                                      'caption_cooccurrence_verified': False})
            self.store.assign_character(annotation_key(row), self.character.get())
            self.status.set('Referência guardada. Confira a transcrição ouvindo o áudio.')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Referência', str(exc), parent=self.win)

    def rebuild(self):
        if self.busy:
            return
        if not self.app.commit():
            return
        try:
            selected_paks = self.selected_paks()
            roots = inventory_roots(self.app, self.root, paks=selected_paks)
            base = [{'table': t, 'hash': h, 'text': text} for (t, h), text in base_texts(self.app).items()]
            mod = gxt_entries(self.app.g)
        except Exception as exc:
            messagebox.showerror('Biblioteca', str(exc), parent=self.win)
            return
        if not roots:
            self.status.set('Nenhuma pasta extraída encontrada. Extraia bancos ou configure pastas de WAVs.')
            return
        self.busy = True
        self.cancel_event = threading.Event()
        progress = ProgressoOperacaoDialog(self.win, 'Catalogando todos os áudios')
        progress.cancel_event = self.cancel_event
        def update(count, total, path):
            def display():
                if not self.closed and progress.win.winfo_exists():
                    progress.atualizar(0, 0, 'Catalogando: %s' % Path(path).name)
                    progress.lbl_detalhes.config(text='%d / %s arquivos' % (count, total if total is not None else 'localizando'))
                    if total:
                        progress.pb.stop()
                        progress.pb.config(mode='determinate', value=count * 100 / total)
            self.app.ui(display)
        def work():
            try:
                build_inventory(self.path, roots, read_manifest(self.root), base, mod, self.cancel_event, update,
                                scope_paks=selected_paks)
                error = None
            except Exception as exc:
                error = str(exc)
            def finish():
                self.busy = False
                if self.closed:
                    return
                progress.fechar()
                if error:
                    self.status.set('Índice anterior preservado: %s' % error)
                else:
                    self.offset = 0
                    self.refresh()
            self.app.ui(finish)
        threading.Thread(target=work, daemon=True).start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        self.app.parar_audio()
        self.win.destroy()
