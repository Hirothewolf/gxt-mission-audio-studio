import json
import queue
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tkinter as tk
from tkinter import ttk

from studio.projects.review import ReviewStore
from studio.ui.bulk_audio_review import BulkAudioReviewWindow


class BulkAudioUiTests(unittest.TestCase):
    def test_real_tk_scope_confirmation_and_grouped_apply_in_isolated_project(self):
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as temp:
            store = ReviewStore(temp)
            source = Path(temp) / 'reference.wav'
            with wave.open(str(source), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000)
                f.writeframes(b'\x01\x01' * 800)
            for key in (1, 2):
                store.import_candidate('MAIN|%08x' % key, source, 'Teste', 'AI')
            callbacks = queue.Queue()
            app = SimpleNamespace(root=root, review_store=store,
                g=SimpleNamespace(order=['MAIN'], t={'MAIN': {'cur': {1: b'Teste', 2: b'Teste'}}}),
                script_db=None, tv=ttk.Treeview(root), achar_audio_dublado=lambda *args: None,
                ui=callbacks.put, recarregar_audios_dublados=Mock(), _tocar=Mock(), commit=Mock(return_value=True))
            app.tv.insert('', 'end', iid='MAIN|00000001')
            app.jogo = Mock()
            app.jogo.mod_ativo.return_value = 'FixtureMod'
            app.jogo.planejar.side_effect = lambda items, mod: [dict(x, problemas=[]) for x in items]
            app.jogo.instalar.side_effect = lambda plan, mod: dict(ok=True, instalados=len(plan), backup='fixture')
            real_toplevel = tk.Toplevel
            def hidden(*args, **kwargs):
                win = real_toplevel(*args, **kwargs); win.withdraw(); return win
            with patch('studio.ui.bulk_audio_review.tk.Toplevel', side_effect=hidden):
                dialog = BulkAudioReviewWindow(app)
            def wait():
                until = time.monotonic() + 8
                while time.monotonic() < until:
                    root.update()
                    while not callbacks.empty(): callbacks.get_nowait()()
                    root.update()
                    if not dialog.busy: return
                    time.sleep(.01)
                self.fail('The batch UI did not finish its operation')
            wait()
            self.assertEqual(len(dialog.rows), 1)
            dialog.scope.set('Todo o GXT'); dialog.scan(); wait()
            self.assertEqual(len(dialog.rows), 2)
            self.assertTrue(any('Confirmar todos' in b.cget('text') for b in dialog.buttons))
            with patch('studio.ui.bulk_audio_review.messagebox.askyesno', return_value=True), \
                 patch('studio.ui.bulk_audio_review.messagebox.showinfo'):
                dialog.confirm(True); wait()
            self.assertTrue(all(r['state'] == 'APPROVED' for r in dialog.rows))
            reports = list((store.directory / 'batches').glob('*.json'))
            self.assertEqual(len(reports), 1)
            self.assertEqual(len(json.loads(reports[0].read_text(encoding='utf-8'))['result']['approved']), 2)
            # Give the isolated rows explicit SFX slots; the UI never sees the live game.
            for index, row in enumerate(dialog.rows, 1): row['ref'] = ('SCRIPT', 1, index)
            with patch('studio.ui.bulk_audio_review.messagebox.askyesno', return_value=True), \
                 patch('studio.ui.bulk_audio_review.messagebox.showinfo'):
                dialog.install(); wait()
            self.assertEqual(app.jogo.instalar.call_count, 1)
            self.assertEqual(len(app.jogo.instalar.call_args.args[0]), 2)
            self.assertEqual(app.g.t['MAIN']['cur'], {1: b'Teste', 2: b'Teste'})
            app.g.t['MAIN']['cur'][1] = b'Texto alterado'
            with self.assertRaisesRegex(ValueError, 'textos alterados'):
                dialog.validate_current(dialog.rows)
            dialog.close()


if __name__ == '__main__':
    unittest.main()
