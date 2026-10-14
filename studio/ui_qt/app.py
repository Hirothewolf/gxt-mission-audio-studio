"""Classic editor layout, implemented with Qt and the maintained studio backend."""
from pathlib import Path
import json
import traceback
import copy

from PySide6.QtCore import Qt, QTimer, QUrl, QItemSelectionModel
from PySide6.QtGui import QAction, QKeySequence, QDesktopServices
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QListWidget, QListWidgetItem, QLineEdit, QComboBox, QLabel,
    QTableView, QPlainTextEdit, QPushButton, QFileDialog, QMessageBox, QInputDialog,
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QProgressBar, QAbstractItemView, QMenu, QStackedWidget)

from studio import config
from studio.gxt.codec import decode_text
from studio.gxt.lint import lint_avisos
from studio.gxt.workspace import Workspace
from studio.projects.review import text_hash
from studio.script.mapping import consultar_script_info, ref_do_registro, recarregar_script_cache
from studio.core.utils import json_atomico
from .document import QtDocument
from .models import GxtTableModel, GxtSortFilterProxyModel
from .context import QtContext
from .tasks import Task
from .classic_filters import FILTERS, row_flags as classic_row_flags, accepts, prepare_filter_snapshot


def review_label(state, text):
    candidates = state.get('candidates', [])
    current = [take for take in candidates if take.get('text_hash') == text_hash(text)]
    if any(take.get('id') == state.get('approved') and take.get('status') == 'approved' for take in current):
        return 'Aprovado no projeto'
    if current:
        return 'AI · para ouvir'
    return 'Take desatualizado' if candidates else ''


