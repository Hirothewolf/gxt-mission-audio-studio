import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton, QTableView, QTableWidget

from studio.ui_qt.library import AudioLibraryDialog, VoiceIdentityDialog, _LibraryTableModel, create_library_dialog
from studio.projects.review import ReviewStore


class _Resolver:
    def __init__(self, records=()):
        self.records = list(records)

    def resolve(self, tab, h, key=None, ainfo=None):
        path = (ainfo or {}).get('original_path')
        return (Path(path), 'fixture exact path', 100) if path else (None, '', 0)


class _IdentityStore:
    def __init__(self):
        self.report = {
            'id': 'report-1', 'model_id': 'fixture',
            'groups': [{'id': 'group-1', 'status': 'UNLABELED', 'item_ids': ['SCRIPT:B001:S002']}],
            'items': [{'audio_id': 'SCRIPT:B001:S002', 'path': 'reference.wav',
                       'status': 'UNLABELED', 'group_id': 'group-1'}],
        }
        self.assignments = []

    def latest(self):
        return self.report

    def assign_group(self, report_id, group_id, character):
        self.assignments.append((report_id, group_id, character))
        return {'identity': character}


class QtLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / 'original.wav'
        self.mod = self.root / 'mod.wav'
        self.reference = self.root / 'reference.wav'
        for path in (self.original, self.mod, self.reference):
            self._wav(path)
        self.review = ReviewStore(self.root)
        self.identity = _IdentityStore()
        self.player = Mock()
        self.record = {
            'table': 'MAIN', 'hash': 0x1234, 'name': 'SOUND_CJ_TEST',
            'line_id': 'MAIN|00001234', 'text': 'Original line',
            'info': {'stable_audio_id': 'SCRIPT:B001:S002', 'original_path': str(self.original),
                     'installed_path': str(self.mod)},
        }
        self.context = SimpleNamespace(
            project_root=self.root, review_store=self.review, identity_store=self.identity,
            player=self.player, base_audio=_Resolver(), _resolver_ready=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _wav(path):
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\0\0' * 800)

    def _widget(self, dialog, cls, text=None):
        matches = dialog.findChildren(cls)
        if text is not None:
            matches = [widget for widget in matches if widget.text() == text]
        self.assertTrue(matches)
        return matches[0]

    def test_factory_builds_native_audio_library_and_source_filter_auditions_exact_path(self):
        dialog = create_library_dialog('audio_library', self.context, records=[self.record])
        self.assertIsInstance(dialog, AudioLibraryDialog)
        self.assertTrue(dialog.wait_for_index(3000))
        table = self._widget(dialog, QTableView)
        self.assertEqual(table.model().rowCount(), 2)
        source = self._widget(dialog, QComboBox)
        source.setCurrentText('Original')
        table.selectRow(0)
        self._widget(dialog, QPushButton, 'Ouvir').click()
        self.player.play.assert_called_once_with(self.original)
        provenance = dialog.selected_row()['provenance'] if dialog.selected_row() else ''
        self.assertTrue(provenance)
        dialog.close()

    def test_manual_character_assignment_persists_in_existing_review_store(self):
        dialog = AudioLibraryDialog(self.context, records=[self.record])
        self.assertTrue(dialog.wait_for_index(3000))
        table = self._widget(dialog, QTableView)
        table.selectRow(0)
        dialog.character.setText('CJ')
        changed = Mock()
        dialog.changed.connect(changed)
        self._widget(dialog, QPushButton, 'Guardar personagem').click()
        self.assertEqual(self.review.character('MAIN|00001234'), 'CJ')
        changed.assert_called_once()
        dialog.close()

    def test_voice_identity_assignment_requires_manual_character_and_uses_existing_store(self):
        dialog = create_library_dialog('voice_identity', self.context, records=[self.record])
        self.assertIsInstance(dialog, VoiceIdentityDialog)
        self.assertTrue(dialog.wait_for_index(3000))
        table = self._widget(dialog, QTableWidget)
        self.assertGreaterEqual(table.rowCount(), 1, dialog.status.text())
        dialog.group_character.setText('Ryder')
        self._widget(dialog, QPushButton, 'Confirmar grupo').click()
        self.assertEqual(self.identity.assignments, [('report-1', 'group-1', 'Ryder')])
        dialog.close()

    def test_voice_reference_catalogue_lists_and_auditions_saved_reference(self):
        self.review.add_reference('CJ', self.reference, 'manual transcript',
                                  {'source': 'user_selected_original'})
        dialog = VoiceIdentityDialog(self.context, records=[self.record])
        self.assertTrue(dialog.wait_for_index(3000))
        self.assertTrue(any('CJ' in dialog.reference_list.item(i).text()
                            for i in range(dialog.reference_list.count())), dialog.status.text())
        dialog.reference_list.setCurrentRow(0)
        self._widget(dialog, QPushButton, 'Ouvir referência').click()
        self.player.play.assert_called_once()
        stored = Path(self.player.play.call_args.args[0])
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.parent.name, 'references')
        dialog.close()

    def test_unknown_dialog_key_fails_clearly(self):
        with self.assertRaises(ValueError):
            create_library_dialog('unknown', self.context)

    def test_virtual_library_model_populates_and_filters_25k_rows_quickly(self):
        rows = [{'source': 'Original' if index % 2 else 'Mod',
                 'audio_id': 'SCRIPT:B%03d:S%03d' % (index % 1000, index % 100),
                 'path': 'C:/audio/clip_%05d.wav' % index,
                 'mapping': 'MAIN/SOUND_%05d' % index,
                 'character': 'CJ' if index % 3 else '',
                 'provenance': 'fixture'} for index in range(25000)]
        model = _LibraryTableModel(('Origem', 'Áudio', 'Arquivo', 'Vínculo', 'Personagem'),
            (lambda row: row['source'], lambda row: row['audio_id'], lambda row: row['path'],
             lambda row: row['mapping'], lambda row: row['character']),
            ('source', 'audio_id', 'path', 'mapping', 'character', 'provenance'))
        import time
        started = time.perf_counter()
        model.set_rows(rows)
        elapsed = time.perf_counter() - started
        self.assertEqual(model.rowCount(), 25000)
        self.assertLess(elapsed, 2.0, '25k virtual rows should populate without per-cell widgets')
        model.set_filter('sound_24999', 'Original')
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.row_at(0)['mapping'], 'MAIN/SOUND_24999')

    def test_library_uses_exact_gameaudio_cache_before_resolver(self):
        class CachedGameAudio:
            def original_para(self, _info):
                return self.path

            def __init__(self, path):
                self.path = path

        self.context.game_audio = CachedGameAudio(self.original)
        dialog = AudioLibraryDialog(self.context, records=[{**self.record, 'info': {'stable_audio_id': 'SCRIPT:B001:S002'}}])
        self.assertTrue(dialog.wait_for_index(3000))
        original_rows = [row for row in dialog.rows if row['source'] == 'Original']
        self.assertEqual(len(original_rows), 1)
        self.assertIn('GameAudio', original_rows[0]['provenance'])
        dialog.close()

    def test_packed_mod_source_uses_existing_installation_proof(self):
        class PackedGameAudio:
            def original_para(self, _info):
                return None

            def mod_ativo(self):
                return 'FixtureMod'

            def modloader_dir(self):
                return self.root

            def ref_para(self, _info):
                return None

        game = PackedGameAudio()
        game.root = self.root
        self.context.game_audio = game
        from studio.ui_qt import audio_sync
        from unittest.mock import patch
        with patch.object(audio_sync, 'installed_line_evidence', return_value={
                'path': self.mod, 'verified': True, 'status': 'verified'}):
            dialog = AudioLibraryDialog(self.context, records=[{
                **self.record, 'info': {'stable_audio_id': 'SCRIPT:B001:S002'},
            }])
            self.assertTrue(dialog.wait_for_index(3000))
            mods = [row for row in dialog.rows if row['source'] == 'Mod']
            self.assertEqual(len(mods), 1)
            self.assertEqual(mods[0]['path'], self.mod.resolve())
            self.assertIn('manifesto', mods[0]['provenance'])
            dialog.close()

    def test_mod_library_uses_open_gxt_selection_instead_of_stale_record_or_config(self):
        class SelectedGameAudio:
            def __init__(self, root):
                self.root = root

            def original_para(self, _info):
                return None

            def mod_ativo(self):
                return 'ConfiguredMod'

            def modloader_dir(self):
                return self.root / 'modloader'

            def ref_para(self, _info):
                return ('BankA', 1, 2)

        game = SelectedGameAudio(self.root)
        self.context.game_audio = game
        self.context.selected_mod = lambda: 'OpenGxtMod'
        selected_path = (game.modloader_dir() / 'OpenGxtMod' / 'audio' / 'SFX' /
                         'BankA' / 'Bank_001' / 'sound_002.wav')
        selected_path.parent.mkdir(parents=True)
        self._wav(selected_path)
        dialog = AudioLibraryDialog(self.context, records=[self.record])
        self.assertTrue(dialog.wait_for_index(3000))
        mods = [row for row in dialog.rows if row['source'] == 'Mod']
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0]['path'], selected_path.resolve())
        self.assertIn('OpenGxtMod', str(mods[0]['path']))
        dialog.close()

    def test_packed_mod_lookup_receives_open_gxt_selection(self):
        class SelectedGameAudio:
            def original_para(self, _info):
                return None

            def mod_ativo(self):
                return 'ConfiguredMod'

            def modloader_dir(self):
                return self.root

        game = SelectedGameAudio()
        game.root = self.root
        self.context.game_audio = game
        self.context.selected_mod = lambda: 'OpenGxtMod'
        from studio.ui_qt import audio_sync
        from unittest.mock import patch
        with patch.object(audio_sync, 'installed_line_evidence', return_value={
                'path': self.mod, 'verified': True, 'status': 'verified'}) as evidence_lookup:
            from studio.ui_qt.library import _packed_mod_path
            path, _ = _packed_mod_path(self.context, {'stable_audio_id': 'SCRIPT:B001:S002'})
        self.assertEqual(path, self.mod.resolve())
        self.assertEqual(evidence_lookup.call_args.kwargs['mod'], 'OpenGxtMod')

    def test_unresolved_record_audition_resolves_in_background_without_catalogue_export(self):
        calls = []
        def resolve(record):
            calls.append(record)
            return self.original
        self.context.original_path = resolve
        self.context.game_audio = SimpleNamespace(original_para=lambda _info: None,
                                                  indexar=lambda *_a, **_k: self.fail('catalogue must not extract'))
        dialog = AudioLibraryDialog(self.context, records=[{**self.record, 'info': {'stable_audio_id': 'SCRIPT:B001:S002'}}])
        self.assertTrue(dialog.wait_for_index(3000))
        dialog.table.selectRow(0)
        dialog.play_selected()
        self.assertIn('segundo plano', dialog.status.text())
        worker = dialog._resolve_worker
        loop = QEventLoop()
        worker.resolved.connect(lambda *_: loop.quit())
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        self.app.processEvents()
        self.assertEqual(len(calls), 1)
        self.player.play.assert_called_once_with(self.original.resolve())
        dialog.close()


if __name__ == '__main__':
    unittest.main()
