"""Real Qt editor/save regressions; all writable state lives in temporary roots."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

from studio.gxt.codec import decode_text
from studio.gxt.model import Gxt
from studio.ui_qt.app import EditorWindow
from studio.ui_qt.document import QtDocument


def fixture_gxt():
    values = [(0x1234, b'Alpha'), (0x5678, b'Bravo')]
    data = bytearray()
    keys = bytearray()
    for key, value in values:
        keys.extend(struct.pack('<II', len(data), key))
        data.extend(value + b'\0')
    return (struct.pack('<HH', 4, 8) + b'TABL' + struct.pack('<I', 12)
            + b'MAIN\0\0\0\0' + struct.pack('<I', 24)
            + b'TKEY' + struct.pack('<I', len(keys)) + keys
            + b'TDAT' + struct.pack('<I', len(data)) + data)


class _Player(QObject):
    status = Signal(str)
    def stop(self):
        pass


class QtSaveRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'source.gxt'
        self.original = fixture_gxt()
        self.path.write_bytes(self.original)
        self.context_patch = patch('studio.ui_qt.app.QtContext', self.make_context)
        self.context_patch.start()
        self.addCleanup(self.context_patch.stop)
        self.backup_patch = patch('studio.ui_qt.document.PROJ_ROOT', self.root)
        self.backup_patch.start()
        self.addCleanup(self.backup_patch.stop)
        self.window = EditorWindow(project_root=self.root, restore=False)
        self.addCleanup(self.window.deleteLater)
        self.errors = []
        self.window.error = self.errors.append
        document = QtDocument(SimpleNamespace(key_names={}, for_gxt=lambda _g: {}))
        document.load(self.path)
        document._names['MAIN'] = {0x1234: 'ALPHA', 0x5678: 'BRAVO'}
        self.window.loaded((document, document.rows(), {}))
        self.window.table.sortByColumn(self.window.model.HashColumn, Qt.SortOrder.AscendingOrder)
        self.choose(0x1234)

    def make_context(self, document, project_root, parent):
        return SimpleNamespace(document=document, project_root=Path(project_root),
            player=_Player(parent), script_db={},
            review_store=SimpleNamespace(data={'lines': {}}))

    def choose(self, key):
        row = self.window.proxy.row_for_identity('MAIN', key)
        self.assertGreaterEqual(row, 0)
        self.window.table.setCurrentIndex(self.window.proxy.index(row, 0))
        self.app.processEvents()

    def saved_text(self, key, path=None):
        return decode_text(Gxt(path or self.path).t['MAIN']['cur'][key])

    def test_inline_hypertranslate_updates_selected_text_and_undo_without_saving(self):
        snapshot = self.window.hypertranslate_snapshot()
        self.assertEqual(snapshot['text'], 'Alpha')
        self.window.hypertranslate_result(dict(identity=snapshot['identity'],
            source_text=snapshot['text'], text='Pudim espacial ~z~', stages_path='checkpoint.json'))
        self.assertEqual(self.window.editor.toPlainText(), 'Pudim espacial ~z~')
        self.assertTrue(self.window.document.dirty)
        self.assertEqual(self.saved_text(0x1234), 'Alpha')
        self.window.history(False)
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')

    def test_inline_hypertranslate_does_not_overwrite_later_typing_or_another_row(self):
        snapshot = self.window.hypertranslate_snapshot()
        result = dict(identity=snapshot['identity'], source_text=snapshot['text'],
                      text='Resultado antigo', stages_path='checkpoint.json')
        self.window.editor.setPlainText('Minha mudança')
        self.window.hypertranslate_result(result)
        self.assertEqual(self.window.editor.toPlainText(), 'Minha mudança')
        self.choose(0x5678)
        self.window.hypertranslate_result(result)
        self.assertEqual(self.window.editor.toPlainText(), 'Bravo')
        self.assertIn('checkpoint.json', self.window.statusBar().currentMessage())

    def test_inline_hypertranslate_rejects_an_old_document_session(self):
        snapshot = self.window.hypertranslate_snapshot()
        snapshot['identity']['document_session'] = -1
        self.window.hypertranslate_result(dict(identity=snapshot['identity'],
            source_text='Alpha', text='Resultado antigo', stages_path='checkpoint.json'))
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')

    def test_clicking_filtered_row_updates_the_text_used_for_inline_translation(self):
        from PySide6.QtTest import QTest
        self.window.show()
        self.window.search.setText('Bravo')
        self.app.processEvents()
        index = self.window.proxy.index(0, 0)
        QTest.mouseClick(self.window.table.viewport(), Qt.MouseButton.LeftButton,
                        pos=self.window.table.visualRect(index).center())
        self.app.processEvents()
        self.assertEqual(self.window.hypertranslate_snapshot()['text'], 'Bravo')
        self.assertEqual(self.window.selected['hash'], 0x5678)

    def test_direct_table_edit_updates_document_undo_and_atomic_save(self):
        index = self.window.proxy.index(self.window.proxy.row_for_identity('MAIN', 0x1234), self.window.model.TextColumn)
        self.assertTrue(self.window.proxy.flags(index) & Qt.ItemFlag.ItemIsEditable)
        self.assertTrue(self.window.proxy.setData(index, 'Texto direto ~z~', Qt.ItemDataRole.EditRole))
        self.assertEqual(self.window.editor.toPlainText(), 'Texto direto ~z~')
        self.assertEqual(self.saved_text(0x1234), 'Alpha')
        self.window.history(False)
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')
        self.window.history(True)
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Texto direto ~z~')

    def test_direct_table_edit_validates_gxt_and_keeps_comparison_readonly(self):
        row = self.window.proxy.row_for_identity('MAIN', 0x1234)
        current = self.window.proxy.index(row, self.window.model.TextColumn)
        original = self.window.proxy.index(row, self.window.model.ComparisonColumn)
        self.assertFalse(self.window.proxy.flags(original) & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(self.window.proxy.setData(current, '\U0001f642', Qt.ItemDataRole.EditRole))
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')
        self.assertFalse(self.window.document.dirty)

    def test_context_menu_edits_clicked_identity_and_preserves_multi_selection(self):
        from PySide6.QtCore import QItemSelectionModel
        selection = self.window.table.selectionModel()
        for i in range(2):
            selection.select(self.window.proxy.index(i, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        index = self.window.proxy.index(self.window.proxy.row_for_identity('MAIN', 0x5678), self.window.model.TextColumn)
        menu = self.window.table_menu_for(index)
        self.assertEqual(self.window.selected['hash'], 0x5678)
        names = [action.text() for action in menu.actions()]
        self.assertIn('Editar texto aqui', names)
        self.assertIn('Redublar seleção em lote…', names)
        self.assertIn('Ouvir no mod (F8)', names)
        self.assertEqual(len(selection.selectedRows()), 2)
        menu.deleteLater()

    def test_double_click_editor_commits_multiline_and_escape_discards(self):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QPlainTextEdit
        self.window.show()
        row = self.window.proxy.row_for_identity('MAIN', 0x1234)
        index = self.window.proxy.index(row, self.window.model.TextColumn)
        self.window.table.scrollTo(index)
        self.app.processEvents()
        QTest.mouseClick(self.window.table.viewport(), Qt.MouseButton.LeftButton,
                        pos=self.window.table.visualRect(index).center())
        QTest.mouseDClick(self.window.table.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.window.table.visualRect(index).center())
        self.app.processEvents()
        inline = self.window.table.findChild(QPlainTextEdit, 'inlineGxtText')
        self.assertIsNotNone(inline)
        inline.setPlainText('Primeira\nSegunda ~z~')
        QTest.keyClick(inline, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.window.editor.toPlainText(), 'Primeira\nSegunda ~z~')
        row = self.window.proxy.row_for_identity('MAIN', 0x1234)
        self.window.table.edit(self.window.proxy.index(row, self.window.model.TextColumn))
        self.app.processEvents()
        inline = next(widget for widget in self.window.table.findChildren(QPlainTextEdit, 'inlineGxtText') if widget.isVisible())
        inline.setPlainText('Não guardar')
        QTest.keyClick(inline, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertEqual(self.window.editor.toPlainText(), 'Primeira\nSegunda ~z~')

    def test_inline_hypertranslate_keeps_valid_text_when_result_is_not_gxt_encodable(self):
        snapshot = self.window.hypertranslate_snapshot()
        self.window.hypertranslate_result(dict(identity=snapshot['identity'],
            source_text='Alpha', text='Texto com emoji \U0001f642', stages_path='checkpoint.json'))
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')
        self.assertFalse(self.window.document.dirty)

    def test_pending_edit_survives_navigation_filter_and_save(self):
        self.window.editor.setPlainText('Alpha editado')
        self.choose(0x5678)
        self.window.editor.setPlainText('Bravo editado')
        self.window.search.setText('Alpha')
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Alpha editado')
        self.assertEqual(self.saved_text(0x5678), 'Bravo editado')
        self.assertEqual(self.errors, [])
        self.assertEqual(Path(str(self.path) + '.bak').read_bytes(), self.original)

    def test_clicked_identity_survives_commit_resorting(self):
        self.window.table.sortByColumn(self.window.model.TextColumn, Qt.SortOrder.AscendingOrder)
        self.window.editor.setPlainText('Zulu')
        self.choose(0x5678)
        self.assertEqual(self.window.selected['hash'], 0x5678)
        self.assertEqual(self.window.editor.toPlainText(), 'Bravo')
        self.window.editor.setPlainText('Bravo editado')
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Zulu')
        self.assertEqual(self.saved_text(0x5678), 'Bravo editado')

    def test_search_reuses_model_and_keyboard_save_writes_the_edit(self):
        from PySide6.QtTest import QTest
        self.window.editor.setPlainText('Texto novo')
        with patch.object(self.window.model, 'set_rows', side_effect=AssertionError('Search rebuilt the document')):
            self.window.search.setText('Texto novo')
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.show()
        self.window.activateWindow()
        self.window.editor.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(self.saved_text(0x1234), 'Texto novo')

    def test_filtered_out_identity_is_reloaded_after_history(self):
        self.window.editor.setPlainText('Zulu')
        self.window.commit()
        self.window.search.setText('Zulu')
        self.window.history(False)
        self.assertEqual(self.window.editor.toPlainText(), 'Alpha')
        self.window.editor.setPlainText(self.window.editor.toPlainText() + ' editado')
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Alpha editado')

    def test_undo_redo_save_as_and_subsequent_save_roundtrip(self):
        self.window.editor.setPlainText('Primeiro')
        self.window.commit()
        self.window.editor.setPlainText('Segundo')
        self.window.commit()
        self.window.history(False)
        self.assertEqual(self.window.editor.toPlainText(), 'Primeiro')
        self.window.history(True)
        self.assertEqual(self.window.editor.toPlainText(), 'Segundo')
        target = self.root / 'saved-as.gxt'
        with patch('studio.ui_qt.app.QFileDialog.getSaveFileName', return_value=(str(target), 'GXT (*.gxt)')):
            self.assertTrue(self.window.save_file(True))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.saved_text(0x1234, target), 'Segundo')
        self.assertEqual(self.window.document.path, target)
        self.assertIn(str(target), self.window.path_label.text())
        self.assertIn(target.name, self.window.windowTitle())
        self.window.editor.setPlainText('Terceiro')
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234, target), 'Terceiro')
        self.assertFalse(self.window.document.dirty)
        self.assertFalse(self.window.isWindowModified())

    def test_cancelled_save_as_preserves_draft_for_next_save(self):
        self.window.editor.setPlainText('Rascunho')
        with patch('studio.ui_qt.app.QFileDialog.getSaveFileName', return_value=('', '')):
            self.assertFalse(self.window.save_file(True))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(self.window.document.dirty)
        self.assertEqual(self.window.document.path, self.path)
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Rascunho')

    def test_failed_atomic_save_keeps_changes_and_destination(self):
        self.window.editor.setPlainText('Ainda pendente')
        target = self.root / 'cannot-save.gxt'
        with patch('studio.ui_qt.app.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            with patch('studio.gxt.model.os.replace', side_effect=PermissionError('test locked file')):
                self.assertFalse(self.window.save_file(True))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(target.exists())
        self.assertEqual(self.window.document.path, self.path)
        self.assertTrue(self.window.document.dirty)
        self.assertEqual(len(self.errors), 1)
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Ainda pendente')

    def test_invalid_text_does_not_report_saved_or_destroy_previous_bytes(self):
        self.window.editor.setPlainText('Unsupported ' + chr(0x1f642))
        self.assertFalse(self.window.save_file())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.saved_text(0x1234), 'Alpha')
        self.assertEqual(len(self.errors), 1)

    def test_external_change_is_preserved_and_can_save_draft_elsewhere(self):
        external = Gxt(str(self.path))
        from studio.gxt.codec import encode_text
        external.t['MAIN']['cur'][0x5678] = encode_text('Outra janela')[0]
        external.save(self.path)
        self.window.editor.setPlainText('Minha edição')
        self.assertFalse(self.window.save_file())
        self.assertEqual(self.saved_text(0x5678), 'Outra janela')
        self.assertTrue(self.window.document.dirty)
        target = self.root / 'preserved.gxt'
        with patch('studio.ui_qt.app.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            self.assertTrue(self.window.save_file(True))
        self.assertEqual(self.saved_text(0x1234, target), 'Minha edição')

    def test_csv_preview_does_not_mutate_and_revert_preserves_later_edits(self):
        csv = self.root / 'texts.csv'
        csv.write_text('tabela;id;atual\nMAIN;1234;Um\nMAIN;5678;Dois\n', encoding='utf-8')
        preview = self.window.document.import_csv(csv, preview=True)
        self.assertEqual(preview['changed'], 2)
        self.assertFalse(self.window.document.dirty)
        result = self.window.document.import_csv(csv)
        self.window._csv_changes = result['changes']
        self.window.refresh_rows()
        self.window.editor.setPlainText('Edição posterior')
        self.window.undo_csv()
        self.assertTrue(self.window.save_file())
        self.assertEqual(self.saved_text(0x1234), 'Edição posterior')
        self.assertEqual(self.saved_text(0x5678), 'Bravo')

    def test_save_publishes_selected_audio_and_retains_failed_choice_for_retry(self):
        import time, wave, math, struct
        from studio.projects.review import ReviewStore
        store = ReviewStore(self.root)
        wav = self.root / 'take.wav'
        with wave.open(str(wav), 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<h', round(2000*math.sin(i/8))) for i in range(800)))
        record = dict(self.window.selected, text='Nova fala')
        self.window.context.current_text = lambda r: decode_text(self.window.document.gxt.t[r['table']]['cur'][r['hash']])
        self.window.context.review_store = store
        candidate = store.import_candidate(record['line_id'], wav, record['text'], 'Teste')
        self.window.editor.setPlainText('Nova fala')
        self.window.commit()
        self.window.select_audio(record, candidate)
        calls = []
        def install(records, progress):
            calls.append(records)
            return dict(installed=[], errors=[dict(reason='Arquivo ocupado')], skipped=[])
        self.window.context.install = install
        self.assertTrue(self.window.save_file())
        deadline = time.monotonic()+5
        while self.window.tasks and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(self.window.tasks)
        self.assertEqual(self.saved_text(0x1234), 'Nova fala')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.window.pending_audio.choices(self.path)), 1)
        self.window.context.install = lambda records, progress: dict(installed=[record['line_id']], errors=[], skipped=[])
        self.assertTrue(self.window.save_file())
        deadline = time.monotonic()+5
        while self.window.tasks and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(self.window.tasks)
        self.assertEqual(self.window.pending_audio.choices(self.path), {})

    def test_save_as_pins_audio_to_the_mod_containing_the_destination_gxt(self):
        import time, wave, math
        from studio.projects.review import ReviewStore
        modloader = self.root / 'game/modloader'
        target = modloader / 'Correct/text/american.gxt'
        target.parent.mkdir(parents=True)
        store = ReviewStore(self.root)
        wav = self.root / 'test.wav'
        with wave.open(str(wav), 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<h', round(1000*math.sin(i/10))) for i in range(800)))
        record = dict(self.window.selected, text='Alpha')
        candidate = store.import_candidate(record['line_id'], wav, 'Alpha', 'Teste')
        self.window.context.review_store = store
        self.window.context.current_text = lambda r: 'Alpha'
        cfg = dict(modloader_mod='Wrong')
        game_audio = SimpleNamespace(cfg=cfg, modloader_dir=lambda: modloader, mod_ativo=lambda: cfg['modloader_mod'])
        self.window.context.game_audio = game_audio
        self.window.select_audio(record, candidate)
        used = []
        def install(records, progress):
            used.append(game_audio.mod_ativo())
            return dict(installed=[record['line_id']], errors=[], skipped=[])
        self.window.context.install = install
        with patch('studio.ui_qt.app.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            self.assertTrue(self.window.save_file(True))
        deadline = time.monotonic()+5
        while self.window.tasks and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(used, ['Correct'])
        self.assertEqual(self.window.pending_audio.choices(target), {})

    def test_npc_batch_flushes_target_draft_before_constructing_requests(self):
        calls = []
        workspace = SimpleNamespace(context=object(),
            flush_text=lambda: calls.append('save target') or True,
            selected_records=lambda: calls.append('read records') or [dict(text='Texto editado')],
            accept_generated=lambda items: None)
        self.window.npc_workspace = workspace
        self.window.navigation.blockSignals(True)
        self.window.navigation.setCurrentIndex(2)
        self.window.navigation.blockSignals(False)
        signal = SimpleNamespace(connect=lambda callback: None)
        dialog = SimpleNamespace(candidatesReady=signal)
        with patch('studio.ui_qt.batch.BatchGenerationDialog', return_value=dialog) as constructor, \
                patch.object(self.window, 'show_dialog'):
            self.window.bulk_generate()
        self.assertEqual(calls, ['save target', 'read records'])
        self.assertEqual(constructor.call_args.args[1][0]['text'], 'Texto editado')
        workspace.flush_text = lambda: False
        with patch('studio.ui_qt.batch.BatchGenerationDialog') as constructor:
            self.window.bulk_generate()
            constructor.assert_not_called()
        self.window.npc_workspace = None

    def test_global_search_follows_npc_tab_without_losing_gxt_query(self):
        from PySide6.QtWidgets import QLineEdit
        self.window.search.setText('Alpha')
        workspace = SimpleNamespace(search=QLineEdit())
        self.window.npc_workspace = workspace
        self.window.navigation.blockSignals(True)
        self.window.navigation.setCurrentIndex(2)
        self.window.navigation.blockSignals(False)
        self.window.switch_search_scope(True)
        self.assertEqual(self.window.search.text(), '')
        self.assertFalse(self.window.filter.isEnabled())
        previous = self.window.proxy.rowCount()
        self.window.search.setText('SPC_EA:B000:S001')
        self.assertEqual(workspace.search.text(), 'SPC_EA:B000:S001')
        self.assertEqual(self.window.proxy.rowCount(), previous)
        self.window.navigation.blockSignals(True)
        self.window.navigation.setCurrentIndex(0)
        self.window.navigation.blockSignals(False)
        self.window.switch_search_scope(False)
        self.assertEqual(self.window.search.text(), 'Alpha')
        self.assertTrue(self.window.filter.isEnabled())
        self.window.npc_workspace = None


if __name__ == '__main__':
    unittest.main()
