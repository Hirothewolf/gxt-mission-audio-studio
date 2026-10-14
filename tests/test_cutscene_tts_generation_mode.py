"""Production-mode contract without loading a model or allocating a GPU."""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from studio.tts.cutscene import build_command


class CutsceneGenerationModeTests(unittest.TestCase):
    def test_editor_command_requests_complete_text_conditioning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'models/qwen3_tts_1_7b_base'
            model.mkdir(parents=True)
            (model / 'config.json').write_text(json.dumps(
                {'tts_model_type': 'base', 'tts_model_size': '1b7'}))
            (model / 'weights.safetensors').write_bytes(b'fixture')
            python = root / '.venv-qwen-local/Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'fixture')
            reference = root / 'reference.wav'
            reference.write_bytes(b'fixture')
            command = build_command(root, '1.7B', reference, 'Original speech.',
                                    'Fala aprovada.', root / 'workspace/cutscene_studio/new.wav')
            self.assertIn('--non-streaming-mode', command)

    def test_runtime_uses_same_conditioning_and_records_it(self):
        try:
            import numpy as np
            import soundfile
        except ImportError:
            self.skipTest('Run with the configured Qwen runtime for NumPy and soundfile.')
        path = Path(__file__).resolve().parents[1] / 'tools/cutscene_tts.py'
        spec = importlib.util.spec_from_file_location('cutscene_tts_mode_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model = SimpleNamespace(create_voice_clone_prompt=Mock(return_value='owned voice'),
                                generate_voice_clone=Mock(return_value=([np.full(8000, .1)], 8000)))
        loader = SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=Mock(return_value=model)))
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False),
                                float16='fp16', float32='fp32', inference_mode=nullcontext)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / 'reference.wav'
            with wave.open(str(reference), 'wb') as wav:
                wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\x00\x01' * 8000)
            checkpoint = root / 'model'
            checkpoint.mkdir()
            (checkpoint / 'config.json').write_text(json.dumps({'tts_model_type': 'base'}))
            args = SimpleNamespace(model=str(checkpoint), reference=str(reference),
                                   reference_text='Original speech.', text='Fala aprovada.',
                                   output=str(root / 'new.wav'), device='cpu',
                                   reference_sha256=module.sha(reference), non_streaming_mode=True)
            with patch.dict(sys.modules, {'torch': torch, 'qwen_tts': loader}), \
                    patch.dict(module.os.environ, {}, clear=False):
                report = module.generate(args)
            self.assertIs(model.generate_voice_clone.call_args.kwargs.get('non_streaming_mode'), True)
            self.assertIs(report.get('non_streaming_mode'), True)
            self.assertEqual(report['text'], 'Fala aprovada.')
            self.assertEqual(report['reference_text'], 'Original speech.')
            self.assertEqual(module.sha(reference), args.reference_sha256)


if __name__ == '__main__':
    unittest.main()
