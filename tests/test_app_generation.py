import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.gxt.model import Gxt
from studio.gxt.codec import encode_text, decode_text_c


def fixture_gxt():
    text = b'Old text\0'
    block = b'TKEY' + struct.pack('<I', 8) + struct.pack('<II', 0, 123)
    block += b'TDAT' + struct.pack('<I', len(text)) + text
    return struct.pack('<HH', 4, 8) + b'TABL' + struct.pack('<I', 12) + b'MAIN\0\0\0\0' + struct.pack('<I', 24) + block


class AppGenerationTests(unittest.TestCase):
    def test_omnivoice_stale_window_error_is_shown_to_the_user(self):
        from types import SimpleNamespace
        from studio.ui.app import _generation_failure_detail
        result = SimpleNamespace(error=None, logs=[
            'Traceback (most recent call last):',
            'ValueError: O pedido veio de uma janela do editor desatualizada para OmniVoice; feche e reabra o GXT Editor antes de gerar.',
        ])
        message = _generation_failure_detail(result, 'omnivoice')
        self.assertIn('Feche e reabra o editor', message)
        self.assertIn('nenhuma referência alternativa', message)

    def test_omnivoice_duration_quarantine_explains_window_and_preserved_take(self):
        import json
        from types import SimpleNamespace
        from studio.ui.app import _generation_failure_detail
        result = SimpleNamespace(error=None, logs=[json.dumps({
            'status': 'QUARANTINED_DURATION', 'duration_seconds': 4.2,
            'window_seconds': 3.1, 'output_path': 'run/quarantine/take.wav',
        })])
        message = _generation_failure_detail(result, 'omnivoice')
        self.assertIn('4.20 s produzidos para 3.10 s disponíveis', message)
        self.assertIn('run/quarantine/take.wav', message)
        self.assertIn('nenhum trecho foi cortado', message)

    def test_success_json_is_not_an_error_message(self):
        from types import SimpleNamespace
        from studio.ui.app import _generation_failure_detail
        result = SimpleNamespace(error=None, returncode=0, logs=['{"status":"UNREVIEWED_WITHIN_WINDOW","output_path":"take.wav"}'])
        self.assertEqual(_generation_failure_detail(result, 'omnivoice'), '')

    def test_validation_error_explains_text_mismatch_without_raw_json(self):
        from studio.ui.app import _generation_validation_detail
        message = _generation_validation_detail({'details': [{'error': 'Texto ou engine não corresponde ao receipt OmniVoice desta fala.'}]})
        self.assertIn('texto', message.lower())
        self.assertIn('gerar', message.lower())
        self.assertNotIn('receipt', message)

    def test_snapshot_contains_current_text_and_preserves_source(self):
        from studio.ui.app import App
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'source.gxt'
            original = fixture_gxt()
            path.write_bytes(original)
            app = App.__new__(App)
            app.g = Gxt(str(path))
            app.g.t['MAIN']['cur'][123] = encode_text('Texto editado')[0]
            app.commit = lambda: True
            app.jobs = type('Jobs', (), {'busy': False})()
            with patch('studio.ui.app.PROJ_ROOT', root), patch('studio.ui.app.select_engine', return_value='engine'):
                engine, run, texts = app._prepare_generation('MAIN', 123)
            snapshot = Gxt(str(run / 'american.gxt'))
            self.assertEqual(decode_text_c(snapshot.t['MAIN']['cur'][123]), 'Texto editado')
            self.assertEqual(texts, {123: 'Texto editado'})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(engine, 'engine')

    def test_each_review_retry_gets_a_new_run_and_output_path(self):
        from studio.ui.app import App
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'source.gxt'
            path.write_bytes(fixture_gxt())
            app = App.__new__(App)
            app.g = Gxt(str(path))
            app.commit = lambda: True
            app.jobs = type('Jobs', (), {'busy': False})()
            with patch('studio.ui.app.PROJ_ROOT', root), patch('studio.ui.app.select_engine', return_value='engine'):
                _engine, first, first_text = app._prepare_generation('MAIN', 123)
                _engine, second, second_text = app._prepare_generation('MAIN', 123)
            first_output = first / 'output' / 'MAIN' / '0000007B.wav'
            second_output = second / 'output' / 'MAIN' / '0000007B.wav'
            self.assertEqual(first_text, second_text)
            self.assertNotEqual(first, second)
            self.assertNotEqual(first_output, second_output)


if __name__ == '__main__':
    unittest.main()
