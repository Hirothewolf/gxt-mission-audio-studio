"""Virtual, provenance-preserving editor for physical SPC NPC recordings."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
                            QThread, Qt, Signal, Slot, QSignalBlocker)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QTableView, QPlainTextEdit, QProgressBar, QSplitter, QComboBox,
    QAbstractItemView, QHeaderView, QGroupBox)

from studio.projects.review import text_hash
from studio.core.utils import json_atomico


RECORD_ROLE = Qt.ItemDataRole.UserRole + 41
HEADERS = ('ID físico', 'Arquivo', 'Banco', 'Duração', 'Transcrição original (ASR)', 'Texto editado', 'Estado')


def _search_text(value):
    return str(value or '').casefold()


def _display_status(record):
    if record.get('status') == 'NO_SPEECH_DETECTED':
        return 'Sem fala detectada'
    language = record.get('asr_language') or 'idioma ?'
    confidence = record.get('asr_language_probability')
    if confidence is not None and float(confidence) < .7:
        return '%s · baixa confiança' % language
    if language != 'en':
        return '%s · revisar idioma' % language
    return 'Rascunho ASR · revisar'


class NpcTableModel(QAbstractTableModel):
    """Small virtual model; rows retain the catalog's immutable physical IDs."""
    def __init__(self, records=(), text_provider=None, parent=None):
        super().__init__(parent)
        self._records = tuple(records)
        self._row_by_id = {r.get('stable_audio_id'): i for i, r in enumerate(self._records)}
        self._text_provider = text_provider or (lambda r: r.get('reference_text', ''))
        self._search_cache = tuple(self._cache_record(row) for row in self._records)
        self._sort_values = tuple((str(r.get('stable_audio_id', '')), str(r.get('archive', '')),
            int(r.get('bank_index', 0)), float(r.get('duration_seconds') or 0),
            _search_text(r.get('reference_text', '')), _search_text(self._text_provider(r)),
            _search_text(_display_status(r))) for r in self._records)

    def set_records(self, records):
        self.beginResetModel()
        self._records = tuple(records)
        self._row_by_id = {r.get('stable_audio_id'): i for i, r in enumerate(self._records)}
        self._search_cache = tuple(self._cache_record(row) for row in self._records)
        self._sort_values = tuple((str(r.get('stable_audio_id', '')), str(r.get('archive', '')),
            int(r.get('bank_index', 0)), float(r.get('duration_seconds') or 0),
            _search_text(r.get('reference_text', '')), _search_text(self._text_provider(r)),
            _search_text(_display_status(r))) for r in self._records)
        self.endResetModel()

    def refresh_text(self, stable_audio_id):
        row = self._row_by_id.get(stable_audio_id)
        if row is None:
            return
        self._search_cache = self._search_cache[:row] + (self._cache_record(self._records[row]),) + self._search_cache[row + 1:]
        values = list(self._sort_values[row])
        values[5] = _search_text(self._text_provider(self._records[row]))
        self._sort_values = self._sort_values[:row] + (tuple(values),) + self._sort_values[row + 1:]
        self.dataChanged.emit(self.index(row, 5), self.index(row, 5))

    def _cache_record(self, record):
        return _search_text(' '.join([str(record.get(key, '')) for key in (
            'stable_audio_id', 'archive', 'bank_index', 'sound_index', 'reference_text',
            'text', 'status', 'asr_language', 'asr_language_probability', 'scope')] +
            [str(self._text_provider(record))]))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def record_at(self, row):
        return self._records[row]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._records):
            return None
        record = self._records[index.row()]
        if role == RECORD_ROLE:
            return record
        if role == Qt.ItemDataRole.ToolTipRole and index.column() in (4, 6):
            language = record.get('asr_language') or 'não identificado'
            probability = record.get('asr_language_probability')
            confidence = '—' if probability is None else '%.1f%%' % (float(probability) * 100)
            return 'Idioma ASR: %s · confiança: %s · estado: %s' % (
                language, confidence, _display_status(record))
        if role == Qt.ItemDataRole.UserRole:
            return self._sort_values[index.row()][index.column()]
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (record.get('stable_audio_id', ''), record.get('archive', ''),
            'B%03d / S%03d · índice' % (int(record.get('bank_index', 0)), int(record.get('sound_index', 0))),
            '%.2f s' % float(record.get('duration_seconds') or 0),
            record.get('reference_text', ''),
            self._text_provider(record), _display_status(record))
        return values[index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section]
        return super().headerData(section, orientation, role)

    def sort_key(self, row, column):
        return self._sort_values[row][column]


