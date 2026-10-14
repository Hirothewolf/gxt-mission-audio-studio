import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from studio.projects.review import ReviewStore
from studio.ui.audition import BatchAuditionWindow
from test_review import make_wav


class BatchAuditionUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        directory = Path(self.tmp.name)
        self.wav = make_wav(directory / 'original.wav')
        self.store = ReviewStore(directory)
        self.app = SimpleNamespace(
            root=self.root, g=SimpleNamespace(t={'MAIN': {'cur': {
                1: b'fala', 2: b'fala', 3: b'fala'}}}), sel=None,
            review_store=self.store, parar_audio=Mock(), wav_original=Mock(return_value=self.wav),
            _tocar=Mock(), player=SimpleNamespace(play_sequence=Mock()),
            limpar_cache_dublado=Mock(), atualizar_linha=Mock(), _carregar_linha=Mock())
        self.imported = []
        for h, metadata in [(1, {'wer': .8}), (2, {'wer': .9, 'duration_override_required': True}),
                            (3, {'wer': .05})]:
            line_id = 'MAIN|%08x' % h
            c = self.store.import_candidate(line_id, self.wav, 'fala', 'test', metadata)
            self.imported.append({'line_id': line_id, 'candidate_id': c['id']})
        self.window = BatchAuditionWindow(self.app, self.imported)
        self.window.win.withdraw()
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()

    def test_player_buttons_and_collective_wer_acceptance(self):
        self.window.play_original()
        self.app._tocar.assert_called_with(self.wav, 'original')
        self.window.play_generated()
        self.assertEqual(self.app._tocar.call_args.args[1], 'gerado')
        self.window.compare()
        self.assertEqual(self.app.player.play_sequence.call_args.args[0][0], self.wav)
        self.window.accept_wer()
        statuses = [self.store.list_candidates(i['line_id'], 'fala')[0]['status'] for i in self.imported]
        self.assertEqual(statuses, ['approved', 'pending', 'pending'])
        self.window.tree.selection_set('1')
        self.window.select()
        self.window.override.set(True)
        self.window.decide('approved')
        self.assertEqual(self.store.list_candidates('MAIN|00000002', 'fala')[0]['status'], 'approved')

    def test_collective_confirmation_cannot_accept_changed_text(self):
        self.app.g.t['MAIN']['cur'][1] = b'outro texto'
        self.window.accept_wer()
        self.assertEqual(self.store.list_candidates('MAIN|00000001', 'outro texto')[0]['status'], 'pending')

    def test_approval_persists_on_reopen_and_does_not_touch_original(self):
        before = self.wav.read_bytes()
        self.window.decide('approved')
        reopened = ReviewStore(self.tmp.name)
        self.assertIsNotNone(reopened.approved_path('MAIN|00000001', 'fala'))
        self.assertEqual(self.wav.read_bytes(), before)

    def test_changed_gxt_context_blocks_collective_approval_without_crashing(self):
        self.app.g = SimpleNamespace(t={})
        self.window.accept_wer()
        self.assertEqual(self.store.list_candidates('MAIN|00000001', 'fala')[0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
