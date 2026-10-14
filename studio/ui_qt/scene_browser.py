"""Mission-aware scene picker. Listing never requires an extracted audio file."""
from collections import defaultdict

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QPushButton, QTreeWidget, QTreeWidgetItem, QProgressBar)

from studio.audio.source_routing import audio_route
from studio.script.native_index import load_current, track_lookup
from .tasks import Task


def scene_catalog(context, records, progress, cancel):
    progress('Encontrando cenas e falas por missão…')
    groups = {}
    def add(container, track):
        return groups.setdefault((container, track), dict(container=container, track=track, scenes=set(), tables=set(), records=[]))
    # All native scenes appear, even ones without a currently linked GXT line.
    try:
        for name, route in track_lookup(context.game_audio.game_dir).items():
            add('CUTSCENE', route['track'])['scenes'].add(name)
    except (OSError, ValueError, TypeError):
        pass
    report = load_current(context.project_root, context.game_audio.game_dir) or {}
    for info in report.get('records', []):
        stream = info.get('stream') or {}
        if info.get('pak') == 'CUTSCENE' and stream.get('track'):
            item = add('CUTSCENE', stream['track'])
            if info.get('scene'):
                item['scenes'].add(info['scene'])
            if info.get('table'):
                item['tables'].add(info['table'])
    for index, record in enumerate(records):
        if cancel.is_set():
            raise InterruptedError('Busca de cenas cancelada.')
        try:
            route = audio_route(record.get('info') or {})
        except ValueError:
            continue
        if not route or route['family'] != 'streams':
            continue
        item = add(route['container'], route['track'])
        item['records'].append(record)
        item['tables'].add(record['table'])
        scene = record.get('info', {}).get('scene')
        if scene:
            item['scenes'].add(scene)
    if not groups:
        # Native track IDs remain explicit; no speculative GXT associations.
        for n in range(1, 142):
            add('CUTSCENE', 'Track_%03d' % n)
    add('BEATS', 'Track_010')['scenes'].add('Prólogo · viatura / In the Beginning')
    navigation = context.document.navigation() if hasattr(context.document, 'navigation') else {}
    member_names = defaultdict(set)
    for category in navigation.values():
        if category.get('section') == 'Missões':
            for member in category.get('members', []):
                member_names[tuple(member)].add(category['label'])
    rows = []
    for _, value in sorted(groups.items()):
        item = dict(value, scenes=sorted(value['scenes']), tables=sorted(value['tables']))
        scene_label = ', '.join(item['scenes'])
        item['label'] = (('%s/%s%s' % (item['container'], item['track'],
                                       ' · ' + scene_label if scene_label else ''))
                         if item['container'] == 'BEATS' else (scene_label or item['track']))
        names = {name for record in item['records'] for name in member_names.get((record['table'], record['hash']), set())}
        item['missions'] = ', '.join(sorted(names)) or ', '.join(item['tables'])
        item['search_text'] = ' '.join(str(value) for record in item['records']
            for value in (record.get('name'), record.get('text'), record.get('line_id'),
                          record.get('stable_audio_id'), (record.get('info') or {}).get('stable_audio_id')) if value)
        rows.append(item)
    return rows