class _NpcProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ''
        self.archive = None
        self.language = 'en'
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        record = model.record_at(source_row)
        if self.archive and record.get('archive') != self.archive:
            return False
        status = record.get('status')
        language = record.get('asr_language')
        confidence = float(record.get('asr_language_probability') or 0)
        if self.language == 'en' and not (status == 'ASR_DRAFT' and language == 'en' and confidence >= .7):
            return False
        if self.language == 'other' and not (status == 'ASR_DRAFT' and language != 'en' and confidence >= .7):
            return False
        if self.language == 'low' and not (status == 'ASR_DRAFT' and confidence < .7):
            return False
        if self.language == 'no_speech' and status != 'NO_SPEECH_DETECTED':
            return False
        query = self.query
        return not query or query in model._search_cache[source_row]

    def record_at(self, row):
        return self.data(self.index(row, 0), RECORD_ROLE)

    def lessThan(self, left, right):
        model = self.sourceModel()
        return model.sort_key(left.row(), left.column()) < model.sort_key(right.row(), right.column())


class _LoadWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, project_root, loader=None, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.loader = loader
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            self.progress.emit('Validando catálogo físico SPC…')
            loader = self.loader
            if loader is None:
                from studio.audio.npc_catalog import load_verified_catalog
                loader = load_verified_catalog
            catalog = loader(self.project_root)
            if self.cancel_event.is_set():
                return
            self.loaded.emit(catalog)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class _SaveWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, context, record, signature, parent=None):
        super().__init__(parent)
        self.context = context
        self.record = copy.deepcopy(record)
        self.signature = copy.deepcopy(signature)

    def run(self):
        try:
            line_id = self.signature['line_id']
            candidate_id = self.signature['candidate_id']
            if self.record.get('line_id') != line_id or self.record.get('text') != self.signature['text']:
                raise ValueError('A fala ou o texto mudaram desde a escolha do take.')
            candidate = self.context.review_store.validate_candidate_source(line_id, candidate_id)
            if (candidate.get('sha256') != self.signature['sha256'] or
                    candidate.get('text_hash') != self.signature['text_hash'] or
                    candidate.get('text') != self.signature['text']):
                raise ValueError('O take mudou desde a seleção; áudio existente preservado.')
            if self.context.game_audio._game_is_running():
                raise ValueError('Feche o GTA antes de aplicar o áudio NPC. Nenhum áudio foi instalado.')
            self.context.save_text(self.record, self.signature['text'])
            self.context.review_store.set_status(line_id, candidate_id, 'approved', self.signature['text'],
                duration_override=bool(candidate.get('metadata', {}).get('duration_override_required')))
            if self.context.game_audio._game_is_running():
                raise ValueError('O GTA abriu antes de aplicar o áudio. A instalação foi cancelada.')
            result = self.context.install([self.record], self.progress.emit)
            self.done.emit(dict(line_id=line_id, candidate_id=candidate_id, install=result))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class NpcWorkspace(QWidget):
    summaryChanged = Signal(dict)
    recordSelected = Signal(object)
    operationChanged = Signal(bool)
    errorRaised = Signal(str)

    def __init__(self, base_context, parent=None, *, catalogue=None, context_factory=None):
        super().__init__(parent)
        self.base_context = base_context
        self.project_root = Path(base_context.project_root).resolve()
        self.context = None
        self.catalogue = None
        self._context_factory = context_factory
        self._catalogue_fixture = catalogue
        self._load_worker = None
        self._save_worker = None
        self._loading = False
        self._saving = False
        self._selected_id = None
        self._reconciling_selection = False
        self._candidate = None
        self._candidate_signature = None
        self._candidate_choices = {}
        self._candidate_choice_path = self.project_root / 'workspace/npc_selected_candidates.json'
        self._choice_load_error = None
        self._records = ()
        self._record_by_id = {}
        self._source_row_by_id = {}
        self._generation = None

        outer = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Buscar ID, arquivo, ASR ou texto…')
        self.archive_combo = QComboBox()
        self.archive_combo.addItem('Todos os arquivos', None)
        self.language_combo = QComboBox()
        self.language_combo.addItem('Inglês (padrão)', 'en')
        self.language_combo.addItem('Outros idiomas', 'other')
        self.language_combo.addItem('Baixa confiança', 'low')
        self.language_combo.addItem('Sem fala detectada', 'no_speech')
        self.language_combo.addItem('Todas as falas', 'all')
        self.status_label = QLabel('Catálogo NPC ainda não carregado.')
        filters.addWidget(self.search, 2)
        filters.addWidget(self.archive_combo)
        filters.addWidget(self.language_combo)
        filters.addWidget(self.status_label, 2)
        outer.addLayout(filters)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.model = NpcTableModel(text_provider=self._text_for_model, parent=self)
        self.proxy = _NpcProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.table)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        self.selection_label = QLabel('Selecione uma gravação SPC para revisar.')
        self.selection_label.setWordWrap(True)
        editor_layout.addWidget(self.selection_label)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('Texto-alvo revisado…')
        self.editor.setMaximumHeight(100)
        editor_layout.addWidget(self.editor)

        controls = QHBoxLayout()
        self.play_original_button = QPushButton('Ouvir original')
        self.play_generated_button = QPushButton('Ouvir take selecionado')
        self.play_installed_button = QPushButton('Ouvir no mod')
        self.compare_button = QPushButton('A/B')
        self.save_button = QPushButton('Salvar texto + aplicar áudio')
        self.generate_button = QPushButton('Gerar take')
        self.save_text_button = QPushButton('Salvar só texto')
        for button in (self.play_original_button, self.play_generated_button,
                       self.play_installed_button, self.compare_button,
                       self.generate_button, self.save_text_button, self.save_button):
            controls.addWidget(button)
        editor_layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        editor_layout.addWidget(self.progress)
        self.detail_label = QLabel('Áudios de origem, take e mod são mantidos separados.')
        self.detail_label.setWordWrap(True)
        editor_layout.addWidget(self.detail_label)
        self.generation_host = QGroupBox('Geração com referência física original')
        self.generation_layout = QVBoxLayout(self.generation_host)
        editor_layout.addWidget(self.generation_host)
        split.addWidget(editor_panel)
        split.setSizes([500, 300])
        outer.addWidget(split, 1)

        self.search.textChanged.connect(self.set_filter)
        self.editor.textChanged.connect(self._editor_changed)
        self.archive_combo.currentIndexChanged.connect(lambda i: self.set_archive(self.archive_combo.itemData(i)))
        self.language_combo.currentIndexChanged.connect(lambda i: self.set_language(self.language_combo.itemData(i)))
        self.table.selectionModel().currentRowChanged.connect(self._selection_changed)
        self.play_original_button.clicked.connect(lambda: self.play('original'))
        self.play_generated_button.clicked.connect(lambda: self.play('approved'))
        self.play_installed_button.clicked.connect(lambda: self.play('installed'))
        self.compare_button.clicked.connect(self.compare_audio)
        self.save_button.clicked.connect(self.save)
        self.save_text_button.clicked.connect(self.save_text)
        self.generate_button.clicked.connect(self.generate)
        self._set_controls(False)
        if catalogue is not None:
            self._accept_catalogue(catalogue)

    def _text_for_model(self, row):
        if self._selected_id == row.get('stable_audio_id'):
            return self.editor.toPlainText()
        if self.context:
            try:
                return self.context.current_text(self._make_record(row))
            except Exception:
                pass
        return row.get('reference_text', '')

    def _make_record(self, row):
        sid = row['stable_audio_id']
        record = dict(row, kind='npc', line_id='NPC|' + sid)
        if self.context:
            record['text'] = self.context.current_text(record)
        else:
            record['text'] = row.get('reference_text', '')
        if sid == self._selected_id:
            record['text'] = self.editor.toPlainText()
        return record

    def _stale_edit_reason(self, record):
        stale = getattr(self.context, 'stale_edits', {}) if self.context else {}
        return stale.get(record.get('stable_audio_id')) if isinstance(stale, dict) else None

    def _editor_changed(self):
        if self._selected_id:
            self.model.refresh_text(self._selected_id)
        if self._candidate_signature:
            matches = self.editor.toPlainText() == self._candidate_signature['text']
            self.save_button.setEnabled(matches and not self.operation_active())
            if not matches:
                self._forget_candidate_choice(self._selected_id)
                self.status_label.setText('Texto alterado; o take selecionado não pode ser aplicado a esta versão.')
            elif self._selected_id not in self._candidate_choices:
                signature = self._candidate_signature
                choices = copy.deepcopy(self._candidate_choices)
                choices[self._selected_id] = dict(line_id=signature['line_id'],
                    candidate_id=signature['candidate_id'], sha256=signature['sha256'],
                    text=signature['text'], text_hash=signature['text_hash'])
                try:
                    self._write_candidate_choices(choices)
                except Exception as exc:
                    self.status_label.setText('Take selecionado só nesta sessão; seleção não persistida: ' + str(exc))

    def _set_controls(self, enabled):
        for control in (self.table, self.editor, self.play_original_button, self.play_generated_button,
                        self.play_installed_button, self.compare_button, self.generate_button,
                        self.save_text_button, self.save_button, self.search, self.archive_combo,
                        self.language_combo):
            control.setEnabled(enabled)
        self.save_button.setEnabled(enabled and self._candidate is not None and not self._saving)

    def load(self):
        """Load and verify the catalog without blocking the GUI; safe to call more than once."""
        if self._loading or self.catalogue is not None:
            return self._load_worker
        self._loading = True
        self.operationChanged.emit(True)
        self.progress.show()
        self.status_label.setText('Validando catálogo NPC…')
        self._load_worker = _LoadWorker(self.project_root, parent=self)
        self._load_worker.loaded.connect(self._accept_catalogue)
        self._load_worker.failed.connect(self._load_failed)
        self._load_worker.progress.connect(self.status_label.setText)
        self._load_worker.finished.connect(self._load_finished)
        self._load_worker.start()
        return self._load_worker

    @Slot(object)
    def _accept_catalogue(self, catalogue):
        try:
            factory = self._context_factory
            if factory is None:
                from .npc_context import NpcContext
                factory = NpcContext
            context = factory(self.base_context, catalogue)
            records = tuple(r for r in catalogue.npc_records if r.get('scope') != 'PLAYER_ROUTE_SEPARATE')
            self.catalogue = catalogue
            self.context = context
            self._records = records
            self._record_by_id = {r['stable_audio_id']: r for r in records}
            self._source_row_by_id = {r['stable_audio_id']: i for i, r in enumerate(records)}
            self._load_candidate_choices()
            self.model._text_provider = self._text_for_model
            self.model.set_records(self._records)
            archives = sorted({str(r.get('archive', '')) for r in self._records})
            self.archive_combo.blockSignals(True)
            self.archive_combo.clear()
            self.archive_combo.addItem('Todos os arquivos', None)
            for archive in archives:
                self.archive_combo.addItem(archive, archive)
            self.archive_combo.blockSignals(False)
            summary = dict(getattr(catalogue, 'summary', {}))
            summary.setdefault('loaded_npc_count', len(self._records))
            self.status_label.setText('%s NPCs físicos · %s arquivos' %
                (len(self._records), len(archives)))
            if self._choice_load_error:
                self.detail_label.setText('Seleção de take não restaurada; sidecar preservado: ' + self._choice_load_error)
            self.summaryChanged.emit(summary)
            self._set_controls(True)
            self._select_first_visible()
            if self._choice_load_error:
                self.status_label.setText('Seleção de take não restaurada; sidecar preservado.')
        except Exception as exc:
            self._load_failed(str(exc))

    def _load_failed(self, message):
        self.status_label.setText('Falha ao carregar catálogo NPC.')
        self.detail_label.setText(message)
        self.errorRaised.emit(message)

    def _load_finished(self):
        self._loading = False
        self.progress.hide()
        self.operationChanged.emit(self.operation_active())

    def cancel_load(self):
        if self._load_worker and self._load_worker.isRunning():
            self._load_worker.cancel()
            self.status_label.setText('Cancelamento solicitado; a validação terminará sem atualizar a lista.')

    def closeEvent(self, event):
        if self.operation_active():
            self.cancel_load()
            if self._generation and self._generation.busy:
                self._generation.cancel()
            event.ignore()
            self.status_label.setText('Operação em andamento; feche após ela terminar.')
            return
        if self.has_dirty_draft() and not self.flush_text():
            event.ignore()
            self.status_label.setText('O rascunho NPC não pôde ser salvo; a aba continua aberta.')
            return
        event.accept()

    def operation_active(self):
        return bool(self._loading or self._saving or (self._generation and self._generation.busy))

    def set_filter(self, text):
        if not self._flush_current_draft():
            return False
        return self._apply_filter_change(query=_search_text(text).strip())

    def set_archive(self, archive):
        if not self._flush_current_draft():
            return False
        return self._apply_filter_change(archive=archive or None)

    def set_language(self, language):
        if not self._flush_current_draft():
            return False
        return self._apply_filter_change(language=language or 'en')

    def _apply_filter_change(self, *, query=None, archive=Ellipsis, language=None):
        """Change proxy filters, then make the physical selection match the visible row."""
        previous_id = self._selected_id
        selection = self.table.selectionModel()
        blocker = QSignalBlocker(selection)
        self._reconciling_selection = True
        self.proxy.beginFilterChange()
        if query is not None:
            self.proxy.query = query
        if archive is not Ellipsis:
            self.proxy.archive = archive
        if language is not None:
            self.proxy.language = language
        self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        target = self._proxy_index_for_id(previous_id) if previous_id else QModelIndex()
        if not target.isValid() and self.proxy.rowCount():
            target = self.proxy.index(0, 0)
        selection.clear()
        if target.isValid():
            self.table.setCurrentIndex(target)
            self.table.selectRow(target.row())
        del blocker
        self._reconciling_selection = False
        if target.isValid():
            row = self.proxy.data(target, RECORD_ROLE)
            if row:
                self._show_record(row)
                return True
        self._clear_active_record()
        return True

    def _select_first_visible(self):
        return self._apply_filter_change()

    def _clear_active_record(self):
        self._selected_id = None
        self._candidate = None
        self._candidate_signature = None
        self.save_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self.editor.setEnabled(False)
        for control in (self.play_original_button, self.play_generated_button,
                        self.play_installed_button, self.compare_button, self.save_text_button):
            control.setEnabled(False)
        self.selection_label.setText('Nenhuma fala visível')
        self.detail_label.setText('Ajuste os filtros para exibir falas NPC.')
        self.recordSelected.emit(None)

    def _flush_current_draft(self):
        sid = self._selected_id
        row = self._record_by_id.get(sid) if sid else None
        if not row or not self.context:
            return True
        text = self.editor.toPlainText()
        record = self._make_record(row)
        try:
            current_text = self.context.current_text(record)
            if self._stale_edit_reason(record):
                if text != current_text:
                    self.status_label.setText(
                        'Texto editado sobre uma origem desatualizada; use “Salvar só texto” antes de navegar.')
                    return False
                # The existing stale binding is saved already. Navigation must not
                # silently rebind it to a changed source; only explicit Save may.
                return True
            if text != current_text:
                self.context.save_text(record, text)
                self.model.refresh_text(sid)
            return True
        except Exception as exc:
            self.status_label.setText('Rascunho NPC não salvo; a seleção foi mantida.')
            self.errorRaised.emit(str(exc))
            return False

    def _proxy_index_for_id(self, sid):
        source_row = self._source_row_by_id.get(sid)
        if source_row is None:
            return QModelIndex()
        return self.proxy.mapFromSource(self.model.index(source_row, 0))

    def select_record(self, stable_audio_id):
        record = self._record_by_id.get(stable_audio_id)
        if not record:
            return False
        if stable_audio_id != self._selected_id and not self._flush_current_draft():
            return False
        idx = self._proxy_index_for_id(stable_audio_id)
        if not idx.isValid():
            self._apply_filter_change(query='', archive=None, language='all')
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            archive_index = self.archive_combo.findData(None)
            if archive_index >= 0:
                self.archive_combo.blockSignals(True)
                self.archive_combo.setCurrentIndex(archive_index)
                self.archive_combo.blockSignals(False)
            all_index = self.language_combo.findData('all')
            if all_index >= 0:
                self.language_combo.blockSignals(True)
                self.language_combo.setCurrentIndex(all_index)
                self.language_combo.blockSignals(False)
            idx = self._proxy_index_for_id(stable_audio_id)
        if not idx.isValid() or self.proxy.data(idx, RECORD_ROLE).get('stable_audio_id') != stable_audio_id:
            return False
        self._reconciling_selection = True
        blocker = QSignalBlocker(self.table.selectionModel())
        self.table.selectionModel().clear()
        self.table.setCurrentIndex(idx)
        self.table.selectRow(idx.row())
        del blocker
        self._reconciling_selection = False
        self._show_record(record)
        return True

    def _selection_changed(self, current, previous):
        if self._reconciling_selection:
            return
        record = self.proxy.data(current, RECORD_ROLE) if current.isValid() else None
        if not record:
            return
        sid = record['stable_audio_id']
        if sid != self._selected_id and not self._flush_current_draft():
            old = self._proxy_index_for_id(self._selected_id) if self._selected_id else QModelIndex()
            if old.isValid():
                self._reconciling_selection = True
                blocker = QSignalBlocker(self.table.selectionModel())
                self.table.setCurrentIndex(old)
                self.table.selectRow(old.row())
                del blocker
                self._reconciling_selection = False
            return
        if sid != self._selected_id:
            self._show_record(record)

    def _show_record(self, row):
        record = self._make_record(row)
        self._selected_id = row['stable_audio_id']
        self._candidate = None
        self._candidate_signature = None
        self.save_button.setEnabled(False)
        self.editor.blockSignals(True)
        self.editor.setPlainText(record['text'])
        self.editor.blockSignals(False)
        self.selection_label.setText('%s · %s · banco índice %d / som índice %d · %.2f s' % (
            record['stable_audio_id'], record.get('archive', '—'), int(record.get('bank_index', 0)),
            int(record.get('sound_index', 0)), float(record.get('duration_seconds') or 0)))
        self.detail_label.setText('%s · idioma %s · confiança %s · falante não identificado' % (
            _display_status(record), record.get('asr_language') or '—',
            '—' if record.get('asr_language_probability') is None else
            ('%.0f%%' % (float(record['asr_language_probability']) * 100))))
        try:
            mod = self.context.selected_mod()
            self.detail_label.setText(self.detail_label.text() + ' · destino: ' +
                (str(mod) if mod else 'destino não definido'))
        except Exception:
            pass
        stale_reason = self._stale_edit_reason(record)
        changed = getattr(self.context, 'catalog_changed_edits', set()) if self.context else set()
        if stale_reason:
            self.detail_label.setText(self.detail_label.text() + ' · EDIÇÃO DESATUALIZADA: ' + str(stale_reason))
        elif record['stable_audio_id'] in changed:
            self.detail_label.setText(self.detail_label.text() + ' · catálogo atualizado; origem igual confirmada')
        can_generate = (record.get('status') == 'ASR_DRAFT' and
            bool(record.get('reference_text', '').strip()) and not stale_reason)
        self.generate_button.setEnabled(can_generate and not self.operation_active())
        self.editor.setEnabled(True)
        for control in (self.play_original_button, self.play_generated_button,
                        self.play_installed_button, self.compare_button, self.save_text_button):
            control.setEnabled(not self.operation_active())
        self._mount_generation(record)
        self._restore_candidate(record)
        self.recordSelected.emit(record)

    def selected_record(self):
        row = self._record_by_id.get(self._selected_id)
        return self._make_record(row) if row else None

    def selected_records(self):
        """Return explicitly selected physical rows; fall back to current only."""
        indexes = self.table.selectionModel().selectedRows(0)
        records = []
        for proxy_index in indexes:
            physical = self.proxy.data(proxy_index, RECORD_ROLE)
            if not physical:
                continue
            record = self._make_record(physical)
            if self.context:
                record['info'] = self.context.info(record)
            records.append(record)
        if not records:
            record = self.selected_record()
            if record:
                if self.context:
                    record['info'] = self.context.info(record)
                records.append(record)
        return records

    def has_dirty_draft(self):
        record = self.selected_record()
        return bool(record and self.context and
                    self.editor.toPlainText() != self.context.current_text(record))

    def flush_text(self):
        """Persist the current text draft only; never approves or installs audio."""
        return self.save_text() if self.has_dirty_draft() else True

    def _load_candidate_choices(self):
        if not self._candidate_choice_path.is_file():
            self._candidate_choices = {}
            return
        try:
            data = json.loads(self._candidate_choice_path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.get('version') != 1 or data.get('scope') != 'NPC' or
                    not isinstance(data.get('selected'), dict)):
                raise ValueError('Sidecar de takes selecionados inválido.')
            choices = {}
            for sid, choice in data['selected'].items():
                if (not isinstance(sid, str) or not isinstance(choice, dict) or
                        choice.get('line_id') != 'NPC|' + sid or
                        not all(isinstance(choice.get(key), str) for key in
                                ('candidate_id', 'sha256', 'text', 'text_hash')) or
                        choice['text_hash'] != text_hash(choice['text'])):
                    continue
                choices[sid] = dict(choice)
            self._candidate_choices = choices
        except (OSError, ValueError, TypeError) as exc:
            self._candidate_choices = {}
            self._choice_load_error = str(exc)

    def _write_candidate_choices(self, choices):
        if self._choice_load_error:
            raise ValueError('Sidecar de seleção inválido foi preservado; corrija-o antes de selecionar outro take.')
        payload = {'version': 1, 'scope': 'NPC', 'selected': choices}
        json_atomico(self._candidate_choice_path, payload)
        self._candidate_choices = copy.deepcopy(choices)

    def _forget_candidate_choice(self, stable_id):
        if stable_id not in self._candidate_choices:
            return
        choices = copy.deepcopy(self._candidate_choices)
        choices.pop(stable_id, None)
        self._write_candidate_choices(choices)

    def _restore_candidate(self, record):
        choice = self._candidate_choices.get(record['stable_audio_id'])
        if not choice or choice['text'] != self.editor.toPlainText():
            return False
        try:
            line_id = 'NPC|' + record['stable_audio_id']
            candidate = self.context.review_store.validate_candidate_source(line_id, choice['candidate_id'])
            if (candidate.get('sha256') != choice['sha256'] or
                    candidate.get('text_hash') != choice['text_hash'] or
                    candidate.get('text') != choice['text']):
                return False
            return self.select_candidate(dict(candidate, line_id=line_id))
        except Exception:
            self.status_label.setText('O take selecionado salvo não passou pela validação; nenhum áudio foi aplicado.')
            return False

    def accept_generated(self, items):
        """Attach batch candidates to NPC physical IDs and select them for review."""
        accepted = []
        choices = copy.deepcopy(self._candidate_choices)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            record, candidate = item.get('record'), item.get('candidate')
            if not isinstance(record, dict) or not isinstance(candidate, dict):
                continue
            sid = record.get('stable_audio_id')
            line_id = 'NPC|' + str(sid or '')
            if (sid not in self._record_by_id or record.get('line_id') != line_id or
                    candidate.get('id') is None or candidate.get('sha256') is None or
                    candidate.get('text_hash') != text_hash(record.get('text', '')) or
                    candidate.get('text') != record.get('text')):
                continue
            bound = dict(candidate, line_id=line_id)
            choices[sid] = dict(line_id=line_id, candidate_id=str(candidate['id']),
                sha256=str(candidate['sha256']), text=record['text'], text_hash=candidate['text_hash'])
            accepted.append(dict(record=record, candidate=bound))
        if not accepted:
            return 0
        try:
            self._write_candidate_choices(choices)
        except Exception as exc:
            self.status_label.setText('Takes gerados preservados, mas não foi possível salvar a seleção: ' + str(exc))
            self.errorRaised.emit(str(exc))
            return 0
        for item in accepted:
            if item['record']['stable_audio_id'] == self._selected_id:
                self.select_candidate(item['candidate'])
        self.status_label.setText('%d take(s) selecionado(s) para revisão; nenhum foi aplicado.' % len(accepted))
        return len(accepted)

    def _mount_generation(self, record):
        if self._generation and self._generation.busy:
            return
        if self._generation:
            self._generation.record = copy.deepcopy(record)
            self._generation.text_provider = self.editor.toPlainText
            available = (record.get('status') == 'ASR_DRAFT' and
                bool(record.get('reference_text', '').strip()) and not self._stale_edit_reason(record))
            self._generation.setVisible(available)
            self.generate_button.setEnabled(available)
            return
        while self.generation_layout.count():
            item = self.generation_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._generation = None
        if not record or not self.context:
            return
        if record.get('status') == 'NO_SPEECH_DETECTED' or not record.get('reference_text', '').strip():
            label = QLabel('Geração bloqueada: sem referência em inglês validada. A fala continua editável e pesquisável.')
            label.setWordWrap(True)
            self.generation_layout.addWidget(label)
            self.generate_button.setEnabled(False)
            return
        if self._stale_edit_reason(record):
            label = QLabel('Salve o texto revisado para vinculá-lo à origem atual antes de gerar ou aplicar áudio.')
            label.setWordWrap(True)
            self.generation_layout.addWidget(label)
            self.generate_button.setEnabled(False)
            return
        from .generation import GenerationWidget
        self._generation = GenerationWidget(self.context, record, self.editor.toPlainText, self.generation_host)
        omni_index = self._generation.settings.engine.findData('omnivoice')
        if omni_index >= 0:
            self._generation.settings.engine.setCurrentIndex(omni_index)
        self._generation.settings.engine.setEnabled(False)
        self._generation.candidateReady.connect(self._generation_candidate_ready)
        self._generation.busyChanged.connect(self._generation_busy_changed)
        self.generation_layout.addWidget(self._generation)
        self.generate_button.setEnabled(record.get('status') == 'ASR_DRAFT' and
            bool(record.get('reference_text', '').strip()) and not self._stale_edit_reason(record))

    def _generation_candidate_ready(self, candidate):
        # ReviewStore candidate rows are intentionally scoped by their lookup
        # key and do not repeat line_id; bind the result to the frozen physical
        # record that GenerationWidget used, never to the current view row.
        if self._generation and isinstance(candidate, dict):
            bound = dict(candidate, line_id=self._generation.record['line_id'])
            self.select_candidate(bound)

    def _generation_busy_changed(self, busy):
        if busy:
            self.table.setEnabled(False)
            self.save_button.setEnabled(False)
        else:
            self.table.setEnabled(True)
            record = self.selected_record()
            self.generate_button.setEnabled(bool(record and record.get('status') == 'ASR_DRAFT' and
                record.get('reference_text', '').strip() and not self._stale_edit_reason(record)))
            self.save_button.setEnabled(self._candidate is not None)
        self.operationChanged.emit(self.operation_active())

    def generate(self):
        record = self.selected_record()
        if not record:
            return False
        if record.get('status') == 'NO_SPEECH_DETECTED' or not record.get('reference_text', '').strip():
            self.status_label.setText('Geração bloqueada: esta fala não tem referência ASR validada.')
            return False
        if self._stale_edit_reason(record):
            self.status_label.setText('Salve o texto revisado para vinculá-lo à origem atual antes de gerar.')
            return False
        if not self._generation:
            self._mount_generation(record)
        self._generation.start()
        return True

    def select_candidate(self, candidate):
        record = self.selected_record()
        if not record or not isinstance(candidate, dict):
            return False
        try:
            line_id = 'NPC|' + record['stable_audio_id']
            if candidate.get('line_id') != line_id:
                raise ValueError('O take pertence a outra gravação SPC.')
            edited_text = self.editor.toPlainText()
            if candidate.get('text_hash') != text_hash(edited_text):
                raise ValueError('O take foi gerado para outro texto; gere novamente para o texto atual.')
            if candidate.get('text') not in (None, edited_text):
                raise ValueError('O take foi gerado para outro texto.')
            choices = copy.deepcopy(self._candidate_choices)
            choices[record['stable_audio_id']] = dict(line_id=line_id,
                candidate_id=str(candidate['id']), sha256=str(candidate['sha256']),
                text=edited_text, text_hash=candidate['text_hash'])
            self._write_candidate_choices(choices)
            self._candidate = copy.deepcopy(candidate)
            self._candidate_signature = dict(line_id=line_id, candidate_id=candidate['id'],
                sha256=candidate['sha256'], text_hash=candidate['text_hash'], text=edited_text)
            self.save_button.setEnabled(True)
            self.status_label.setText('Take selecionado: %s · ainda não aplicado ao mod.' % candidate['id'])
            return True
        except (KeyError, ValueError, TypeError) as exc:
            self._candidate = self._candidate_signature = None
            self.save_button.setEnabled(False)
            self.status_label.setText(str(exc))
            return False

    def _candidate_path(self, candidate):
        if not candidate:
            return None
        try:
            validated = self.context.review_store.validate_candidate_source(
                candidate['line_id'], candidate['id'])
            if validated.get('sha256') != candidate.get('sha256'):
                return None
            return self.context.review_store.resolve_path(validated['path'])
        except Exception:
            try:
                path = Path(candidate['path'])
                return path if path.is_file() else None
            except Exception:
                return None

    def play(self, kind='original'):
        record = self.selected_record()
        if not record or not self.context:
            return False
        try:
            if kind == 'original':
                path = self.context.original_path(record)
            elif kind == 'installed':
                path = self.context.installed_path(record)
            elif kind == 'approved':
                path = self._candidate_path(self._candidate)
                if not path:
                    path = self.context.approved_path(record['line_id'], self.editor.toPlainText())
            elif kind == 'take':
                path = self._candidate_path(self._candidate)
            else:
                raise ValueError('Fonte de áudio desconhecida.')
            if path:
                self.context.player.play(path)
                return True
            self.status_label.setText('Áudio indisponível para esta fonte.')
            return False
        except Exception as exc:
            self.status_label.setText(str(exc))
            return False

    def compare_audio(self):
        record = self.selected_record()
        if not record or not self.context:
            return False
        original = self.context.original_path(record)
        take = self._candidate_path(self._candidate)
        if not take:
            take = self.context.approved_path(record['line_id'], self.editor.toPlainText())
        if not take:
            take = self.context.installed_path(record)
        if not take:
            self.status_label.setText('A/B precisa do original e de um take ou áudio instalado.')
            return False
        self.context.player.compare([original, take])
        return True

    def save_text(self):
        record = self.selected_record()
        if not record or not self.context:
            return False
        try:
            # Explicit user action intentionally revalidates stale text bindings.
            self.context.save_text(record, self.editor.toPlainText())
            self.model.refresh_text(record['stable_audio_id'])
            self.status_label.setText('Texto NPC salvo; nenhum áudio foi aplicado.')
            row = self._record_by_id.get(record['stable_audio_id'])
            if row:
                can_generate = (row.get('status') == 'ASR_DRAFT' and
                    bool(row.get('reference_text', '').strip()) and
                    not self._stale_edit_reason(record))
                self.generate_button.setEnabled(can_generate and not self.operation_active())
            return True
        except Exception as exc:
            self.status_label.setText(str(exc))
            self.errorRaised.emit(str(exc))
            return False

    def save(self):
        record = self.selected_record()
        signature = self._candidate_signature
        if (record and signature and self._candidate and
                signature.get('line_id') == record.get('line_id') and
                signature.get('text') == self.editor.toPlainText() and
                not self._stale_edit_reason(record)):
            return self.save_text_audio()
        return self.save_text()

    def save_text_audio(self):
        record = self.selected_record()
        signature = self._candidate_signature
        if not record or not signature or not self._candidate:
            self.status_label.setText('Selecione um take gerado para este texto antes de salvar áudio.')
            return False
        text = self.editor.toPlainText()
        line_id = 'NPC|' + record['stable_audio_id']
        try:
            if self._stale_edit_reason(record):
                raise ValueError('Salve o texto revisado primeiro para vinculá-lo à origem atual; depois gere ou selecione um take novo.')
            if signature != dict(line_id=line_id, candidate_id=self._candidate.get('id'),
                    sha256=self._candidate.get('sha256'), text_hash=self._candidate.get('text_hash'), text=text):
                raise ValueError('O texto ou take selecionado mudou; escolha novamente um take para este texto.')
            if text_hash(text) != signature['text_hash']:
                raise ValueError('O take selecionado pertence a outro texto.')
            game_audio = self.context.game_audio
            if game_audio._game_is_running():
                raise ValueError('Feche o GTA antes de aplicar o áudio NPC. Nenhum áudio foi instalado.')
            final_record = dict(record, text=text, line_id=line_id)
            frozen_signature = copy.deepcopy(signature)
            self._saving = True
            self._set_operation_ui(True)
            self._save_worker = _SaveWorker(self.context, final_record, frozen_signature, self)
            self._save_worker.done.connect(self._save_done)
            self._save_worker.failed.connect(self._save_failed)
            self._save_worker.progress.connect(self._save_progress)
            self._save_worker.finished.connect(self._save_finished)
            self._save_worker.start()
            return True
        except Exception as exc:
            self.status_label.setText(str(exc))
            self.errorRaised.emit(str(exc))
            return False

    @Slot(object)
    def _save_done(self, result):
        result = result if isinstance(result, dict) else {}
        line_id = result.get('line_id')
        install_result = result.get('install', {})
        if line_id and line_id in install_result.get('installed', []):
            self._forget_candidate_choice(line_id[4:])
            self._candidate = self._candidate_signature = None
            self.save_button.setEnabled(False)
            self.status_label.setText('NPC salvo: texto e áudio selecionado aplicados.')
            return
        details = install_result.get('errors', []) or install_result.get('skipped', [])
        self.status_label.setText('Áudio preservado; aplicação não confirmada%s.' %
            (': ' + '; '.join(str(item) for item in details) if details else ''))

    def _save_failed(self, message):
        self.status_label.setText('Falha ao salvar NPC: ' + message)
        self.errorRaised.emit(message)

    def _save_progress(self, current, total, message):
        self.status_label.setText('%s · %d/%d' % (message, current, total))

    def _save_finished(self):
        self._saving = False
        self._set_operation_ui(False)
        self.operationChanged.emit(self.operation_active())

    def _set_operation_ui(self, active):
        self.progress.setVisible(active)
        self.table.setEnabled(not active)
        self.editor.setEnabled(not active)
        self.save_button.setEnabled(not active and self._candidate is not None)
        self.operationChanged.emit(self.operation_active())


__all__ = ['NpcWorkspace', 'NpcTableModel']
