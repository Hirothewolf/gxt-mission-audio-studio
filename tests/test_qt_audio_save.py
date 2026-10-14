import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from studio.projects.review import ReviewStore
from studio.ui_qt.audio_save import PendingAudio, apply_selected


class AudioSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.path = self.root / 'american.gxt'
        self.queue = PendingAudio(self.root)
        self.store = ReviewStore(self.root)
        self.wav = self.root / 'source.wav'
        # A valid, nonempty PCM fixture, never game data.
        import wave, struct, math
        with wave.open(str(self.wav), 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<h', round(1000 * math.sin(i / 10))) for i in range(800)))
        self.record = dict(table='MAIN', hash=0x1234, line_id='MAIN|00001234', text='Texto')

    def test_only_explicit_choice_is_installed_not_latest_take(self):
        first = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'Primeiro')
        later = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'Outro')
        self.queue.select(self.path, self.record, first)
        selected = []
        def install(records, progress):
            selected.append(self.store.data['lines'][self.record['line_id']]['approved'])
            return dict(installed=[self.record['line_id']], errors=[], skipped=[])
        context = SimpleNamespace(review_store=self.store, current_text=lambda r: 'Texto', install=install)
        snapshot = self.queue.choices(self.path)
        result = apply_selected(context, snapshot)
        self.assertEqual(selected, [first['id']])
        self.assertNotEqual(selected, [later['id']])
        self.queue.complete(self.path, snapshot, result['installed'])
        self.assertEqual(self.queue.choices(self.path), {})

    def test_restart_failure_and_changed_text_preserve_pending(self):
        candidate = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'Teste')
        self.queue.select(self.path, self.record, candidate)
        reloaded = PendingAudio(self.root)
        context = SimpleNamespace(review_store=self.store, current_text=lambda r: 'Novo texto', install=Mock())
        result = apply_selected(context, reloaded.choices(self.path))
        context.install.assert_not_called()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(reloaded.choices(self.path)), 1)

    def test_finished_save_does_not_drop_newer_selection(self):
        first = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'A')
        second = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'B')
        self.queue.select(self.path, self.record, first)
        snapshot = self.queue.choices(self.path)
        self.queue.select(self.path, self.record, second)
        self.queue.complete(self.path, snapshot, [self.record['line_id']])
        self.assertEqual(self.queue.choices(self.path)[self.record['line_id']]['candidate_id'], second['id'])

    def test_without_selection_audio_is_untouched(self):
        context = SimpleNamespace(install=Mock())
        self.assertEqual(apply_selected(context, {})['installed'], [])
        context.install.assert_not_called()

    def test_preview_uses_pending_choice_before_save_instead_of_previous_approval(self):
        from studio.ui_qt.context import QtContext
        approved = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'Anterior')
        chosen = self.store.import_candidate(self.record['line_id'], self.wav, 'Texto', 'Escolhido')
        self.store.set_status(self.record['line_id'], approved['id'], 'approved', 'Texto')
        self.queue.select(self.path, self.record, chosen)
        context = QtContext.__new__(QtContext)
        context.review_store, context.pending_audio = self.store, self.queue
        context.document = SimpleNamespace(path=self.path)
        self.assertEqual(context.chosen_path(self.record['line_id'], 'Texto'), self.store.resolve_path(chosen['path']))
        self.assertEqual(context.approved_path(self.record['line_id'], 'Texto'), self.store.resolve_path(approved['path']))
