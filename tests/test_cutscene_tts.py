import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from studio.tts.cutscene import build_command, local_models

class ExplicitCutsceneTTSTests(unittest.TestCase):
    def setup_project(self, root):
        model = root / 'models/qwen3_tts_1_7b_base'
        model.mkdir(parents=True)
        (model / 'config.json').write_text(json.dumps({'tts_model_type':'base','tts_model_size':'1b7'}))
        (model / 'model.safetensors').write_bytes(b'fixture')
        python = root / '.venv-qwen-local/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'fixture')
        reference = root / 'reference.wav'
        reference.write_bytes(b'fixture')
        return reference

    def test_selection_passes_exact_reference_transcription_and_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = self.setup_project(root)
            command = build_command(root, '1.7B', ref, 'Original words.', 'Texto hipertraduzido.', root/'workspace/cutscene_studio/take.wav')
            for flag, value in [('--reference', str(ref.resolve())), ('--reference-text','Original words.'), ('--text','Texto hipertraduzido.')]:
                self.assertEqual(command[command.index(flag)+1], value)
            self.assertIn('cutscene_tts.py', command[2])
            self.assertEqual([m['label'] for m in local_models(root)], ['1.7B'])

    def test_formatting_is_removed_only_from_spoken_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = self.setup_project(root)
            command = build_command(root, '1.7B', ref, '~z~Original', '~s~Novo', root/'workspace/cutscene_studio/take.wav')
            self.assertEqual(command[command.index('--reference-text')+1], 'Original')
            self.assertEqual(command[command.index('--text')+1], 'Novo')
            with self.assertRaises(ValueError):
                build_command(root, '1.7B', ref, '~z~', 'Novo', root/'workspace/cutscene_studio/take.wav')

    def test_absent_checkpoint_does_not_fallback_or_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = self.setup_project(root)
            with self.assertRaises(ValueError):
                build_command(root, '0.6B', ref, 'Original', 'Novo', root/'workspace/cutscene_studio/take.wav')

    def test_rejects_output_outside_draft_and_empty_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = self.setup_project(root)
            with self.assertRaises(ValueError):
                build_command(root, '1.7B', ref, 'Original', 'Novo', root/'reference.wav')
            with self.assertRaises(ValueError):
                build_command(root, '1.7B', ref, '', 'Novo', root/'workspace/cutscene_studio/take.wav')

    def test_reference_hash_captured_and_stale_selection_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = self.setup_project(root)
            expected = hashlib.sha256(ref.read_bytes()).hexdigest()
            command = build_command(root, '1.7B', ref, 'Original', 'Novo',
                root/'workspace/cutscene_studio/take.wav', reference_sha256=expected)
            self.assertEqual(command[command.index('--reference-sha256')+1], expected)
            ref.write_bytes(b'changed selection')
            with self.assertRaisesRegex(ValueError, 'referência'):
                build_command(root, '1.7B', ref, 'Original', 'Novo',
                    root/'workspace/cutscene_studio/take.wav', reference_sha256=expected)

if __name__ == '__main__': unittest.main()
