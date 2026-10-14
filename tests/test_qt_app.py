import unittest
import struct
import tempfile
from types import SimpleNamespace
from pathlib import Path

from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTableView
from studio.ui_qt.app import EditorWindow, review_label
from studio.projects.review import text_hash
from studio.ui_qt.document import QtDocument
from studio.ui_qt.models import GxtSortFilterProxyModel, GxtTableModel


class _Value:
    def __init__(self, value):
        self.value = value

    def currentText(self):
        return self.value

    def isChecked(self):
        return self.value

    def text(self):
        return self.value


class _Table:
    def setColumnWidth(self, *_args):
        pass

    def setColumnHidden(self, *_args):
        pass

    def setCurrentIndex(self, *_args):
        pass


class _StatusBar:
    def showMessage(self, *_args):
        pass


def _fixture_gxt(entries):
    data = bytearray()
    offsets = []
    for _key, value in entries:
        offsets.append(len(data))
        data.extend(value + b'\0')
    keys = b''.join(struct.pack('<II', offset, key)
                    for (key, _value), offset in zip(entries, offsets))
    block = b'TKEY' + struct.pack('<I', len(keys)) + keys
    block += b'TDAT' + struct.pack('<I', len(data)) + data
    table_header = b'MAIN\0\0\0\0' + struct.pack('<I', 24)
    return struct.pack('<HH', 4, 8) + b'TABL' + struct.pack('<I', 12) + table_header + block


class QtAppFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_no_audio_filter_keeps_rows_without_audio(self):
        window = SimpleNamespace(
            document=SimpleNamespace(gxt=object()),
            selected=None,
            everywhere=_Value(False),
            pairs=None,
            filter=_Value('Sem áudio'),
            all_rows=[{'table': 'MAIN', 'hash': 0x1234, 'name': 'HELLO',
                       'text': 'Hello', 'comparison': None, 'flags': set()}],
            row_flags=lambda _row: set(),
            enrich=lambda row: dict(row, line_id='MAIN|00001234', info={}),
            context=SimpleNamespace(review_store=SimpleNamespace(data={'lines': {}})),
            workspace=SimpleNamespace(get=lambda _line_id: {}),
            _resetting=False,
            commit=lambda: True,
            model=GxtTableModel(),
            proxy=GxtSortFilterProxyModel(),
            table=_Table(),
            search=_Value(''),
            statusBar=lambda: _StatusBar(),
        )
        window.proxy.setSourceModel(window.model)

        EditorWindow.apply_filter(window)

        self.assertEqual(window.proxy.rowCount(), 1)

    def test_search_placeholder_audio_id_is_in_model_filter_index(self):
        model = GxtTableModel()
        proxy = GxtSortFilterProxyModel()
        proxy.setSourceModel(model)
        model.set_rows([{'table': 'MAIN', 'hash': 0x1234, 'name': 'HELLO',
                         'text': 'Hello', 'comparison': None, 'flags': set(),
                         'audio_label': 'SCRIPT:B015:S003'}])

        proxy.set_filter_text('SCRIPT:B015:S003')

        self.assertEqual(proxy.rowCount(), 1)

    def test_undo_keeps_the_edited_identity_selected(self):
        selected = {'table': 'MAIN', 'hash': 0x1234, 'text': 'Novo'}
        window = SimpleNamespace(
            commit=lambda: True,
            document=SimpleNamespace(undo=lambda: True, redo=lambda: False),
            selected=selected,
            refresh_rows=lambda: None,
        )

        EditorWindow.history(window, False)

        self.assertIs(window.selected, selected)

    def test_committing_saved_text_clears_the_edited_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = QtDocument()
            path = Path(tmp) / 'texts.gxt'
            path.write_bytes(_fixture_gxt([(0x1234, b'Original')]))
            document.load(path)
            document.set_text('MAIN', 0x1234, 'Novo')
            row = {'table': 'MAIN', 'hash': 0x1234, 'name': 'HELLO', 'text': 'Novo',
               'comparison': None, 'flags': {'edited'}}
            model = GxtTableModel()
            model.set_rows([row])
            proxy = GxtSortFilterProxyModel()
            proxy.setSourceModel(model)
            table = QTableView()
            table.setModel(proxy)
            editor = QPlainTextEdit()
            editor.setPlainText('Original')
            window = SimpleNamespace(
                selected={'table': 'MAIN', 'hash': 0x1234, 'text': 'Novo', 'line_id': 'MAIN|00001234'},
                context=SimpleNamespace(review_store=SimpleNamespace(data={'lines': {}})),
                editor=editor,
                _draft_text='Novo',
                document=document,
                all_rows=[row],
                model=model,
                table=table,
                setWindowModified=lambda *_args: None,
                error=lambda *_args: None,
            )

            self.assertTrue(EditorWindow.commit(window))
            self.assertNotIn('edited', row['flags'])
            self.assertFalse(document.dirty)

    def test_commit_refreshes_the_visible_text_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = QtDocument()
            path = Path(tmp) / 'texts.gxt'
            path.write_bytes(_fixture_gxt([(0x1234, b'Original')]))
            document.load(path)
            document.set_text('MAIN', 0x1234, 'Novo')
            row = {'table': 'MAIN', 'hash': 0x1234, 'name': 'HELLO', 'text': 'Novo',
                   'comparison': None, 'flags': {'edited'}}
            model = GxtTableModel()
            model.set_rows([row])
            proxy = GxtSortFilterProxyModel()
            proxy.setSourceModel(model)
            table = QTableView()
            table.setModel(proxy)
            editor = QPlainTextEdit()
            editor.setPlainText('Mais novo')
            window = SimpleNamespace(
                selected={'table': 'MAIN', 'hash': 0x1234, 'text': 'Novo', 'line_id': 'MAIN|00001234'},
                context=SimpleNamespace(review_store=SimpleNamespace(data={'lines': {}})),
                editor=editor,
                _draft_text='Novo',
                document=document,
                all_rows=[row],
                model=model,
                table=table,
                setWindowModified=lambda *_args: None,
                error=lambda *_args: None,
            )

            self.assertTrue(EditorWindow.commit(window))
            self.assertEqual(model.row_at(0)['text'], 'Mais novo')

    def test_document_switch_is_blocked_while_dialog_operation_runs(self):
        dialog = SimpleNamespace(operation_active=lambda: True, worker=None, job=None, generation=None)
        committed = []
        window = SimpleNamespace(
            tasks=[], dialogs=[dialog],
            error=lambda *_args: None,
            commit=lambda: committed.append(True) or True,
            document=SimpleNamespace(dirty=False),
        )

        self.assertFalse(EditorWindow.may_close_document(window))
        self.assertEqual(committed, [])

    def test_text_edit_does_not_claim_old_approved_take_matches(self):
        state = {'approved': 'old', 'candidates': [dict(id='old', status='approved', text_hash=text_hash('Antigo'))]}
        self.assertEqual(review_label(state, 'Antigo'), 'Aprovado no projeto')
        self.assertEqual(review_label(state, 'Novo'), 'Take desatualizado')


if __name__ == '__main__':
    unittest.main()
