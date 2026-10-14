import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QMessageBox

from studio.audio import cutscene_batch as batch
from studio.ui_qt.cutscene_batch import CutsceneBatchDialog


class _Gxt:
    def __init__(self, path, current='Olá'):
        self.path = str(path)
        encoded = b'Ol\x98' if current == 'Olá' else current.encode('ascii')
        self.t = {'MAIN': {'orig': {0x1234: b'Hello there'}, 'cur': {0x1234: encoded}}}


class _GameAudio:
    def __init__(self, game_dir):
        self.game_dir = str(game_dir)

    def saat(self):
        return {'stream': 'fake-saat.exe'}


class QtCutsceneBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.game = self.root / 'game'
        self.game.mkdir()
        self.gxt_path = self.root / 'current.gxt'
        self.gxt_path.write_bytes(b'fake gxt')
        self.current = _Gxt(self.gxt_path)
        self.document = SimpleNamespace(gxt=self.current, comparison=None)
        self.context = SimpleNamespace(project_root=self.root, game_audio=_GameAudio(self.game), document=self.document)
        self.report = {
            'id': 'native-1',
            'sources': {'gxt': {'path': str(self.root / 'base.gxt'), 'sha256': 'base-hash'},
                        'stream': {'path': str(self.game / 'audio/streams/CUTSCENE'), 'sha256': 'archive-hash'}},
            'records': [{'pak': 'CUTSCENE', 'stream': {'track': 'Track_001'},
                         'gxt_stable_id': 'MAIN:00001234', 'caption_start_ms': 0,
                         'caption_duration_ms': 3000, 'event_name': 'TEST_LINE', 'scene': 'TEST_SCENE'}],
        }
        self.track = {'track': 'Track_001', 'path': self.root / 'Track_001.ogg', 'sha256': 'track-hash'}
        cache_manifest = self.root / '.cache' / 'cutscene_original' / 'archive-hash'[:20] / 'source.json'
        cache_manifest.parent.mkdir(parents=True)
        cache_manifest.write_text('{}', encoding='utf-8')
        self.plan_item = {'id': 'row-1', 'track': 'Track_001', 'status': 'READY', 'reasons': [],
                          'final_reasons': [], 'outcome': 'OK', 'key': 'TEST_LINE', 'start': 0, 'end': 3,
                          'cps': None, 'en_text': 'Hello there', 'pt_text': 'Olá'}

    def tearDown(self):
        self.temp.cleanup()

    def _wait(self, dialog, timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline and dialog._busy:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        return not dialog._busy

    def _load_patches(self, captured=None, list_tracks=None):
        def plan_all(report, lookup, durations, policy, gxt_paths):
            if captured is not None:
                captured.append(lookup('MAIN', 0x1234))
            return {'Track_001': [dict(self.plan_item)]}
        return (
            patch('studio.ui_qt.cutscene_batch.load_current', return_value=self.report),
            patch('studio.ui_qt.cutscene_batch.audio.list_tracks', return_value=list_tracks or [self.track]),
            patch('studio.ui_qt.cutscene_batch.Gxt', return_value=SimpleNamespace(t={
                'MAIN': {'orig': {0x1234: b'Hello there'}}})),
            patch('studio.ui_qt.cutscene_batch.audio.digest', return_value='base-hash'),
            patch('studio.ui_qt.cutscene_batch.batch.plan_all', side_effect=plan_all),
        )

    def test_autoload_plans_current_gxt_snapshot_and_never_starts_generation(self):
        captured = []
        patches = self._load_patches(captured)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch('studio.ui_qt.cutscene_batch.batch.run_track') as run_track:
            dialog = CutsceneBatchDialog(self.context, autoload=True)
            self.assertTrue(self._wait(dialog))
            self.assertEqual(captured, [('Hello there', 'Olá')])
            self.assertEqual(dialog.track_table.rowCount(), 1)
            self.assertEqual(len(dialog.plans['Track_001']), 1)
            run_track.assert_not_called()
            dialog.close()

    def test_tracks_spec_selects_only_exact_available_tracks(self):
        patches = self._load_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            dialog = CutsceneBatchDialog(self.context, autoload=True,
                                         tracks_spec=['Track_001', 'Track_999'])
            self.assertTrue(self._wait(dialog))
            self.assertEqual(dialog.selected_tracks(), ['Track_001'])
            dialog.close()

    def test_plan_uses_snapshot_captured_before_worker_starts(self):
        captured = []
        patches = self._load_patches(captured)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            dialog = CutsceneBatchDialog(self.context, autoload=False)
            original_plan = dialog._plan_sync
            gate = __import__('threading').Event()
            def delayed_plan(*args, **kwargs):
                gate.wait(1)
                return original_plan(*args, **kwargs)
            dialog._plan_sync = delayed_plan
            dialog.autoload()
            self.current.t['MAIN']['cur'][0x1234] = b'Changed after the snapshot'
            gate.set()
            self.assertTrue(self._wait(dialog))
            self.assertEqual(captured, [('Hello there', 'Olá')])
            dialog.close()

    def test_missing_legacy_cache_uses_existing_extractor_before_listing_tracks(self):
        (self.root / '.cache' / 'cutscene_original' / 'archive-hash'[:20] / 'source.json').unlink()
        listing = Mock(side_effect=[ValueError('Cache CUTSCENE original ausente.'), [self.track]])
        patches = self._load_patches(list_tracks=[self.track])
        with patches[0], patch('studio.ui_qt.cutscene_batch.audio.list_tracks', listing), patches[2], patches[3], patches[4], \
                patch('studio.ui_qt.cutscene_batch.extract_cutscene', return_value=self.root / 'cache') as extraction:
            dialog = CutsceneBatchDialog(self.context, autoload=True)
            self.assertTrue(self._wait(dialog))
            extraction.assert_called_once_with(self.game, self.root, 'fake-saat.exe')
            listing.assert_called_once()
            dialog.close()

    def test_run_requires_explicit_action_and_persists_cancelled_partial_report(self):
        patches = self._load_patches()
        result = dict(track='Track_001', items=[dict(self.plan_item, outcome='MISSING_PTBR')],
                      cancelled=True, render=None, draft=None)
        def run_track(project, report, track, items, synthesize, policy, cancel_event=None, on_progress=None):
            cancel_event.set()
            report_path = batch.batch_dir(self.root, 'Track_001') / 'report.json'
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result), encoding='utf-8')
            raise batch.CancelledError('cancelled')
        with (patches[0], patches[1], patches[2], patches[3], patches[4],
              patch('studio.ui_qt.cutscene_batch.batch.omnivoice_disponivel', return_value=(True, '')),
              patch('studio.ui_qt.cutscene_batch.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes),
              patch('studio.ui_qt.cutscene_batch.batch.omnivoice_factory', return_value=object()),
              patch('studio.ui_qt.cutscene_batch.batch.run_track', side_effect=run_track),
              patch('studio.ui_qt.cutscene_batch.batch.write_summary_csv', return_value=self.root / 'summary.csv') as write_csv):
            dialog = CutsceneBatchDialog(self.context, autoload=True)
            self.assertTrue(self._wait(dialog))
            self.assertFalse(dialog.results)
            dialog.track_table.selectRow(0)
            dialog.run_selected()
            self.assertTrue(self._wait(dialog))
            self.assertTrue(dialog.results.get('Track_001', {}).get('cancelled'),
                            '%s | %s' % (dialog.status.text(), dialog.errors))
            write_csv.assert_called_once()
            dialog.close()


if __name__ == '__main__':
    unittest.main()
