import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.tts import registry
from studio.tts.registry import select_engine


class LegacyRegistryTests(unittest.TestCase):
    def test_auto_selects_only_a_fully_configured_supported_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / 'python.exe'
            script = root / 'tools' / 'redub_chatterbox.py'
            py.touch()
            script.parent.mkdir()
            script.touch()
            cfg = {'tts_engines': {'chatterbox': {'python': str(py), 'script': str(script)}}}
            with patch.object(registry.config, 'CFG', cfg), patch.object(registry.config, 'PROJ_ROOT', root):
                engine = select_engine()
            self.assertEqual(engine.id, 'chatterbox')
            self.assertEqual(engine.python, py)
            self.assertEqual(engine.script, script)

    def test_explicit_missing_engine_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / 'python.exe'
            script = root / 'redub_qwen.py'
            py.touch()
            script.touch()
            cfg = {
                'engine_padrao': 'Chatterbox',
                'tts_engines': {'qwen': {'python': str(py), 'script': str(script)}}
            }
            with patch.object(registry.config, 'CFG', cfg), patch.object(registry.config, 'PROJ_ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'Chatterbox'):
                    select_engine()

    def test_python_without_script_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / 'python.exe'
            py.touch()
            cfg = {'python_qwen': str(py)}
            with patch.object(registry.config, 'CFG', cfg), patch.object(registry.config, 'PROJ_ROOT', root):
                with self.assertRaises(RuntimeError):
                    select_engine('qwen')
                found = {name: (python, script) for name, python, script in registry.detectar_engines()}
            self.assertFalse(found['Qwen3-TTS'][0] and found['Qwen3-TTS'][1].is_file())

    def test_legacy_config_paths_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / 'python.exe'
            script = root / 'redub_qwen.py'
            py.touch()
            script.touch()
            cfg = {'python_qwen': str(py), 'script_qwen': str(script)}
            with patch.object(registry.config, 'CFG', cfg), patch.object(registry.config, 'PROJ_ROOT', root):
                engine = select_engine('Qwen3-TTS')
            self.assertEqual(engine.id, 'qwen')
            self.assertEqual(engine.script, script)

    def test_f5_checkpoint_is_not_mistaken_for_a_supported_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / 'python.exe'
            script = root / 'tools' / 'redub_f5tts.py'
            py.touch()
            script.parent.mkdir()
            script.touch()
            cfg = {'tts_engines': {'f5': {'python': str(py), 'script': str(script)}}}
            with patch.object(registry.config, 'CFG', cfg), patch.object(registry.config, 'PROJ_ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'F5-TTS não está conectado a um runtime validado'):
                    select_engine('F5-TTS')


if __name__ == '__main__':
    unittest.main()
