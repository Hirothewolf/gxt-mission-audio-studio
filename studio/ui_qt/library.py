"""Native Qt views over the classic audio and voice identity stores.

The library only exposes paths that the active resolver or the record's explicit
source metadata can identify. Character labels come from manual review data; this
module does not infer speakers or run voice analysis.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QEventLoop, QModelIndex, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QProgressBar, QTabWidget, QTableView, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)


def _record_info(context, record):
    info = dict(record.get('info') or {})
    if not info and callable(getattr(context, 'info', None)):
        try:
            info = dict(context.info(record) or {})
        except (OSError, ValueError, KeyError, TypeError):
            info = {}
    return info


def _record_line_id(record):
    if record.get('line_id'):
        return str(record['line_id'])
    table = record.get('table') or record.get('tab')
    value = record.get('hash', record.get('key'))
    if table is not None and value is not None:
        try:
            return '%s|%08x' % (table, int(value))
        except (TypeError, ValueError):
            pass
    return ''


def _path_value(record, info, names):
    for source in (record, info):
        for name in names:
            value = source.get(name)
            if value:
                try:
                    path = Path(value).expanduser().resolve()
                    if path.is_file():
                        return path
                except (OSError, TypeError, ValueError):
                    continue
    return None


def _loose_mod_path(context, record, info):
    game = getattr(context, 'game_audio', None)
    selected_mod = getattr(context, 'selected_mod', None)
    mod = None
    if callable(selected_mod):
        try:
            mod = str(selected_mod() or '').strip()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            mod = None
    if not mod and game is not None:
        try:
            mod = str(game.mod_ativo() or '').strip()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            mod = None
    # Legacy explicit paths are useful for contexts without mod selection, but
    # must not override the mod selected by the open GXT.
    if not game or not mod:
        explicit = _path_value(record, info, ('installed_path', 'mod_path', 'mod_audio_path'))
        if explicit:
            return explicit, 'Caminho de mod informado no registro'
    if not game:
        return None, ''
    try:
        if not mod or mod in ('.', '..') or any(char in mod for char in '/\\:'):
            return None, ''
        base = game.modloader_dir()
        ref = game.ref_para(info)
        if not base or not ref or len(ref) != 3:
            return None, ''
        pak, bank, sound = ref
        path = Path(base) / mod / 'audio' / 'SFX' / str(pak) / ('Bank_%03d' % int(bank)) / ('sound_%03d.wav' % int(sound))
        path = path.resolve()
        if path.is_file():
            return path, 'ModLoader solto por coordenada SCRIPT'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, ''
    return None, ''


def _packed_mod_path(context, info):
    """Return only a path backed by the ModLoader install manifest; never export here."""
    game = getattr(context, 'game_audio', None)
    if not game or not info:
        return None, ''
    try:
        from .audio_sync import installed_line_evidence
        selected_mod = getattr(context, 'selected_mod', None)
        mod = selected_mod() if callable(selected_mod) else game.mod_ativo()
        evidence = installed_line_evidence(info, context.project_root, game, mod=mod)
        path = evidence.get('path')
        if path:
            return Path(path).resolve(), 'Banco do mod confirmado pelo manifesto de instalação'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None, ''


def _manual_character(store, record):
    line_id = _record_line_id(record)
    if not line_id or not store:
        return ''
    try:
        return str(store.character(line_id) or '')
    except (OSError, ValueError, KeyError, AttributeError):
        return ''


class _LibraryWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, context, records, mode, parent=None):
        super().__init__(parent)
        self.context = context
        self.records = [dict(row) for row in records or () if isinstance(row, dict)]
        self.mode = mode
        self.cancel_requested = threading.Event()

    def cancel(self):
        self.cancel_requested.set()

    def run(self):
        try:
            resolver = getattr(self.context, 'base_audio', None)
            if resolver is not None and not getattr(self.context, '_resolver_ready', True):
                self.status.emit('Indexando arquivos originais… o cancelamento será aplicado ao fim desta etapa.')
                initialize = getattr(self.context, 'initialize_resolver', None)
                if callable(initialize):
                    initialize()
            if self.cancel_requested.is_set():
                self.loaded.emit({'cancelled': True})
                return

            rows = []
            seen_paths = set()
            for index, record in enumerate(self.records):
                if self.cancel_requested.is_set():
                    self.loaded.emit({'cancelled': True})
                    return
                info = _record_info(self.context, record)
                table = record.get('table') or record.get('tab')
                key = record.get('hash', record.get('key'))
                original = _path_value(record, info, ('original_path', 'original_audio_path'))
                original_method, original_confidence = 'explicit record path', None
                game = getattr(self.context, 'game_audio', None)
                if not original and game is not None and info:
                    try:
                        cached = game.original_para(info)
                        if cached and Path(cached).is_file():
                            original = Path(cached).resolve()
                            original_method = 'cache exato do GameAudio'
                    except (OSError, ValueError, TypeError, KeyError, AttributeError):
                        original = None
                if not original and resolver is not None and table is not None and key is not None:
                    try:
                        resolved, original_method, original_confidence = resolver.resolve(
                            table, key, record.get('name') or record.get('label'), info)
                        original = Path(resolved).resolve() if resolved else None
                        if original and not original.is_file():
                            original = None
                    except (OSError, ValueError, TypeError, KeyError):
                        original = None
                mod, mod_method = _loose_mod_path(self.context, record, info)
                if not mod:
                    mod, mod_method = _packed_mod_path(self.context, info)
                mapping = '%s · %s' % (table or '—', record.get('name') or record.get('label') or _record_line_id(record) or 'sem chave')
                character = _manual_character(getattr(self.context, 'review_store', None), record)
                sources = []
                if original:
                    provenance = 'Original exato: %s%s' % (
                        original_method, ' (%d%%)' % int(original_confidence)
                        if isinstance(original_confidence, (int, float)) else '')
                    sources.append((original, 'Original', provenance))
                if mod:
                    sources.append((mod, 'Mod', mod_method))
                for path, source, provenance in sources:
                    row = {'path': path, 'source': source, 'provenance': provenance,
                           'record': record, 'mapping': mapping, 'character': character,
                           'audio_id': info.get('stable_audio_id') or record.get('stable_audio_id') or path.stem}
                    rows.append(row)
                    seen_paths.add(str(path.resolve()))
                if not sources:
                    rows.append({'path': None, 'source': 'Indisponível',
                        'provenance': 'Nenhum arquivo original ou mod resolvido para esta chave.',
                        'record': record, 'mapping': mapping, 'character': character,
                        'audio_id': info.get('stable_audio_id') or record.get('stable_audio_id') or _record_line_id(record)})

            if resolver is not None:
                for indexed in list(getattr(resolver, 'records', ()) or ()):
                    if self.cancel_requested.is_set():
                        self.loaded.emit({'cancelled': True})
                        return
                    try:
                        path = Path(indexed['path']).resolve()
                        if not path.is_file() or str(path) in seen_paths:
                            continue
                        rows.append({'path': path, 'source': 'Original',
                            'provenance': 'Índice de originais: %s' % (indexed.get('rel') or path),
                            'record': None, 'mapping': 'Sem vínculo selecionado', 'character': '',
                            'audio_id': indexed.get('stable_audio_id') or indexed.get('rel') or path.stem})
                        seen_paths.add(str(path))
                    except (OSError, TypeError, ValueError, KeyError):
                        continue

            result = {'rows': rows}
            if self.mode == 'voice_identity':
                store = getattr(self.context, 'identity_store', None)
                result['report'] = store.latest() if store else None
                review = getattr(self.context, 'review_store', None)
                result['characters'] = review.characters() if review else []
                result['references'] = []
                for character in result['characters']:
                    for reference in review.references(character):
                        result['references'].append((character, reference))
            if self.cancel_requested.is_set():
                self.loaded.emit({'cancelled': True})
            else:
                self.loaded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class _LibraryTableModel(QAbstractTableModel):
    """Virtual table over library rows with cached searchable text."""
    RowRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, headers, columns, search_fields, parent=None):
        super().__init__(parent)
        self.headers = tuple(headers)
        self.columns = tuple(columns)
        self.search_fields = tuple(search_fields)
        self._rows = []
        self._search_values = []
        self._visible = []
        self._query = ''
        self._source = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._visible)):
            return None
        row = self._rows[self._visible[index.row()]]
        if role == self.RowRole:
            return row
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(row.get('provenance') or row.get('path') or '')
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        value = self.columns[index.column()](row)
        return '' if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows or ())
        self._search_values = [
            ' '.join(str(row.get(field) or '') for field in self.search_fields).casefold()
            for row in self._rows
        ]
        self._rebuild_visible()
        self.endResetModel()

    def set_filter(self, query='', source=None):
        query = str(query or '').casefold().strip()
        source = source or None
        if query == self._query and source == self._source:
            return
        self.beginResetModel()
        self._query, self._source = query, source
        self._rebuild_visible()
        self.endResetModel()

    def _rebuild_visible(self):
        query, source = self._query, self._source
        self._visible = [index for index, row in enumerate(self._rows)
                         if (source is None or row.get('source') == source)
                         and (not query or query in self._search_values[index])]

    def row_at(self, visible_index):
        if 0 <= visible_index < len(self._visible):
            return self._rows[self._visible[visible_index]]
        return None


class _LibraryTableView(QTableView):
    """QTableView with the familiar currentRow accessor from the old widgets."""
    def currentRow(self):
        index = self.currentIndex()
        return index.row() if index.isValid() else -1


class _ResolveWorker(QThread):
    resolved = Signal(object, str, str)

    def __init__(self, context, record, preferred, parent=None):
        super().__init__(parent)
        self.context = context
        self.record = dict(record)
        self.preferred = preferred

    def run(self):
        methods = {'Original': getattr(self.context, 'original_path', None),
                   'Mod': getattr(self.context, 'installed_path', None)}
        order = [self.preferred] if self.preferred in methods else []
        order += [source for source in ('Original', 'Mod') if source not in order]
        errors = []
        for source in order:
            resolver = methods.get(source)
            if not callable(resolver):
                continue
            try:
                path = resolver(self.record)
                if path and Path(path).is_file():
                    self.resolved.emit(Path(path).resolve(), source, '')
                    return
            except Exception as exc:
                errors.append(str(exc))
        self.resolved.emit(None, '', '; '.join(errors) or 'Nenhum arquivo foi localizado.')


class _BaseLibraryDialog(QDialog):
    recordRequested = Signal(object)
    changed = Signal()

    def __init__(self, context, records=None, parent=None, title='Biblioteca'):
        super().__init__(parent)
        self.context = context
        self.records_input = [dict(row) for row in records or () if isinstance(row, dict)]
        self.rows = []
        self._load_done = False
        self._load_loop = None
        self._closing = False
        self._close_after_worker = False
        self._resolve_worker = None
        self.setWindowTitle(title)
        self.resize(1050, 700)
        self._root = QVBoxLayout(self)
        self.status = QLabel('Preparando biblioteca…')
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.cancel_button = QPushButton('Cancelar indexação')
        self.cancel_button.clicked.connect(self.cancel_index)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.progress)
        status_row.addWidget(self.cancel_button)
        self._root.addLayout(status_row)
        self.worker = _LibraryWorker(context, self.records_input, self.mode, self)
        self.worker.status.connect(self.status.setText)
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    mode = 'audio_library'

    def cancel_index(self):
        if self.worker.isRunning():
            self.worker.cancel()
            self.status.setText('Cancelamento solicitado. A etapa atual terminará antes de fechar a biblioteca.')

    def wait_for_index(self, timeout_ms=3000):
        """Wait for background loading while servicing Qt events (also useful to callers)."""
        if self._load_done:
            return True
        loop = QEventLoop(self)
        timer = QTimer(self)
        timer.setSingleShot(True)
        self._load_loop = loop
        if self._load_done:
            self._load_loop = None
            return True
        timer.timeout.connect(loop.quit)
        timer.start(max(1, int(timeout_ms)))
        loop.exec()
        self._load_loop = None
        return self._load_done

    def _loaded(self, result):
        self._load_done = True
        loop, self._load_loop = self._load_loop, None
        if loop is not None:
            loop.quit()
        self.progress.hide()
        self.cancel_button.hide()
        if result.get('cancelled'):
            self.status.setText('Indexação cancelada; nenhum resultado parcial foi publicado.')
            return
        self.rows = list(result.get('rows') or [])
        self._apply_loaded(result)

    def _apply_loaded(self, result):
        pass

    def _failed(self, message):
        self._load_done = True
        loop, self._load_loop = self._load_loop, None
        if loop is not None:
            loop.quit()
        self.progress.hide()
        self.cancel_button.hide()
        self.status.setText('Não foi possível carregar a biblioteca: ' + message)

    def _worker_finished(self):
        if self._close_after_worker:
            self._close_after_worker = False
            QTimer.singleShot(0, self.close)

    def operation_active(self):
        return self.worker.isRunning() or bool(self._resolve_worker and self._resolve_worker.isRunning())

    def closeEvent(self, event):
        if self.worker.isRunning() or (self._resolve_worker and self._resolve_worker.isRunning()):
            self._close_after_worker = True
            self._closing = True
            self.worker.cancel()
            self.context.player.stop()
            self.hide()
            event.ignore()
            return
        try:
            self.context.player.stop()
        except (AttributeError, RuntimeError):
            pass
        super().closeEvent(event)

    def resolve_and_play(self, row, preferred=None):
        if self._resolve_worker and self._resolve_worker.isRunning():
            self.status.setText('Já estou localizando o áudio selecionado…')
            return
        if not row.get('record'):
            return
        self.status.setText('Localizando o áudio desta fala em segundo plano…')
        worker = _ResolveWorker(self.context, row['record'], preferred, self)
        self._resolve_worker = worker
        worker.resolved.connect(self._resolved_for_playback)
        worker.finished.connect(self._resolve_worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _resolve_worker_finished(self):
        self._resolve_worker = None
        if self._close_after_worker:
            self._close_after_worker = False
            QTimer.singleShot(0, self.close)

    def _resolved_for_playback(self, path, source, error):
        if path:
            self.status.setText('%s localizado; iniciando reprodução.' % source)
            self.context.player.play(path)
        else:
            self.status.setText('Áudio indisponível: ' + error)


class AudioLibraryDialog(_BaseLibraryDialog):
    """Search original/mod audio and map a selected GXT record by hand."""
    mode = 'audio_library'

    def __init__(self, context, records=None, parent=None):
        super().__init__(context, records, parent, 'Biblioteca de áudio — original e mod')
        self.search = QLineEdit()
        self.search.setPlaceholderText('Buscar arquivo, ID, tabela, chave ou personagem manual')
        self.search.setClearButtonEnabled(True)
        self.source_filter = QComboBox()
        self.source_filter.addItems(['Todas as origens', 'Original', 'Mod'])
        self.source_filter.currentTextChanged.connect(self.refresh_rows)
        controls = QHBoxLayout()
        controls.addWidget(self.search, 1)
        controls.addWidget(QLabel('Origem:'))
        controls.addWidget(self.source_filter)
        self._root.insertLayout(1, controls)
        self.table = _LibraryTableView()
        self.library_model = _LibraryTableModel(
            ['Origem', 'ID do áudio', 'Arquivo', 'Vínculo de texto', 'Personagem manual'],
            (lambda row: row.get('source'), lambda row: row.get('audio_id'),
             lambda row: Path(row['path']).name if row.get('path') else 'Áudio indisponível',
             lambda row: row.get('mapping'),
             lambda row: row.get('character') or 'Sem atribuição manual'),
            ('source', 'audio_id', 'path', 'mapping', 'character', 'provenance'), self)
        self.table.setModel(self.library_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self.show_provenance)
        self.table.doubleClicked.connect(lambda *_: self.open_record())
        self._root.addWidget(self.table, 1)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self.refresh_rows)
        self.search.textChanged.connect(lambda *_: self._search_timer.start())
        self.provenance = QLabel('A proveniência do original aparece ao selecionar um arquivo.')
        self.provenance.setWordWrap(True)
        self._root.addWidget(self.provenance)
        form = QFormLayout()
        self.character = QLineEdit()
        self.character.setPlaceholderText('Identidade conferida por você')
        self.transcript = QPlainTextEdit()
        self.transcript.setPlaceholderText('Transcrição confirmada por escuta, se aplicável')
        self.transcript.setMaximumHeight(65)
        form.addRow('Personagem manual', self.character)
        form.addRow('Transcrição da referência', self.transcript)
        self._root.addLayout(form)
        actions = QHBoxLayout()
        for label, callback in (('Ouvir', self.play_selected), ('Parar', self.stop_audio),
                                ('Abrir fala no editor', self.open_record),
                                ('Guardar personagem', self.assign_character),
                                ('Guardar referência', self.save_reference)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        self._root.addLayout(actions)

    def _apply_loaded(self, result):
        self.library_model.set_rows(self.rows)
        self.refresh_rows()
        self.status.setText('%d fontes de áudio disponíveis. Identidades exibidas aqui vêm de atribuições manuais.' % len(self.rows))

    def refresh_rows(self, *_):
        query = self.search.text().casefold().strip()
        source = self.source_filter.currentText()
        self.library_model.set_filter(query, None if source == 'Todas as origens' else source)

    def selected_row(self):
        index = self.table.currentIndex()
        return self.library_model.row_at(index.row()) if index.isValid() else None

    def show_provenance(self):
        row = self.selected_row()
        if not row:
            self.provenance.setText('A proveniência do original aparece ao selecionar um arquivo.')
            self.character.clear()
            return
        self.provenance.setText('%s\n%s' % (row['provenance'], row['path']))
        record = row.get('record')
        self.character.setText(_manual_character(self.context.review_store, record) if record else '')

    def play_selected(self):
        row = self.selected_row()
        if row and row.get('path'):
            self.context.player.play(Path(row['path']))
        elif row and row.get('record'):
            source = self.source_filter.currentText()
            self.resolve_and_play(row, source if source in ('Original', 'Mod') else None)

    def stop_audio(self):
        self.context.player.stop()

    def open_record(self):
        row = self.selected_row()
        if row and row.get('record'):
            self.recordRequested.emit(dict(row['record']))

    def assign_character(self):
        row = self.selected_row()
        record = row.get('record') if row else None
        line_id = _record_line_id(record or {})
        if not line_id:
            QMessageBox.information(self, 'Personagem manual', 'Selecione um áudio vinculado a uma fala do GXT.')
            return
        try:
            self.context.review_store.assign_character(line_id, self.character.text())
            for source in self.rows:
                if source.get('record') and _record_line_id(source['record']) == line_id:
                    source['character'] = self.character.text().strip()
            self.refresh_rows()
            self.changed.emit()
            self.status.setText('Personagem manual guardado em %s.' % line_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Personagem manual', str(exc))

    def save_reference(self):
        row = self.selected_row()
        character = self.character.text().strip()
        if not row or not character:
            QMessageBox.information(self, 'Referência', 'Selecione o áudio e informe o personagem confirmado manualmente.')
            return
        try:
            self.context.review_store.add_reference(character, row['path'], self.transcript.toPlainText().strip(),
                {'source': row['source'], 'source_path': str(Path(row['path']).resolve()),
                 'provenance': row['provenance'], 'gxt_line_id': _record_line_id(row.get('record') or {}) or None,
                 'identity_confirmation': 'MANUAL'})
            record = row.get('record')
            if record and _record_line_id(record):
                self.context.review_store.assign_character(_record_line_id(record), character)
            self.changed.emit()
            self.status.setText('Referência copiada para o catálogo. O original permaneceu intacto.')
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Referência', str(exc))

class VoiceIdentityDialog(_BaseLibraryDialog):
    """Browse saved voice groups and reference clips; confirmations are manual."""
    mode = 'voice_identity'

    def __init__(self, context, records=None, parent=None):
        super().__init__(context, records, parent, 'Identidade de voz — catálogo e confirmações manuais')
        self.tabs = QTabWidget()
        self._root.insertWidget(1, self.tabs, 1)
        self.group_tab = QWidget()
        group_layout = QVBoxLayout(self.group_tab)
        group_layout.addWidget(QLabel('Grupos salvos no relatório mais recente. A identidade só muda após confirmação manual.'))
        self.groups = QTableWidget(0, 3)
        self.groups.setHorizontalHeaderLabels(['Grupo', 'Estado do relatório', 'Falas'])
        self.groups.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.groups.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.groups.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.groups.itemSelectionChanged.connect(self.show_group)
        group_layout.addWidget(self.groups, 1)
        self.members = QTableWidget(0, 4)
        self.members.setHorizontalHeaderLabels(['Áudio original', 'Estado salvo', 'Score', 'Caminho registrado'])
        self.members.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.members.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.members.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        group_layout.addWidget(self.members, 1)
        group_controls = QHBoxLayout()
        self.group_character = QLineEdit()
        self.group_character.setPlaceholderText('Digite o personagem depois de ouvir')
        group_controls.addWidget(self.group_character, 1)
        for label, callback in (('Ouvir áudio', self.play_member), ('Confirmar grupo', self.assign_group)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            group_controls.addWidget(button)
        group_layout.addLayout(group_controls)
        self.tabs.addTab(self.group_tab, 'Grupos salvos')

        self.mapping_tab = QWidget()
        mapping_layout = QVBoxLayout(self.mapping_tab)
        mapping_layout.addWidget(QLabel('Atribua um nome somente após conferência por escuta. Estas marcações usam o arquivo de revisão do projeto.'))
        self.mapping_search = QLineEdit()
        self.mapping_search.setPlaceholderText('Filtrar por texto, tabela, chave ou nome manual')
        mapping_layout.addWidget(self.mapping_search)
        self.mappings = _LibraryTableView()
        self.mapping_model = _LibraryTableModel(
            ['Fala / chave', 'Áudio', 'Personagem manual', 'Proveniência'],
            (lambda row: row.get('mapping'), lambda row: row.get('audio_id'),
             lambda row: row.get('character') or 'Sem atribuição manual', lambda row: row.get('provenance')),
            ('mapping', 'audio_id', 'character', 'path', 'provenance'), self)
        self.mappings.setModel(self.mapping_model)
        self.mappings.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mappings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.mappings.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mappings.horizontalHeader().setStretchLastSection(True)
        mapping_layout.addWidget(self.mappings, 1)
        mapping_controls = QHBoxLayout()
        self.mapping_character = QLineEdit()
        self.mapping_character.setPlaceholderText('Personagem confirmado por você')
        mapping_controls.addWidget(self.mapping_character, 1)
        for label, callback in (('Ouvir áudio', self.play_mapping), ('Abrir fala no editor', self.open_mapping),
                                ('Guardar personagem', self.assign_mapping)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            mapping_controls.addWidget(button)
        mapping_layout.addLayout(mapping_controls)
        self.tabs.addTab(self.mapping_tab, 'Mapeamentos atuais')

        self.reference_tab = QWidget()
        reference_layout = QVBoxLayout(self.reference_tab)
        reference_layout.addWidget(QLabel('Referências adicionadas manualmente ao projeto. Nenhuma referência é criada por análise automática.'))
        self.reference_search = QLineEdit()
        self.reference_search.setPlaceholderText('Buscar personagem, arquivo ou transcrição')
        self.reference_search.textChanged.connect(self.refresh_references)
        reference_layout.addWidget(self.reference_search)
        self.reference_list = QListWidget()
        self.reference_list.itemDoubleClicked.connect(lambda *_: self.play_reference())
        reference_layout.addWidget(self.reference_list, 1)
        reference_actions = QHBoxLayout()
        for label, callback in (('Ouvir referência', self.play_reference), ('Abrir arquivo', self.reveal_reference)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            reference_actions.addWidget(button)
        reference_layout.addLayout(reference_actions)
        self.tabs.addTab(self.reference_tab, 'Catálogo de referências')
        self._mapping_search_timer = QTimer(self)
        self._mapping_search_timer.setSingleShot(True)
        self._mapping_search_timer.setInterval(120)
        self._mapping_search_timer.timeout.connect(self.refresh_mappings)
        self.mapping_search.textChanged.connect(lambda *_: self._mapping_search_timer.start())
        self.report = None
        self.groups_data = []
        self.group_items = []
        self.references_data = []
        self.visible_mappings = []
        from .voice_analysis import VoiceAnalysisWidget
        self.analysis = VoiceAnalysisWidget(context, self)
        self.tabs.addTab(self.analysis, 'Análise automática (opcional)')
        self.analysis.completed.connect(self.analysis_finished)

    def operation_active(self):
        return super().operation_active() or self.analysis.busy()

    def closeEvent(self, event):
        if self.analysis.busy():
            self._close_after_analysis = True
            self.analysis.cancel()
            event.ignore()
        else:
            super().closeEvent(event)

    def analysis_finished(self):
        self._apply_loaded(dict(rows=self.rows, report=self.context.identity_store.latest(), references=self.references_data))
        if getattr(self, '_close_after_analysis', False):
            self._close_after_analysis = False
            self.close()

    def _apply_loaded(self, result):
        self.report = result.get('report')
        self.groups_data = list((self.report or {}).get('groups') or [])
        self.group_items = list((self.report or {}).get('items') or [])
        self.references_data = list(result.get('references') or [])
        self.groups.setRowCount(len(self.groups_data))
        for index, group in enumerate(self.groups_data):
            confirmed = str(group.get('status') or '').upper() in ('CONFIRMED', 'MANUAL')
            label = group.get('confirmed_identity') if confirmed else 'Grupo %s' % (group.get('id') or index + 1)
            state = 'Personagem confirmado manualmente' if confirmed else 'Grupo salvo · personagem não confirmado'
            values = (label, state, len(group.get('item_ids') or []))
            for column, value in enumerate(values):
                self.groups.setItem(index, column, QTableWidgetItem(str(value)))
        self.mapping_model.set_rows([row for row in self.rows if row.get('record')])
        self.refresh_mappings()
        self.refresh_references()
        self.status.setText('Relatório salvo: %d grupos · %d referências manuais · %d arquivos ligados à tela atual.' %
                            (len(self.groups_data), len(self.references_data), len(self.rows)))
        if self.groups_data:
            self.groups.selectRow(0)

    def _group(self):
        index = self.groups.currentRow()
        return self.groups_data[index] if 0 <= index < len(self.groups_data) else None

    def show_group(self):
        group = self._group()
        self.members.setRowCount(0)
        if not group or not self.report:
            return
        by_id = {item.get('audio_id'): item for item in self.group_items}
        members = [by_id[audio_id] for audio_id in group.get('item_ids', []) if audio_id in by_id]
        self.members.setRowCount(len(members))
        self.member_rows = members
        for index, item in enumerate(members):
            values = (item.get('audio_id') or '', item.get('status') or 'Sem estado', '—', item.get('path') or '')
            for column, value in enumerate(values):
                self.members.setItem(index, column, QTableWidgetItem(str(value)))
        self.group_character.clear()
        self.group_character.setPlaceholderText('Digite o personagem depois de ouvir')
        self.group_character.setEnabled(str(group.get('status') or '').upper() not in ('CONFIRMED', 'MANUAL'))

    def play_member(self):
        index = self.members.currentRow()
        member = getattr(self, 'member_rows', [])[index] if 0 <= index < len(getattr(self, 'member_rows', [])) else None
        path = Path(member.get('path', '')) if member else None
        if path and path.is_file():
            self.context.player.play(path)
        elif member:
            self.status.setText('Áudio não disponível no caminho salvo: %s' % (path or ''))

    def assign_group(self):
        group = self._group()
        character = self.group_character.text().strip()
        if not group or not self.report or not character:
            QMessageBox.information(self, 'Confirmação manual', 'Selecione um grupo e digite o personagem confirmado por escuta.')
            return
        try:
            result = self.context.identity_store.assign_group(self.report['id'], group['id'], character)
            self.changed.emit()
            self.status.setText('Grupo confirmado manualmente como %s (%d falas).' % (result['identity'], len(result.get('item_ids', []))))
            self.report = self.context.identity_store.latest()
            self.groups_data = list((self.report or {}).get('groups') or [])
            self._apply_loaded({'rows': self.rows, 'report': self.report, 'references': self.references_data})
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, 'Confirmação manual', str(exc))

    def refresh_mappings(self, *_):
        query = self.mapping_search.text().casefold().strip()
        self.mapping_model.set_filter(query)

    def selected_mapping(self):
        index = self.mappings.currentIndex()
        return self.mapping_model.row_at(index.row()) if index.isValid() else None

    def play_mapping(self):
        row = self.selected_mapping()
        if row and row.get('path'):
            self.context.player.play(Path(row['path']))
        elif row and row.get('record'):
            self.resolve_and_play(row)

    def open_mapping(self):
        row = self.selected_mapping()
        if row and row.get('record'):
            self.recordRequested.emit(dict(row['record']))

    def assign_mapping(self):
        row = self.selected_mapping()
        line_id = _record_line_id((row or {}).get('record') or {})
        if not line_id:
            QMessageBox.information(self, 'Personagem manual', 'Selecione um áudio ligado a uma fala do GXT.')
            return
        try:
            self.context.review_store.assign_character(line_id, self.mapping_character.text())
            character = self.mapping_character.text().strip()
            for source in self.rows:
                if source.get('record') and _record_line_id(source['record']) == line_id:
                    source['character'] = character
            self.mapping_model.set_rows([source for source in self.rows if source.get('record')])
            self.refresh_mappings()
            self.changed.emit()
            self.status.setText('Atribuição manual guardada para %s.' % line_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Personagem manual', str(exc))

    def refresh_references(self, *_):
        query = self.reference_search.text().casefold().strip()
        self.reference_list.clear()
        for character, reference in self.references_data:
            searchable = ' '.join((character, reference.get('name', ''), reference.get('transcript', ''), reference.get('path', ''))).casefold()
            if query and query not in searchable:
                continue
            item = QListWidgetItem('%s · %s · %s' % (character, reference.get('name') or Path(reference.get('path', '')).name,
                                                       reference.get('transcript') or 'sem transcrição'))
            item.setData(Qt.ItemDataRole.UserRole, (character, reference))
            self.reference_list.addItem(item)

    def _selected_reference(self):
        item = self.reference_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def play_reference(self):
        selected = self._selected_reference()
        if not selected:
            return
        _, reference = selected
        try:
            path = self.context.review_store.resolve_path(reference['path'])
            self.context.player.play(path)
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText('Referência indisponível: %s' % exc)

    def reveal_reference(self):
        selected = self._selected_reference()
        if not selected:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        try:
            path = self.context.review_store.resolve_path(selected[1]['path'])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText('Referência indisponível: %s' % exc)


def create_library_dialog(key, context, parent=None, records=None):
    """Create the native replacement for one of the classic library windows."""
    if key == 'audio_library':
        return AudioLibraryDialog(context, records=records, parent=parent)
    if key == 'voice_identity':
        return VoiceIdentityDialog(context, records=records, parent=parent)
    raise ValueError('Unknown library dialog: %s' % key)