class SceneBrowserDialog(QDialog):
    sceneRequested = Signal(object)
    batchRequested = Signal(object)

    def __init__(self, context, records, parent=None, autoload=True):
        super().__init__(parent)
        self.context, self.records = context, list(records)
        self.rows = []
        self.task = None
        self._close_pending = False
        self.setWindowTitle('Cutscenes — escolher cena completa')
        self.resize(920, 580)
        root = QVBoxLayout(self)
        root.addWidget(QLabel('Escolha a cena. O original e o áudio atual do mod são carregados automaticamente.'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Buscar missão, cena, tabela ou faixa…')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        root.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Cena', 'Missão / tabela', 'Arquivo', 'Falas vinculadas'])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(lambda *_: self.open_selected())
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        root.addWidget(self.tree, 1)
        self.detail = QLabel('')
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        root.addWidget(self.progress)
        self.status = QLabel('Preparando catálogo de cenas…')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        bar = QHBoxLayout()
        self.refresh_button = QPushButton('Atualizar lista')
        self.refresh_button.clicked.connect(self.load)
        bar.addWidget(self.refresh_button)
        bar.addStretch()
        self.batch_button = QPushButton('Redublar falas desta cena…')
        self.batch_button.setEnabled(False)
        self.batch_button.clicked.connect(lambda: self.batchRequested.emit(self.selected()))
        bar.addWidget(self.batch_button)
        self.open_button = QPushButton('Editar cena completa')
        self.open_button.setEnabled(False)
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self.open_selected)
        bar.addWidget(self.open_button)
        root.addLayout(bar)
        if autoload:
            QTimer.singleShot(0, self.load)

    def load(self):
        if self.task:
            return
        self.progress.show()
        self.refresh_button.setEnabled(False)
        self.task = Task(lambda progress, cancel: scene_catalog(self.context, self.records, progress, cancel), self)
        self.task.progress.connect(self.status.setText)
        self.task.succeeded.connect(self.populate)
        self.task.failed.connect(self.status.setText)
        self.task.finished.connect(self.finished_task)
        self.task.start()

    def populate(self, rows):
        self.rows = rows
        self.tree.clear()
        for index, row in enumerate(rows):
            item = QTreeWidgetItem([row['label'], row['missions'], row['container'] + '/' + row['track'], str(len(row['records']))])
            item.setToolTip(0, row['label'])
            item.setToolTip(1, row['missions'])
            # QVariant recursively copies dicts/lists. Store an integer here;
            # the Python catalogue owns the potentially large GXT evidence.
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.tree.addTopLevelItem(item)
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 260)
        self.tree.setColumnWidth(2, 190)
        self.status.setText('%d cenas/faixas disponíveis. BEATS inclui as legendas vinculadas; a extração só acontece ao abrir a escolhida.' % len(rows))
        self.filter_rows(self.search.text())
        if rows:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def selected(self):
        item = self.tree.currentItem()
        return self.rows[item.data(0, Qt.ItemDataRole.UserRole)] if item and not item.isHidden() else None

    def selection_changed(self):
        row = self.selected()
        self.open_button.setEnabled(bool(row) and not self.task)
        self.batch_button.setEnabled(bool(row and row['records']) and not self.task)
        if row:
            samples = []
            for record in row['records'][:4]:
                name = record.get('name') or record.get('line_id') or record.get('stable_audio_id')
                text = str(record.get('text') or '').strip()
                if name or text:
                    samples.append('%s%s' % (name or 'Legenda', ': ' + text if text else ''))
            linked = (' · Vínculos: ' + ' | '.join(samples)) if samples else ''
            self.detail.setText('%s · %d falas%s. Selecione um trecho na timeline para ouvir, editar ou usar como referência da voz.' %
                                (row['container'] + '/' + row['track'], len(row['records']), linked))

    def filter_rows(self, text):
        terms = text.casefold().split()
        for n in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(n)
            index = item.data(0, Qt.ItemDataRole.UserRole)
            row = self.rows[index]
            searchable = (' '.join(item.text(i) for i in range(4)) + ' ' + row.get('search_text', '')).casefold()
            item.setHidden(not all(t in searchable for t in terms))
        self.selection_changed()

    def open_selected(self):
        row = self.selected()
        if row and not self.task:
            self.sceneRequested.emit(row)

    def operation_active(self):
        return self.task is not None

    def finished_task(self):
        task, self.task = self.task, None
        task.wait()
        task.setParent(None)
        task.deleteLater()
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.selection_changed()
        if self._close_pending:
            self.close()

    def closeEvent(self, event):
        if self.task:
            self._close_pending = True
            self.task.cancel_event.set()
            self.status.setText('Cancelando a busca de cenas…')
            event.ignore()
        else:
            super().closeEvent(event)