class EditorWindow(QMainWindow):
    def __init__(self, project_root=None, restore=True):
        super().__init__()
        self.setWindowTitle('GXT Editor & Mission Audio Studio — Qt clássico')
        self.resize(1450, 900)
        self.document = QtDocument()
        self.context = QtContext(self.document, project_root, self)
        from .audio_save import PendingAudio
        self.pending_audio = PendingAudio(self.context.project_root)
        self.context.pending_audio = self.pending_audio
        self._audio_saving = False
        from .audio_sync import ExternalFileWatcher
        self.file_watcher = ExternalFileWatcher()
        if hasattr(self.context.review_store, 'path'):
            self.file_watcher.watch('review', self.context.review_store.path)
        self.workspace = Workspace(self.context.project_root / 'workspace/gxt_editor_workspace.json')
        self.tasks, self.dialogs = [], []
        self.all_rows, self.categories = [], {}
        self.pairs = None
        self.selected = None
        self._draft_text = ''
        self._loading = False
        self._resetting = False
        self._audio_presence = {}
        self._presence_task = None
        self._presence_rerun = False
        self._presence_closing = False
        self._audio_stamps = {}
        self.npc_workspace = None
        self._search_scope = 'gxt'
        self._gxt_search_query = ''
        self._make_ui()
        from .classic_actions import install_classic_actions
        install_classic_actions(self)
        self.context.player.status.connect(self.statusBar().showMessage)
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(3000)
        self.sync_timer.timeout.connect(self.sync_external)
        self.sync_timer.start()
        if restore:
            QTimer.singleShot(0, self.restore_session)

    def _make_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(7, 7, 7, 7)
        self.setCentralWidget(central)
        toolbar = QHBoxLayout()
        self.toolbar = toolbar
        layout.addLayout(toolbar)
        filters = QHBoxLayout()
        filters.addWidget(QLabel('Buscar:'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Texto, chave, hash, tabela ou ID de áudio')
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.everywhere = QCheckBox('em todas')
        filters.addWidget(self.everywhere)
        self.filter = QComboBox()
        self.filter.addItems(FILTERS[:16] + ('Com take',))
        filters.addWidget(self.filter)
        self.ignore_tags = QCheckBox('ignorar tags')
        filters.addWidget(self.ignore_tags)
        self.gxt_filter_buttons = []
        for label, callback in [('Sem dublagem', self.next_undubbed),
                                ('Próx. diferença', lambda: self.next_row('different', True))]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            filters.addWidget(button)
            self.gxt_filter_buttons.append(button)
        layout.addLayout(filters)
        self.path_label = QLabel('Abra um GXT. Textos, referências e takes usam os serviços do editor clássico.')
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        horizontal = QSplitter()
        layout.addWidget(horizontal, 1)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        self.nav_search = QLineEdit()
        self.nav_search.setPlaceholderText('Encontrar tabela / missão / categoria')
        side.addWidget(self.nav_search)
        self.navigation = QTabWidget()
        from .navigation import GroupedNavigation
        self.tables, self.missions = QListWidget(), GroupedNavigation()
        self.navigation.addTab(self.tables, 'Tabelas GXT')
        self.navigation.addTab(self.missions, 'Missões e categorias')
        self.npc_tab = QWidget()
        npc_side = QVBoxLayout(self.npc_tab)
        self.npc_summary = QLabel('Vozes de NPC por arquivo, banco e ID físico. O catálogo será carregado ao abrir esta aba.')
        self.npc_summary.setWordWrap(True)
        npc_side.addWidget(self.npc_summary)
        npc_refresh = QPushButton('Atualizar catálogo NPC')
        npc_refresh.clicked.connect(lambda: self.open_npc_workspace(reload=True))
        npc_side.addWidget(npc_refresh)
        npc_side.addStretch()
        self.navigation.addTab(self.npc_tab, 'NPCs')
        side.addWidget(self.navigation, 1)
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        side.addWidget(self.evidence)
        horizontal.addWidget(sidebar)
        right = QSplitter(Qt.Orientation.Vertical)
        self.content_pages = QStackedWidget()
        self.gxt_page = right
        self.content_pages.addWidget(right)
        horizontal.addWidget(self.content_pages)
        self.model = GxtTableModel(self)
        self.model.text_editor = self.edit_table_text
        self.proxy = GxtSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        from .table_edit import GxtTextDelegate
        self.table.setItemDelegateForColumn(self.model.TextColumn, GxtTextDelegate(self.table))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_table_menu)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        # Keep the familiar classic sequence while identities remain model roles.
        for destination, logical in enumerate((8, 4, 0, 5, 7, 6, 3, 2, 9, 1)):
            header.moveSection(header.visualIndex(logical), destination)
        right.addWidget(self.table)
        editing = QWidget()
        edit_layout = QVBoxLayout(editing)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        panels = QSplitter()
        original = QWidget()
        original_layout = QVBoxLayout(original)
        original_layout.setContentsMargins(0, 0, 0, 0)
        original_layout.addWidget(QLabel('Comparação (somente leitura)'))
        self.comparison = QPlainTextEdit()
        self.comparison.setReadOnly(True)
        original_layout.addWidget(self.comparison)
        panels.addWidget(original)
        current = QWidget()
        current_layout = QVBoxLayout(current)
        current_layout.setContentsMargins(0, 0, 0, 0)
        current_layout.addWidget(QLabel('Texto atual (editável)'))
        self.editor = QPlainTextEdit()
        current_layout.addWidget(self.editor)
        panels.addWidget(current)
        edit_layout.addWidget(panels, 1)
        self.lint = QLabel()
        self.lint.setWordWrap(True)
        edit_layout.addWidget(self.lint)
        text_bar = QHBoxLayout()
        for title, callback in [('Aplicar (Ctrl+Enter)', self.commit), ('Copiar comparação', self.copy_comparison),
                ('Restaurar salvo', self.restore_text), ('★ Favorita', lambda: self.mark('fav')),
                ('✓ Revisada', lambda: self.mark('rev')), ('Nota…', self.note)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            text_bar.addWidget(button)
        edit_layout.addLayout(text_bar)
        from .hypertranslate import HypertranslateBar
        translation_bar = QHBoxLayout()
        translation_bar.addWidget(QLabel('Hipertraduzir esta linha:'))
        self.hypertranslate = HypertranslateBar(self.context, self)
        self.hypertranslate.set_text_provider(self.hypertranslate_snapshot)
        self.hypertranslate.resultReady.connect(self.hypertranslate_result)
        self.hypertranslate.failed.connect(lambda message: self.statusBar().showMessage(message))
        translation_bar.addWidget(self.hypertranslate)
        translation_bar.addStretch(1)
        edit_layout.addLayout(translation_bar)
        audio_bar = QHBoxLayout()
        for title, callback in [('Original (F5)', lambda: self.play('original')),
                ('Aprovado (F6)', lambda: self.play('approved')), ('A/B (F7)', self.compare_audio),
                ('No mod (F8)', lambda: self.play('installed')), ('Parar', self.context.player.stop),
                ('Revisar / gerar fala…', self.review), ('Trecho na timeline…', self.timeline_selected)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            audio_bar.addWidget(button)
        edit_layout.addLayout(audio_bar)
        self.audio_label = QLabel('Selecione uma fala para consultar sua origem e revisão.')
        self.audio_label.setWordWrap(True)
        self.audio_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        edit_layout.addWidget(self.audio_label)
        right.addWidget(editing)
        horizontal.setSizes([285, 1150])
        right.setSizes([480, 300])
        self.busy = QProgressBar()
        self.busy.setMaximumWidth(150)
        self.busy.setRange(0, 0)
        self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)
        self.cancel_operation = QPushButton('Cancelar carregamento')
        self.cancel_operation.clicked.connect(self.cancel_tasks)
        self.cancel_operation.hide()
        self.statusBar().addPermanentWidget(self.cancel_operation)
        self.change_status = QLabel('Sem alterações pendentes')
        self.statusBar().addPermanentWidget(self.change_status)
        self.presence_status = QLabel()
        self.statusBar().addPermanentWidget(self.presence_status)
        self.presence_timer = QTimer(self)
        self.presence_timer.setSingleShot(True)
        self.presence_timer.setInterval(120)
        self.presence_timer.timeout.connect(self.start_audio_presence)
        self.search.textChanged.connect(self.search_changed)
        self.filter.currentTextChanged.connect(self.apply_filter)
        self.everywhere.toggled.connect(self.apply_filter)
        self.ignore_tags.toggled.connect(self.apply_filter)
        self.nav_search.textChanged.connect(self.filter_navigation)
        self.navigation_timer = QTimer(self)
        self.navigation_timer.setSingleShot(True)
        self.navigation_timer.setInterval(25)
        self.navigation_timer.timeout.connect(self.navigate)
        self.tables.currentItemChanged.connect(self.request_navigation)
        self.missions.categorySelected.connect(self.request_navigation)
        self.navigation.currentChanged.connect(self.request_navigation)
        self.table.selectionModel().currentChanged.connect(self.select)
        self.table.clicked.connect(self.activate_visible_row)
        self.table.activated.connect(self.activate_visible_row)
        self.editor.textChanged.connect(self.update_lint)

    def error(self, message):
        QMessageBox.warning(self, 'Não foi possível concluir', str(message))

    def work(self, function, success=None):
        task = Task(function, self)
        self.tasks.append(task)
        self.busy.show()
        self.cancel_operation.show()
        self.statusBar().showMessage('Carregando… você pode continuar consultando a lista.')
        task.progress.connect(self.statusBar().showMessage)
        task._failure_slot = lambda message: self.statusBar().showMessage(message) if task.cancel_event.is_set() else self.error(message)
        task.failed.connect(task._failure_slot)
        if success:
            task.succeeded.connect(success)
        task.finished.connect(lambda: self.task_done(task))
        task.start()
        return task

    def task_done(self, task):
        if task in self.tasks:
            self.tasks.remove(task)
        task.wait()
        task.setParent(None)
        task.deleteLater()
        self.busy.setVisible(bool(self.tasks))
        self.cancel_operation.setVisible(bool(self.tasks))

    def cancel_tasks(self):
        for task in self.tasks:
            task.cancel_event.set()
        self.statusBar().showMessage('Cancelando… os arquivos e resultados concluídos serão preservados.')

    def request_navigation(self, *args):
        if self.document.gxt or self.navigation.currentIndex() == 2:
            self.statusBar().showMessage('Carregando lista de textos e áudios…')
            self.navigation_timer.start()

    def restore_session(self):
        session = config.CFG.get('sessao', {})
        path = session.get('gxt')
        if path and Path(path).is_file():
            self.load(path, session.get('cmp'))

    def open_file(self):
        if not self.may_close_document():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Abrir GXT', '', 'GXT (*.gxt)')
        if path:
            self.load(path)

    def load(self, path, comparison=None):
        if self._loading:
            return
        self._loading = True
        def operation(progress, cancel):
            document = QtDocument()
            document.load(path)
            if cancel.is_set():
                raise InterruptedError('Abertura cancelada; documento atual preservado.')
            if comparison and Path(comparison).is_file():
                document.load_comparison(comparison)
            progress('Preparando chaves e navegação…')
            rows, navigation = document.rows(), document.navigation()
            if cancel.is_set():
                raise InterruptedError('Abertura cancelada; documento atual preservado.')
            return document, rows, navigation
        task = self.work(operation, self.loaded)
        task.finished.connect(lambda: setattr(self, '_loading', False))

    def loaded(self, result):
        self.document, self.all_rows, self.categories = result
        self.context.document = self.document
        from .audio_sync import detect_mod_from_gxt
        if hasattr(self.context, 'game_audio'):
            mod = detect_mod_from_gxt(self.document.path, self.context.game_audio)
            if mod:
                self.context.game_audio.cfg['modloader_mod'] = mod
                self.context.game_audio._ml_escaneado = False
        self.file_watcher.watch('gxt', self.document.path)
        config.CFG['recentes'] = [str(self.document.path)] + [p for p in config.CFG.get('recentes', []) if str(p) != str(self.document.path)][:9]
        self._csv_changes = []
        self.selected = None
        self.editor.clear()
        self.comparison.clear()
        self.tables.blockSignals(True)
        self.tables.clear()
        self.tables.addItem('Todas as tabelas')
        self.tables.item(0).setData(Qt.ItemDataRole.UserRole, None)
        for table in self.document.gxt.order:
            item = QListWidgetItem('%s · %s linhas' % (table, len(self.document.gxt.t[table]['hashes'])))
            item.setData(Qt.ItemDataRole.UserRole, table)
            self.tables.addItem(item)
        self.tables.setCurrentRow(0)
        self.tables.blockSignals(False)
        self.missions.blockSignals(True)
        self.missions.set_categories(self.categories)
        self.missions.select_category('all')
        self.missions.blockSignals(False)
        self._audio_presence.clear()
        self._audio_stamps = self.audio_source_stamps()
        self.path_label.setText('Editando: %s | Comparação: %s' % (self.document.path,
            self.document.comparison.path if self.document.comparison else 'nenhuma'))
        self.navigate()
        self.setWindowTitle('%s[*] — GXT Editor & Mission Audio Studio' % self.document.path.name)
        self.update_change_status()
        self.statusBar().showMessage('%d entradas carregadas; arquivo original preservado até salvar.' % len(self.all_rows))

    def compare_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Comparar GXT', '', 'GXT (*.gxt)')
        if path and self.document.gxt:
            try:
                if not self.commit():
                    return
                self.document.load_comparison(path)
                self.refresh_rows()
                self.path_label.setText('Editando: %s | Comparação: %s' % (self.document.path, path))
            except Exception as exc:
                self.error(exc)

    def sync_external(self):
        if self.tasks or self._audio_saving or any(getattr(d, 'operation_active', lambda: False)() for d in self.dialogs):
            return
        try:
            stamps = self.audio_source_stamps()
            if stamps != self._audio_stamps:
                self._audio_stamps = stamps
                self._audio_presence.clear()
                self.presence_timer.start()
                self.presence_status.setText('Áudio do mod alterado; atualizando…')
            if self.file_watcher.changed('review'):
                if not self.commit():
                    return
                from studio.projects.review import ReviewStore
                loaded = ReviewStore(self.context.project_root)
                self.context.review_store.data = loaded.data
                self.file_watcher.accept('review')
                for dialog in self.dialogs:
                    if getattr(dialog, 'store', None) is self.context.review_store:
                        dialog.refresh()
                self.refresh_rows() if self.document.gxt else None
                self.statusBar().showMessage('Takes do projeto atualizados a partir do disco.')
            if self.file_watcher.changed('gxt'):
                draft = bool(self.selected and self.editor.toPlainText() != self._draft_text)
                if self.document.dirty or draft or self.pending_audio.choices(self.document.path):
                    self.change_status.setText('GXT mudou fora desta janela; suas edições e escolhas foram preservadas.')
                elif self.document.path.is_file() and not self.dialogs:
                    self.load(self.document.path, self.document.comparison.path if self.document.comparison else None)
        except (ValueError, OSError) as exc:
            self.change_status.setText('Sincronização pendente: ' + str(exc))

    def audio_source_stamps(self):
        if not hasattr(self.context, 'game_audio'):
            return {}
        import hashlib
        game = self.context.game_audio
        mod = self.context.selected_mod()
        base = game.modloader_dir()
        if not base or not mod:
            return {}
        folder = base / mod
        paths = [folder / 'audio/streams' / name for name in ('BEATS', 'CUTSCENE')]
        paths += [folder / 'audio/SFX' / name for name in ('SCRIPT', 'SPC_EA', 'SPC_FA', 'SPC_GA', 'SPC_NA', 'SPC_PA')]
        paths += [folder / 'audio/CONFIG/BankLkup.dat',
                  self.context.project_root / 'workspace/modloader_manifest.json',
                  self.context.project_root / 'workspace/stream_installs' / (hashlib.sha256(mod.encode()).hexdigest()[:16] + '.json')]
        result = {}
        for path in paths:
            try:
                stat = path.stat()
                result[str(path)] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                result[str(path)] = None
        return result

    def navigate(self, *args):
        if self.navigation.currentIndex() == 2:
            if self.commit():
                self.switch_search_scope(True)
                self.open_npc_workspace()
            return
        self.switch_search_scope(False)
        if not self.document.gxt or not self.commit():
            return
        self.content_pages.setCurrentWidget(self.gxt_page)
        if self.navigation.currentIndex() == 0:
            item = self.tables.currentItem()
            table = item.data(Qt.ItemDataRole.UserRole) if item else None
            self.pairs = {(table, h) for h in self.document.gxt.t[table]['hashes']} if table else None
            self.evidence.setText('Chaves originais preservadas. A categoria não altera a identidade da fala.')
        else:
            category = self.categories.get(self.missions.selected_category_id(), {})
            self.pairs = set(self.missions.selected_member_ids())
            item = self.missions.currentItem()
            self.evidence.setText(category.get('evidence') or
                ('%s: união das categorias deste grupo.' % item.text(0) if item else 'Escolha um grupo ou categoria.'))
        self.apply_filter()

    def filter_navigation(self, text):
        for widget in (self.tables,):
            for index in range(widget.count()):
                widget.item(index).setHidden(text.casefold() not in widget.item(index).text().casefold())
        self.missions.set_filter(text)
        if self.npc_workspace:
            self.npc_workspace.set_filter(text)

    def open_npc_workspace(self, reload=False):
        if self.npc_workspace is None:
            from .npc_workspace import NpcWorkspace
            self.npc_workspace = NpcWorkspace(self.context, self)
            self.npc_workspace.summaryChanged.connect(lambda summary: self.npc_summary.setText(
                '%s vozes NPC · %s registros totais\n%s falas com outro idioma detectado; transcrições são rascunhos por ID físico.' %
                (summary.get('npc_count', 0), summary.get('total', 0), summary.get('non_english_speech', 0))))
            self.npc_workspace.summaryChanged.connect(lambda summary: self.statusBar().showMessage(
                '%s registros NPC carregados; selecione uma fala para editar ou ouvir.' % summary.get('loaded_npc_count', 0)))
            self.npc_workspace.errorRaised.connect(self.statusBar().showMessage)
            self.content_pages.addWidget(self.npc_workspace)
            self.npc_workspace.load()
        elif reload:
            self.npc_workspace.load()
        self.content_pages.setCurrentWidget(self.npc_workspace)
        if self.npc_workspace.catalogue is not None:
            self.statusBar().showMessage('Catálogo NPC carregado; filtros e audição disponíveis.')
        self.evidence.setText('NPCs usam IDs físicos dos arquivos SPC. Não são chaves GXT nem identidades de personagem confirmadas.')
        return self.npc_workspace

    def npc_active(self):
        return bool(self.npc_workspace and self.navigation.currentIndex() == 2)

    def switch_search_scope(self, npc):
        scope = 'npc' if npc else 'gxt'
        if self._search_scope != scope:
            if npc:
                self._gxt_search_query = self.search.text()
                query = self.npc_workspace.search.text() if self.npc_workspace else ''
            else:
                query = self._gxt_search_query
            blocked = self.search.blockSignals(True)
            self.search.setText(query)
            self.search.blockSignals(blocked)
            self._search_scope = scope
        self.search.setPlaceholderText('Texto, ASR ou ID físico NPC' if npc else
                                      'Texto, chave, hash, tabela ou ID de áudio')
        for widget in (self.everywhere, self.filter, self.ignore_tags, *self.gxt_filter_buttons):
            widget.setEnabled(not npc)
        self.presence_status.setVisible(not npc)

    def enrich(self, row):
        result = dict(row)
        result['line_id'] = '%s|%08x' % (row['table'], row['hash'])
        result['info'] = consultar_script_info(self.context.script_db, row['table'], row['hash'], row['name']) or {}
        result['ref'] = ref_do_registro(result['info'])
        return result

    def row_flags(self, row):
        return classic_row_flags(self, row)

    def search_changed(self, text):
        if self.npc_active():
            self.npc_workspace.search.setText(text)
            return
        if not self.document.gxt or not self.commit():
            return
        selected = (self.selected['table'], self.selected['hash']) if self.selected else None
        self._resetting = True
        try:
            self.proxy.set_filter_text(text)
            if selected:
                index = self.proxy.row_for_identity(*selected)
                if index >= 0:
                    self.table.setCurrentIndex(self.proxy.index(index, 0))
        finally:
            self._resetting = False
        self.statusBar().showMessage('%d/%d entradas visíveis' % (self.proxy.rowCount(), len(self.all_rows)))

    def apply_filter(self, *args):
        if not self.document.gxt:
            return
        if not self.commit():
            return
        prepare_filter_snapshot(self)
        self.proxy.set_ignore_tags(self.ignore_tags.isChecked() if hasattr(self, 'ignore_tags') else False)
        selected = (self.selected['table'], self.selected['hash']) if self.selected else None
        choices = self.pending_audio.choices(self.document.path) if hasattr(self, 'pending_audio') else {}
        rows = []
        for source in self.all_rows:
            if not self.everywhere.isChecked() and self.pairs is not None and (source['table'], source['hash']) not in self.pairs:
                continue
            row = dict(source)
            row['flags'] = self.row_flags(row)
            record = self.enrich(row)
            row['audio_label'] = record['info'].get('stable_audio_id') or ('Áudio mapeado' if record['info'] else '')
            state = self.context.review_store.data.get('lines', {}).get(record['line_id'], {})
            row['review_label'] = review_label(state, row['text'])
            choice = choices.get(record['line_id'])
            if choice and choice['record']['text'] == row['text']:
                row['review_label'] = 'Escolhido · salvar'
            row['note'] = self.workspace.get(record['line_id']).get('note', '')
            if hasattr(self, '_audio_presence'):
                self.add_audio_presence_labels(row, record)
            if not accepts(self.filter.currentText(), row):
                continue
            rows.append(row)
        self._resetting = True
        self.model.set_rows(rows)
        self.proxy.set_filter_text(self.search.text())
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 105)
        self.table.setColumnWidth(2, 430)
        self.table.setColumnWidth(3, 330)
        self.table.setColumnHidden(1, True)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 170)
        self.table.setColumnWidth(6, 155)
        self.table.setColumnWidth(7, 115)
        self.table.setColumnWidth(8, 90)
        self.table.setColumnWidth(9, 85)
        self._resetting = False
        if selected:
            index = self.proxy.row_for_identity(*selected)
            if index >= 0:
                self.table.setCurrentIndex(self.proxy.index(index, 0))
        self.statusBar().showMessage('%d/%d entradas visíveis' % (self.proxy.rowCount(), len(self.all_rows)))
        if hasattr(self, 'presence_timer'):
            self.presence_timer.start()

    def add_audio_presence_labels(self, row, record):
        cached = self._audio_presence.get(record['line_id'])
        if not cached or cached['text'] != row['text']:
            return
        original, installed, take = (cached.get(name, {}) for name in ('original', 'mod', 'take'))
        states = dict(available='Disponível', needs_extract='No banco; pronto para ouvir', not_found='Não encontrado',
                      stale='Arquivo alterado; atualizar prova', proveninstalled='Integrado com prova')
        state = installed.get('state', 'not_found')
        row['installed_label'] = states.get(state, 'Não encontrado')
        if installed.get('matches_take') is False:
            row['installed_label'] += ' · diferente do take'
        if original.get('state') in ('available', 'needs_extract', 'proveninstalled'):
            row['original_label'] = row.get('audio_label', '') + ' ✓'
        if take.get('path') and not row.get('review_label'):
            row['review_label'] = 'Gerado integrado'
            row['flags'].add('dub')
        row['audio_tooltip'] = '\n'.join('%s: %s' % (label, states.get(item.get('state'), 'Não encontrado'))
            for label, item in (('Original', original), ('Mod', installed), ('Gerado', take)))

    def start_audio_presence(self):
        if not callable(getattr(self.context, 'audio_evidence', None)) or not self.document.gxt or self.npc_active():
            return
        if self._presence_task is not None:
            self._presence_task.cancel()
            self._presence_rerun = True
            return
        records = [row for row in self.visible_records() if row.get('info')]
        if not records:
            return
        context = copy.copy(self.context)
        context.game_audio = copy.copy(self.context.game_audio)
        context.game_audio.cfg = dict(self.context.game_audio.cfg)
        context.current_text = lambda record: record['text']
        context._audio_record_info = dict(getattr(self.context, '_audio_record_info', {}))
        document, path = self.document, str(self.document.path)
        def operation(progress, cancel):
            results = {}
            for i, record in enumerate(records, 1):
                if cancel.is_set():
                    break
                try:
                    values = {source: context.audio_evidence(record, source) for source in ('original', 'mod', 'take')}
                    values['text'] = record['text']
                    results[record['line_id']] = values
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    results[record['line_id']] = dict(text=record['text'], error=str(exc))
                if i % 200 == 0:
                    progress('Áudios %d/%d' % (i, len(records)))
            return results
        task = Task(operation, self)
        self._presence_task = task
        self.presence_status.setText('Consultando áudios…')
        task.progress.connect(self.presence_status.setText)
        def ready(results):
            if self.document is not document or str(self.document.path) != path:
                return
            self._audio_presence.update(results)
            selected = self.table.selectionModel()
            blocked = selected.blockSignals(True)
            try:
                for index in range(self.model.rowCount()):
                    row = dict(self.model.row_at(index))
                    self.add_audio_presence_labels(row, self.enrich(row))
                    self.model.update_row(row)
            finally:
                selected.blockSignals(blocked)
        task.succeeded.connect(ready)
        task.failed.connect(lambda message: self.presence_status.setText('Áudios: consulta pendente'))
        def finished():
            task.wait()
            task.setParent(None)
            task.deleteLater()
            self._presence_task = None
            self.presence_status.setText('Fontes consultadas')
            if self._presence_closing:
                self._presence_closing = False
                self.close()
            elif self._presence_rerun:
                self._presence_rerun = False
                self.presence_timer.start()
        task.finished.connect(finished)
        task.start()

    def activate_visible_row(self, index):
        # A proxy filter can reuse row 0 while currentChanged was suppressed.
        # An explicit click/Enter must still select that row's real identity.
        if not index.isValid():
            return
        source = self.model.row_at(self.proxy.mapToSource(index).row())
        if source and (not self.selected or (source['table'], source['hash']) !=
                       (self.selected['table'], self.selected['hash'])):
            self.select(index)

    def edit_table_text(self, table, key_hash, text):
        try:
            self.document._encode_valid(text)
            if not self.commit():
                return False
            if not self.selected or (self.selected['table'], self.selected['hash']) != (table, key_hash):
                source = next(row for row in self.all_rows if (row['table'], row['hash']) == (table, key_hash))
                self.show_record(source)
            self.editor.setPlainText(text)
            if not self.commit():
                return False
            self.statusBar().showMessage('Texto alterado. Ctrl+S salva o GXT e os áudios escolhidos.')
            return True
        except (ValueError, UnicodeError, KeyError, StopIteration) as exc:
            self.statusBar().showMessage('O texto não foi alterado: ' + str(exc))
            return False

    def table_menu_for(self, index):
        if not index.isValid():
            return None
        row = self.model.row_at(self.proxy.mapToSource(index).row())
        if not row or not self.commit():
            return None
        selection = self.table.selectionModel()
        if selection.isRowSelected(index.row(), index.parent()):
            selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            self.table.setCurrentIndex(index)
        self.activate_visible_row(index)
        identity = (row['table'], row['hash'])
        menu = QMenu(self.table)
        def add(title, callback, enabled=True):
            action = menu.addAction(title)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False: callback())
        def edit():
            visible = self.proxy.row_for_identity(*identity)
            if visible >= 0:
                cell = self.proxy.index(visible, self.model.TextColumn)
                self.table.scrollTo(cell)
                self.table.edit(cell)
        add('Editar texto aqui', edit)
        add('Copiar texto atual', lambda: QApplication.clipboard().setText(self.editor.toPlainText()))
        add('Copiar chave e ID', lambda: QApplication.clipboard().setText('%s:%s · %08X' % (row['table'], row['name'], row['hash'])))
        add('Hipertraduzir esta linha', lambda: self.hypertranslate._start_from_provider())
        menu.addSeparator()
        audio = bool(self.enrich(row).get('info'))
        add('Ouvir original (F5)', lambda: self.play('original'), audio)
        add('Ouvir gerado / escolhido (F6)', lambda: self.play('approved'), audio)
        add('Ouvir no mod (F8)', lambda: self.play('installed'), audio)
        add('Comparar original e gerado (F7)', self.compare_audio, audio)
        add('Revisar / gerar fala…', self.review, audio)
        add('Redublar seleção em lote…', self.bulk_generate)
        add('Abrir trecho na timeline…', self.timeline_selected, audio)
        menu.addSeparator()
        add('Marcar favorita', lambda: self.mark('fav'))
        add('Nota…', self.note)
        add('Salvar texto e áudios escolhidos', self.save_file)
        return menu

    def show_table_menu(self, position):
        menu = self.table_menu_for(self.table.indexAt(position))
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(position))
            menu.deleteLater()

    def select(self, index, previous=None):
        if self._resetting or not index.isValid():
            return
        # A commit can sort/filter the proxy. Preserve the clicked identity first.
        source = self.model.row_at(self.proxy.mapToSource(index).row())
        if not source:
            return
        source = dict(source)
        if not self.commit():
            if previous and previous.isValid():
                self.table.setCurrentIndex(previous)
            return
        current_row = self.proxy.row_for_identity(source['table'], source['hash'])
        selection = self.table.selectionModel()
        blocked = selection.blockSignals(True)
        try:
            if current_row >= 0:
                selection.setCurrentIndex(self.proxy.index(current_row, index.column()),
                    QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            selection.blockSignals(blocked)
        self.show_record(source)
        autoplay = config.CFG.get('autoplay', 'none')
        if autoplay in ('original', 'dub'):
            self.play('original' if autoplay == 'original' else 'approved')
        elif autoplay == 'ab':
            self.compare_audio()

    def show_record(self, source):
        self.selected = self.enrich(source)
        text = decode_text(self.document.gxt.t[source['table']]['cur'][source['hash']])
        self.selected['text'] = text
        self._draft_text = text
        self.editor.setPlainText(text)
        self.comparison.setPlainText(source.get('comparison') or '')
        info = self.selected['info']
        state = self.context.review_store.data.get('lines', {}).get(self.selected['line_id'], {})
        self.audio_label.setText('%s · %s | Áudio: %s | %d takes no projeto | Aprovação e instalação são etapas distintas.' % (
            source['table'], source['name'], info.get('stable_audio_id') or info.get('original_stream') or ('mapeado' if info else 'sem vínculo'),
            len(state.get('candidates', []))))

    def update_lint(self):
        warnings = lint_avisos(self.editor.toPlainText())
        self.lint.setText(' · '.join(w[0] for w in warnings) if warnings else '')
        self.update_change_status()

    def update_change_status(self):
        if not hasattr(self, 'change_status'):
            return
        draft = bool(self.selected and self.editor.toPlainText() != self._draft_text)
        edited = self.document.gxt.total_edited() if self.document.gxt else 0
        pending = len(self.pending_audio.choices(self.document.path))
        self.setWindowModified(bool(draft or edited or pending))
        self.change_status.setText('Salvando áudios…' if self._audio_saving else
            ('%d textos alterados · %d áudios escolhidos para salvar' % (edited + int(draft), pending)
             if edited or draft or pending else 'Texto e escolhas salvos'))

    def commit(self):
        if not self.selected or self.editor.toPlainText() == self._draft_text:
            return True
        try:
            text = self.editor.toPlainText()
            self.document.set_text(self.selected['table'], self.selected['hash'], text)
            if hasattr(self, 'pending_audio') and self.document.path:
                choice = self.pending_audio.choices(self.document.path).get(self.selected['line_id'])
                if choice and choice['record']['text'] != text:
                    self.pending_audio.clear(self.document.path, self.selected['line_id'])
            self._draft_text = text
            self.selected['text'] = text
            for row in self.all_rows:
                if (row['table'], row['hash']) == (self.selected['table'], self.selected['hash']):
                    row['text'] = text
                    baseline = self.document.gxt.t[row['table']]['orig'][row['hash']]
                    current = self.document.gxt.t[row['table']]['cur'][row['hash']]
                    if baseline == current:
                        row['flags'].discard('edited')
                    else:
                        row['flags'].add('edited')
                    if row.get('comparison') is not None and row['comparison'] != text:
                        row['flags'].add('different')
                    else:
                        row['flags'].discard('different')
                    state = self.context.review_store.data.get('lines', {}).get(self.selected['line_id'], {})
                    row['review_label'] = review_label(state, text)
                    selection = self.table.selectionModel()
                    blocked = selection.blockSignals(True)
                    try:
                        self.model.update_row(row)
                    finally:
                        selection.blockSignals(blocked)
                    break
            self.setWindowModified(self.document.dirty)
            if hasattr(self, 'update_change_status'):
                self.update_change_status()
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def hypertranslate_identity(self):
        if not self.selected or not self.document.gxt:
            return None
        return dict(table=self.selected['table'], hash=self.selected['hash'],
                    path=str(self.document.path), document_session=id(self.document.gxt))

    def hypertranslate_snapshot(self):
        if not self.selected or not self.commit():
            return dict(text='', identity=None)
        return dict(text=self.editor.toPlainText(), identity=self.hypertranslate_identity())

    def hypertranslate_result(self, result):
        checkpoint = result.get('stages_path') or ''
        # A request may finish after typing, navigation, or replacing the GXT.
        # Keep the full run on disk, but never replace a different live draft.
        if (result.get('identity') != self.hypertranslate_identity() or
                result.get('source_text') != self.editor.toPlainText()):
            self.statusBar().showMessage('Tradução guardada; a linha mudou durante a operação. Histórico: ' + checkpoint)
            return False
        previous = self.editor.toPlainText()
        try:
            self.document._encode_valid(result['text'])
        except (ValueError, TypeError, UnicodeError) as exc:
            self.statusBar().showMessage('Tradução guardada, mas o GXT não aceita alguns caracteres: %s. Histórico: %s' % (exc, checkpoint))
            return False
        self.editor.setPlainText(result['text'])
        if not self.commit():
            self.editor.setPlainText(previous)
            return False
        self.statusBar().showMessage('Hipertradução aplicada à linha; Salvar grava no GXT. Etapas: ' + checkpoint)
        return True

    def refresh_rows(self):
        self.all_rows = self.document.rows()
        # History/save mutate the document independently of the edit widget.
        # Reload before apply_filter can commit the previous widget contents.
        if self.selected:
            identity = (self.selected['table'], self.selected['hash'])
            source = next((row for row in self.all_rows
                           if (row['table'], row['hash']) == identity), None)
            if source:
                self.show_record(source)
        self.apply_filter()
        self.setWindowModified(self.document.dirty)

    def history(self, redo):
        if self.commit() and (self.document.redo() if redo else self.document.undo()):
            self.refresh_rows()

    def copy_comparison(self):
        if self.comparison.toPlainText():
            self.editor.setPlainText(self.comparison.toPlainText())

    def restore_text(self):
        if self.selected:
            self.editor.setPlainText(decode_text(self.document.gxt.t[self.selected['table']]['orig'][self.selected['hash']]))

    def mark(self, key):
        if self.selected:
            line_id = self.selected['line_id']
            self.workspace.set(line_id, **{key: not self.workspace.get(line_id).get(key)})
            self.apply_filter()
            self.statusBar().showMessage('Marcação salva no projeto.')

    def note(self):
        if self.selected:
            line_id = self.selected['line_id']
            text, ok = QInputDialog.getMultiLineText(self, 'Nota da fala', self.selected['name'], self.workspace.get(line_id).get('note', ''))
            if ok:
                self.workspace.set(line_id, note=text)
                self.apply_filter()

    def save_file(self, save_as=False, force_gxt=False):
        if self.npc_active() and not force_gxt:
            return self.npc_workspace.save()
        if self.tasks:
            self.statusBar().showMessage('Aguarde a operação atual para salvar texto e áudio juntos.')
            return False
        for dialog in self.dialogs[:]:
            flush = getattr(dialog, 'flush_text', None)
            if flush:
                flush()
            if getattr(dialog, 'generation', None) and dialog.operation_active():
                self.statusBar().showMessage('Aguarde a geração/conferência do áudio para salvar a escolha junto com o texto.')
                return False
        if not self.document.gxt or not self.commit():
            return False
        if self._audio_saving:
            self.statusBar().showMessage('O salvamento de áudio ainda está em andamento.')
            return False
        before = self.document.path
        destination = before
        if save_as:
            path, _ = QFileDialog.getSaveFileName(self, 'Salvar GXT', str(destination), 'GXT (*.gxt)')
            if not path:
                return False
            destination = Path(path)
        try:
            self.document.save(destination)
            self.setWindowTitle('%s[*] — GXT Editor & Mission Audio Studio' % self.document.path.name)
            self.file_watcher.watch('gxt', destination)
            self.pending_audio.saved_as(before, destination)
            self.refresh_rows()
            self.path_label.setText('Editando: %s | Comparação: %s' % (self.document.path,
                self.document.comparison.path if self.document.comparison else 'nenhuma'))
            choices = self.pending_audio.choices(destination)
            if choices and hasattr(self.context, 'game_audio'):
                from .audio_sync import detect_mod_from_gxt
                target_mod = detect_mod_from_gxt(destination, self.context.game_audio)
                if not target_mod:
                    self.update_change_status()
                    self.change_status.setText('GXT salvo; áudio guardado no projeto. Abra o GXT dentro do mod para gravar os bancos.')
                    return True
                self.context.game_audio.cfg['modloader_mod'] = target_mod
                for choice in choices.values():
                    choice['record']['_target_mod'] = target_mod
            if choices:
                self._audio_saving = True
                from .audio_save import apply_selected
                def progress(index, total, message):
                    return '%d/%d · %s' % (index, total, message)
                def ready(result):
                    self._audio_saving = False
                    if hasattr(self.context, 'filter_installation_evidence'):
                        self.context.filter_installation_evidence.update({line_id: True for line_id in result.get('installed', [])})
                    persisted = self.pending_audio.choices(destination)
                    # Target pin is job-local; the saved pending list keeps only user choices.
                    for line_id, choice in persisted.items():
                        submitted = choices.get(line_id)
                        if submitted and submitted['candidate_id'] == choice['candidate_id']:
                            submitted['record'].pop('_target_mod', None)
                    try:
                        self.pending_audio.complete(destination, choices, result.get('installed', []))
                    except (OSError, ValueError, TypeError) as exc:
                        self.change_status.setText('Áudio aplicado; registro pendente: ' + str(exc))
                        return
                    self.refresh_rows()
                    for dialog in self.dialogs:
                        if hasattr(dialog, 'reload_sources'):
                            dialog.reload_sources()
                    self.update_change_status()
                    errors = result.get('errors', []) + result.get('skipped', [])
                    self.statusBar().showMessage('GXT salvo · %d áudios gravados no mod · %d pendências' %
                        (len(result.get('installed', [])), len(errors)))
                    if errors:
                        self.change_status.setText('GXT salvo; áudio pendente — ' + str(errors[0].get('reason', errors[0])))
                task = self.work(lambda emit, cancel: apply_selected(self.context, choices,
                    lambda i, n, m: emit(progress(i, n, m))), ready)
                handler = getattr(task, '_failure_slot', None)
                if handler is not None:
                    task.failed.disconnect(handler)
                def failed(message):
                    self._audio_saving = False
                    self.update_change_status()
                    self.change_status.setText('GXT salvo; áudio pendente: ' + str(message))
                task.failed.connect(failed)
            else:
                self.statusBar().showMessage('GXT salvo: %s · áudios existentes mantidos.' % self.document.path)
            self.update_change_status()
            return True
        except Exception as exc:
            self.error(exc)
            return False

    def export_csv(self):
        if not self.commit() or not self.document.gxt:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Exportar textos visíveis', '', 'CSV (*.csv)')
        if path:
            try:
                self.document.export_csv(path, [self.proxy.identity_at(i) for i in range(self.proxy.rowCount())])
                self.statusBar().showMessage('CSV exportado.')
            except Exception as exc:
                self.error(exc)

    def import_csv(self):
        if not self.commit() or not self.document.gxt:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Importar CSV', '', 'CSV (*.csv)')
        if path:
            try:
                preview = self.document.import_csv(path, preview=True)
                if not preview['changed']:
                    QMessageBox.information(self, 'Prévia do CSV', 'Nenhum texto compatível será alterado.')
                    return
                summary = 'Alterar %d textos? Inválidos: %d · ausentes: %d · incompatíveis: %d' % tuple(
                    preview[k] for k in ('changed', 'invalid', 'missing', 'incompatible'))
                prompt = QMessageBox(QMessageBox.Icon.Question, 'Prévia da importação', summary,
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, self)
                prompt.setDetailedText('\n\n'.join('%s · %08X\nAntes: %s\nDepois: %s' %
                    (t, h, decode_text(before), decode_text(after)) for t, h, before, after in preview['changes']))
                if prompt.exec() != QMessageBox.StandardButton.Ok:
                    return
                result = self.document.import_csv(path)
                self._csv_changes = result['changes']
                self.refresh_rows()
                QMessageBox.information(self, 'CSV importado', 'Alteradas: %d · inválidas: %d · ausentes: %d · incompatíveis: %d' %
                    tuple(result[k] for k in ('changed', 'invalid', 'missing', 'incompatible')))
            except Exception as exc:
                self.error(exc)

    def undo_csv(self):
        if not self.commit():
            return
        count = 0
        for table, key, before, after in getattr(self, '_csv_changes', []):
            if self.document.gxt.t[table]['cur'][key] == after:
                self.document.set_text(table, key, decode_text(before))
                count += 1
        self._csv_changes = []
        self.refresh_rows()
        self.statusBar().showMessage('%d textos da importação restaurados; edições posteriores preservadas.' % count)

    def next_row(self, flag, expected, direction=1):
        count = self.proxy.rowCount()
        start = self.table.currentIndex().row()
        for offset in range(1, count + 1):
            index = (start + offset * direction) % count
            row = self.model.row_at(self.proxy.mapToSource(self.proxy.index(index, 0)).row())
            if (flag in self.row_flags(row)) == expected:
                self.table.setCurrentIndex(self.proxy.index(index, 0))
                return

    def adjacent_row(self, direction):
        count = self.proxy.rowCount()
        if count:
            self.table.setCurrentIndex(self.proxy.index((self.table.currentIndex().row() + direction) % count, 0))

    def next_undubbed(self, direction=1):
        count = self.proxy.rowCount()
        start = self.table.currentIndex().row()
        for offset in range(1, count + 1):
            index = (start + direction * offset) % count
            row = self.model.row_at(self.proxy.mapToSource(self.proxy.index(index, 0)).row())
            flags = self.row_flags(row)
            if 'audio' in flags and 'dub' not in flags:
                self.table.setCurrentIndex(self.proxy.index(index, 0))
                return

    def play(self, kind):
        if self.npc_active():
            return self.npc_workspace.play(kind)
        if not self.selected or not self.commit():
            return
        record = dict(self.selected)
        def operation(progress, cancel):
            return {'original': self.context.original_path, 'installed': self.context.installed_path,
                'approved': lambda r: self.context.chosen_path(r['line_id'], r['text'])}[kind](record)
        self.work(operation, self.context.player.play)

    def compare_audio(self, loop=False, installed=False):
        if self.npc_active():
            return self.npc_workspace.compare_audio()
        if self.selected and self.commit():
            record = dict(self.selected)
            def resolve(progress, cancel):
                paths = [self.context.original_path(record), self.context.chosen_path(record['line_id'], record['text'])]
                if installed:
                    paths.append(self.context.installed_path(record))
                return paths
            self.work(resolve, lambda paths: self.context.player.compare(paths, loop=loop,
                pause_ms=int(config.CFG.get('pausa_ab_ms', 300))))

    def choose_scene(self):
        if not self.commit():
            return
        from .scene_browser import SceneBrowserDialog
        dialog = SceneBrowserDialog(self.context, [self.enrich(row) for row in self.all_rows], self)
        dialog.sceneRequested.connect(self.open_scene)
        dialog.batchRequested.connect(lambda row: self.scene_batch(row['records'], [row['track']]))
        return self.show_dialog(dialog)

    def open_scene(self, row):
        if self.tasks:
            self.statusBar().showMessage('Aguarde a abertura atual ou cancele antes de abrir outra cena.')
            return
        from .scene import prepare_scene
        records = [self.enrich(item) for item in self.all_rows]
        selected = next(iter(row['records']), None)
        def ready(result):
            window = self.timeline()
            window.load_project(result['path'])
            self.statusBar().showMessage('Cena completa aberta; referências e áudio do mod carregados separadamente.')
        self.work(lambda progress, cancel: prepare_scene(self.context, selected, records, progress, cancel, scene_route=row), ready)

    def scene_batch(self, records=None, tracks_spec=None):
        if not self.document.gxt or not self.commit():
            self.statusBar().showMessage('Abra o GXT para gerar o texto atual das cutscenes.')
            return
        from .cutscene_batch import CutsceneBatchDialog
        dialog = CutsceneBatchDialog(self.context, self, tracks_spec=tracks_spec)
        dialog.sceneRequested.connect(lambda payload: self.open_scene(dict(container='CUTSCENE',
            track=payload['track']['track'], records=[])))
        return self.show_dialog(dialog)

    def help_dialog(self):
        QMessageBox.information(self, 'Guia do editor',
            '1. Abra o GXT e escolha a missão ou tabela.\n2. Edite o texto; Salvar grava o arquivo indicado no cabeçalho.\n'
            '3. Na revisão, o take selecionado fica escolhido. Salvar grava o GXT e os áudios escolhidos no mod.\n'
            '4. No estúdio de cutscenes, escolha uma cena e um trecho. Ouvir trecho reproduz inclusive o original; '
            'Ouvir mistura reproduz somente as camadas habilitadas. Aplicar cena ao mod reconstrói o banco com backup.\n\n'
            'Use Cutscenes para encontrar e abrir a cena inteira sem extrair arquivos manualmente. Ctrl+P encontra comandos.')

    def command_palette(self):
        actions = {}
        def collect(menu, prefix=''):
            for action in menu.actions():
                if action.menu():
                    collect(action.menu(), prefix + action.text() + ' / ')
                elif action.text() and action.isEnabled():
                    actions[prefix + action.text().replace('&', '')] = action
        collect(self.menuBar())
        names = sorted(actions)
        name, ok = QInputDialog.getItem(self, 'Comandos', 'Digite ou selecione:', names, 0, True)
        if ok and name in actions:
            actions[name].trigger()

    def classic_tool(self, key):
        if key == 'models':
            from .model_settings import ModelManagerDialog
            dialog = ModelManagerDialog(self.context, self)
            def refresh_settings():
                for current in self.dialogs:
                    generation = getattr(current, 'generation', None)
                    if generation and not generation.busy:
                        generation.settings.refresh_engines()
            dialog.settingsChanged.connect(refresh_settings)
            return self.show_dialog(dialog)
        if not self.commit():
            return
        if key == 'cutscene_manual':
            return self.choose_scene()
        if key == 'cutscene_batch':
            return self.scene_batch()
        try:
            if key in ('audio_library', 'voice_identity'):
                from .library import create_library_dialog
                dialog = create_library_dialog(key, self.context, self,
                    records=[row for row in self.visible_records() if row.get('info')])
                dialog.recordRequested.connect(self.review_record)
            else:
                from .utilities import create_utility_dialog
                dialog = create_utility_dialog(key, self.context, self)
            if hasattr(dialog, 'changed'):
                dialog.changed.connect(self.tool_changed)
            return self.show_dialog(dialog)
        except Exception as exc:
            self.error(exc)

    def tool_changed(self):
        self.context.script_db = recarregar_script_cache()
        self.refresh_audio_lists()

    def review_record(self, record):
        from .audio_review import AudioReviewDialog
        text = self.context.current_text(record)
        dialog = self.show_dialog(AudioReviewDialog(self.context, record, text, self))
        self.bind_review(dialog)
        return dialog

    def refresh_audio_lists(self):
        if self.npc_active():
            return self.npc_workspace.load()
        if self.tasks or any(getattr(d, 'operation_active', lambda: False)() for d in self.dialogs):
            self.statusBar().showMessage('Aguarde a operação atual antes de atualizar as fontes.')
            return
        if not self.commit():
            return
        from studio.projects.review import ReviewStore
        try:
            self.context.review_store.data = ReviewStore(self.context.project_root).data
            self.context.game_audio._ml_escaneado = False
            self._filter_approved_cache = {}
            self._audio_presence.clear()
            for dialog in self.dialogs:
                if hasattr(dialog, 'reload_sources'):
                    dialog.reload_sources()
            self.refresh_rows()
            self.statusBar().showMessage('Takes e fontes atualizados; originais e mod são consultados separadamente.')
        except (OSError, ValueError) as exc:
            self.error(exc)

    def show_dialog(self, dialog):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.dialogs.append(dialog)
        dialog.finished.connect(lambda *_: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.show()
        dialog.raise_()
        return dialog

    def review(self):
        if self.npc_active():
            return self.npc_workspace.generate()
        if self.selected and self.commit():
            from .audio_review import AudioReviewDialog
            dialog = AudioReviewDialog(self.context, dict(self.selected), self.editor.toPlainText(), self)
            self.bind_review(dialog)
            self.show_dialog(dialog)

    def bind_review(self, dialog):
        if hasattr(dialog, 'selectionChanged'):
            dialog.selectionChanged.connect(self.select_audio)
        if hasattr(dialog, 'sceneRequested'):
            dialog.sceneRequested.connect(self.timeline_scene)
        if hasattr(dialog, 'textChanged'):
            dialog.textChanged.connect(self.edit_review_text)

    def edit_review_text(self, record, text):
        try:
            if not self.commit():
                return
            self.document.set_text(record['table'], record['hash'], text)
            self.refresh_rows()
            self.update_change_status()
        except Exception as exc:
            self.error(exc)

    def select_audio(self, record, candidate):
        try:
            if not self.document.path:
                raise ValueError('Abra um GXT antes de escolher o áudio.')
            record = dict(record)
            record['text'] = self.context.current_text(record)
            if candidate is None:
                self.pending_audio.clear(self.document.path, record['line_id'])
                self.update_audio_badge(record)
                self.update_change_status()
                return
            if candidate.get('kind') == 'source':
                if candidate.get('source') == 'installed':
                    self.pending_audio.clear(self.document.path, record['line_id'])
                    self.update_audio_badge(record)
                    self.update_change_status()
                    return
                self.work(lambda emit, cancel: self.context.review_store.import_candidate(record['line_id'],
                    candidate['path'], record['text'], 'Original selecionado manualmente'),
                    lambda imported: self.select_audio(record, imported))
                return
            self.pending_audio.select(self.document.path, record, candidate)
            self.update_audio_badge(record, 'Escolhido · salvar')
            self.update_change_status()
            self.statusBar().showMessage('%s: take escolhido. Salvar grava texto e áudio no mod.' % record.get('name', record['line_id']))
        except Exception as exc:
            self.error(exc)

    def update_audio_badge(self, record, label=None):
        state = self.context.review_store.data.get('lines', {}).get(record['line_id'], {})
        label = label if label is not None else review_label(state, record['text'])
        for row in self.all_rows:
            if (row['table'], row['hash']) == (record['table'], record['hash']):
                row['review_label'] = label
                selection = self.table.selectionModel()
                blocked = selection.blockSignals(True)
                try:
                    self.model.update_row(row)
                finally:
                    selection.blockSignals(blocked)
                return

    def visible_records(self):
        return [self.enrich(self.model.row_at(self.proxy.mapToSource(self.proxy.index(i, 0)).row()))
                for i in range(self.proxy.rowCount())]

    def bulk_review(self):
        if self.npc_active():
            self.statusBar().showMessage('Os takes NPC são ouvidos e escolhidos na própria aba NPCs.')
            return
        if self.commit():
            from .audio_review import BulkAudioReviewDialog
            self.show_dialog(BulkAudioReviewDialog(self.context, self.visible_records(), self))

    def bulk_generate(self):
        if self.npc_active():
            from .batch import BatchGenerationDialog
            if not self.npc_workspace.flush_text():
                return
            records = self.npc_workspace.selected_records()
            if not records:
                self.statusBar().showMessage('Selecione as vozes NPC que deseja redublar.')
                return
            dialog = BatchGenerationDialog(self.npc_workspace.context, records, self)
            dialog.candidatesReady.connect(self.npc_workspace.accept_generated)
            return self.show_dialog(dialog)
        if self.commit():
            from .batch import BatchGenerationDialog
            indexes = self.table.selectionModel().selectedRows()
            rows = [self.enrich(self.model.row_at(self.proxy.mapToSource(index).row())) for index in indexes]
            dialog = BatchGenerationDialog(self.context, rows or self.visible_records(), self)
            dialog.candidatesReady.connect(self.batch_candidates_ready)
            self.show_dialog(dialog)

    def batch_candidates_ready(self, items):
        selected = 0
        for item in items:
            record, candidate = item['record'], item['candidate']
            if self.context.current_text(record) == record['text']:
                self.select_audio(record, candidate)
                selected += 1
        self.statusBar().showMessage('%d takes do lote escolhidos. Ctrl+S salva o texto e aplica esses áudios ao mod.' % selected)

    def timeline(self):
        from .timeline import TimelineWindow
        window = TimelineWindow(self.context.project_root, self.context.game_audio, self)
        window.sceneRequested.connect(self.choose_scene)
        window.publishRequested.connect(lambda: self.publish_scene(window))
        if hasattr(window, 'reviewRequested'):
            window.reviewRequested.connect(lambda metadata: self.timeline_review(window, metadata))
            window.review_button.setEnabled(True)
        return self.show_dialog(window)

    def publish_scene(self, window):
        if window.job or self.tasks:
            window.status.setText('Aguarde a operação atual antes de aplicar a cena.')
            return
        import copy
        from studio.audio.scene_publish import publish_scene
        from .scene_mix import render_project
        frozen = render_project(window.project)
        def done(result):
            window.project.data['scene']['installed_archive_sha256'] = result['archive_sha256']
            window.project.data['scene']['installed_lookup_sha256'] = result['lookup_sha256']
            window.status.setText('Cena aplicada ao mod e banco conferido. Backup: ' + result['backup'])
            self.refresh_audio_lists()
        window._start(lambda cancel, progress: publish_scene(self.context, frozen, progress=progress, cancel=cancel), done)

    def timeline_review(self, window, metadata):
        metadata = dict(metadata)
        selected_clip = window.project.clip(window.selected_id) if window.selected_id else None
        range_start, range_end = window.selection_frames
        if selected_clip and range_end <= range_start:
            range_start = selected_clip['position']
            source = window.project.data['sources'][selected_clip['source_id']]
            range_end = range_start + round((selected_clip['source_out']-selected_clip['source_in']) *
                window.project.sample_rate / source['sample_rate'])
        if not metadata.get('table') or metadata.get('gxt_hash') is None:
            matches = {}
            for clip in window.project.data['clips']:
                detail = clip.get('metadata', {})
                if detail.get('window_role') not in ('reference', 'destination') or not detail.get('table'):
                    continue
                source = window.project.data['sources'][clip['source_id']]
                end = clip['position'] + round((clip['source_out']-clip['source_in']) *
                    window.project.sample_rate / source['sample_rate'])
                if clip['position'] < range_end and end > range_start:
                    matches[(detail['table'], detail['gxt_hash'])] = detail
            if len(matches) == 1:
                metadata.update(next(iter(matches.values())))
            elif matches:
                labels = {'%s · %s · %s' % (item['table'], item.get('name', ''), item.get('text', '')): item
                          for item in matches.values()}
                label, ok = QInputDialog.getItem(self, 'Falas sobrepostas',
                    'O intervalo cruza mais de uma fala. Qual texto você quer gerar?', list(labels), 0, False)
                if not ok:
                    return
                metadata.update(labels[label])
        table, key = metadata.get('table'), metadata.get('gxt_hash')
        if not self.document.gxt or table not in self.document.gxt.t or key not in self.document.gxt.t[table]['cur']:
            self.error('Associe o clipe a uma chave do GXT aberto antes de gerar sua fala.')
            return
        from .audio_review import AudioReviewDialog
        text = decode_text(self.document.gxt.t[table]['cur'][key])
        record = dict(table=table, hash=key, name=metadata.get('name', '%08X' % key),
                      text=text, info=metadata.get('record_info', {}), line_id='%s|%08x' % (table, key))
        from .scene import selected_original_reference
        try:
            reference = selected_original_reference(window.project, window.selected_id, window.selection_frames, table, key)
        except ValueError as exc:
            self.error(exc)
            return
        if reference:
            record['reference_override'] = reference
        dialog = self.show_dialog(AudioReviewDialog(self.context, record, text, self))
        self.bind_review(dialog)
        position = window.project.clip(window.selected_id)['position'] if window.selected_id else window.cursor_frame
        def generated(candidate):
            path = self.context.review_store.resolve_path(candidate['path'])
            if window.job:
                window.status.setText('Take salvo no projeto; aguarde a importação atual antes de adicioná-lo à timeline.')
                return
            window.import_wav(path, 'pt', position, dict(metadata, take=candidate['id'], text=text,
                replace_end=range_end / window.project.sample_rate, replace_start=range_start / window.project.sample_rate))
        dialog.generation.candidateReady.connect(generated)

    def timeline_selected(self):
        if not self.selected or not self.commit():
            return
        record = dict(self.selected)
        def opened(path):
            if not path:
                self.error('Áudio original indisponível. Extraia o banco correspondente.')
                return
            window = self.timeline()
            window.open_record(record, path, record['text'])
            approved = self.context.approved_path(record['line_id'], record['text'])
            if approved:
                # Wait for the source import to finish before appending the PT layer.
                job = window.job
                if job:
                    job.finished.connect(lambda: QTimer.singleShot(0, lambda: window.import_wav(approved, 'pt', 0, record)))
        self.work(lambda progress, cancel: self.context.original_path(record), opened)

    def timeline_scene(self, record=None):
        if not (record or self.selected):
            return self.choose_scene()
        if not self.commit():
            return
        from .scene import prepare_scene
        selected = dict(record or self.selected)
        records = [self.enrich(row) for row in self.all_rows]
        def ready(result):
            window = self.timeline()
            window.load_project(result['path'])
            self.statusBar().showMessage('Cena preparada: %d falas vinculadas, %d takes disponíveis, %d intervalos indisponíveis.' %
                (result['linked'], result['approved'], result['unavailable']))
        self.work(lambda progress, cancel: prepare_scene(self.context, selected, records, progress, cancel), ready)

    def keep_loaded(self, value):
        self.context.jobs.set_keep_model_loaded(value)
        config.CFG['omnivoice_keep_model_loaded'] = value
        config.cfg_save()

    def settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Jogo, projeto e destino dos áudios')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        game = QLineEdit(config.CFG.get('game_dir', ''))
        mod = QLineEdit(self.context.game_audio.mod_ativo())
        form.addRow('Pasta do GTA', game)
        form.addRow('Mod dentro de modloader', mod)
        form.addRow('Projeto e takes', QLabel(str(self.context.project_root)))
        layout.addLayout(form)
        layout.addWidget(QLabel('Áudios são instalados no mod selecionado. Os bancos originais do jogo são apenas referências.'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                name = mod.text().strip()
                if not name or name in ('.', '..') or any(c in name for c in '/\\:'):
                    raise ValueError('Informe somente o nome da pasta do mod, sem barras.')
                self.context.game_audio.definir_pasta(game.text())
                config.CFG['modloader_mod'] = name
                config.cfg_save()
                self.statusBar().showMessage('Destino: ' + str(self.context.game_audio.modloader_dir() / name))
            except Exception as exc:
                self.error(exc)

    def open_mod(self):
        base = self.context.game_audio.modloader_dir()
        if base:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(base / self.context.game_audio.mod_ativo())))

    def extract(self):
        archives = self.context.game_audio.available_archives()
        pak, ok = QInputDialog.getItem(self, 'Extrair áudio original', 'Banco / arquivo:', archives, 0, False)
        if ok:
            force = QMessageBox.question(self, 'Reindexar', 'Forçar uma nova extração? Não altera os arquivos do jogo.') == QMessageBox.StandardButton.Yes
            self.work(lambda progress, cancel: self.context.game_audio.indexar_arquivos([pak],
                on_line=progress, cancel=cancel, forcar=force), lambda result: self.statusBar().showMessage('Extração concluída. ' + str(result)))

    def reindex(self):
        game = self.context.game_audio.game_dir
        if not game:
            self.error('Selecione a pasta do GTA primeiro.')
            return
        def operation(progress, cancel):
            from studio.script.native_index import build
            progress('Lendo vínculos nativos do SCRIPT e das cutscenes…')
            records = [v for k, v in self.context.script_db.items() if isinstance(k, tuple) and k[0] not in ('ANY', 'EVENT', 'AID', '_AMBIGUOUS') and isinstance(v, dict)]
            report = build(game, records, progress=lambda i, n, message: progress(message))
            if cancel.is_set():
                raise ValueError('Reindexação cancelada; índice anterior preservado.')
            for source in report['sources'].values():
                stat = Path(source['path']).stat()
                source['signature'] = [stat.st_size, stat.st_mtime_ns]
            from studio.audio.stream_catalog import refresh_available_catalog
            refresh_available_catalog(self.context.project_root)
            json_atomico(self.context.project_root / 'workspace/native_audio/index.json', report)
            return recarregar_script_cache()
        def ready(db):
            self.context.script_db = db
            self.apply_filter()
            self.statusBar().showMessage('Vínculos nativos atualizados: SCRIPT, CUTSCENE e BEATS.')
        self.work(operation, ready)

    def may_close_document(self):
        npc = getattr(self, 'npc_workspace', None)
        if npc and npc.operation_active():
            self.statusBar().showMessage('Aguarde ou cancele a operação NPC antes de trocar de arquivo.')
            return False
        if getattr(self, 'hypertranslate', None) and self.hypertranslate.operation_active():
            self.statusBar().showMessage('A hipertradução está em andamento. Cancele na própria linha ou aguarde antes de trocar o GXT.')
            return False
        if self.tasks or any((getattr(dialog, 'operation_active', None) and dialog.operation_active()) or
                (getattr(dialog, 'worker', None) and dialog.worker.isRunning()) or
                (getattr(dialog, 'generation', None) and dialog.generation.busy) or
                getattr(dialog, 'job', None) for dialog in self.dialogs):
            self.error('Aguarde ou cancele a operação atual antes de trocar o GXT.')
            return False
        if not self.commit():
            return False
        pending = bool(self.pending_audio.choices(self.document.path))
        if not self.document.dirty and not pending:
            return True
        response = QMessageBox.question(self, 'Alterações não salvas', 'Salvar o texto e os áudios escolhidos?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        return (self.save_file(force_gxt=True) and not self.tasks) if response == QMessageBox.StandardButton.Save else response == QMessageBox.StandardButton.Discard

    def closeEvent(self, event):
        self.presence_timer.stop()
        if self._presence_task is not None:
            self._presence_closing = True
            self._presence_task.cancel()
            self.presence_status.setText('Encerrando consulta de áudio…')
            event.ignore()
            return
        if self.npc_workspace:
            if self.npc_workspace.operation_active():
                self.statusBar().showMessage('Aguarde ou cancele a operação NPC antes de fechar.')
                event.ignore()
                return
            if not self.npc_workspace.flush_text():
                event.ignore()
                return
        if getattr(self, 'hypertranslate', None) and self.hypertranslate.operation_active():
            self.statusBar().showMessage('Cancele a hipertradução na tela principal ou aguarde antes de fechar.')
            event.ignore()
            return
        if self.tasks or any((getattr(dialog, 'operation_active', None) and dialog.operation_active()) or getattr(dialog, 'job', None) or
                (getattr(dialog, 'generation', None) and dialog.generation.busy) or
                (getattr(dialog, 'worker', None) and dialog.worker.isRunning()) for dialog in self.dialogs):
            self.error('Uma operação está em andamento. Cancele ou aguarde antes de fechar o editor.')
            event.ignore()
            return
        if not self.may_close_document():
            event.ignore()
            return
        for dialog in self.dialogs[:]:
            dialog.close()
            if dialog.isVisible():
                event.ignore()
                return
        self.context.shutdown()
        if self.document.path:
            session = dict(config.CFG.get('sessao', {}))
            session.update(gxt=str(self.document.path), cmp=self.document.comparison.path if self.document.comparison else '')
            config.CFG['sessao'] = session
            config.cfg_save()
        event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    window = EditorWindow()
    window.show()
    return app.exec()
