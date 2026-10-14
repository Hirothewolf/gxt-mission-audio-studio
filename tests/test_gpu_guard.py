import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import omnivoice_tts as runner  # noqa: E402

RUNNER_SOURCE = '''
from pathlib import Path
MODEL_SOURCE = Path(__file__).parent / 'src'
MODEL_CACHE = Path(__file__).parent / 'cache'
MODEL_REVISION = 'rev1'
STATUS = {'memory_free_mib': 5800, 'utilization_percent': 14}
def runtime_pins(): return {}
def gpu_status():
    if STATUS is None: raise OSError('nvidia-smi ausente')
    return dict(STATUS)
'''


def make_project(tmp):
    root = Path(tmp)
    runner_dir = root / 'deliverables' / 'omnivoice_opening_pacing_v2'
    runner_dir.mkdir(parents=True)
    (runner_dir / 'run_omnivoice_opening_pacing_v2.py').write_text(RUNNER_SOURCE, encoding='utf-8')
    (runner_dir / 'src').mkdir()
    (runner_dir / 'cache' / 'models--k2-fsa--OmniVoice' / 'snapshots' / 'rev1').mkdir(parents=True)
    return root


class GpuGuardTests(unittest.TestCase):
    def test_default_guard_is_on_with_the_original_limits(self):
        self.assertEqual(runner._gpu_guard({}), {'enabled': True, 'min_free_mib': 5120, 'max_utilization_percent': 10})
        self.assertFalse(runner._gpu_guard({'gpu_guard': {'enabled': False}})['enabled'])

    def test_busy_gpu_is_refused_when_guard_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                runner._load_runtime(make_project(tmp), runner._gpu_guard({}))
            self.assertIn('GPU fora do limite', str(ctx.exception))
            self.assertIn('Desative a trava', str(ctx.exception))

    def test_guard_off_skips_the_check_even_when_gpu_reading_fails(self):
        for status in ('{"memory_free_mib": 100, "utilization_percent": 99}', 'None'):
            with tempfile.TemporaryDirectory() as tmp:
                root = make_project(tmp)
                path = root / 'deliverables' / 'omnivoice_opening_pacing_v2' / 'run_omnivoice_opening_pacing_v2.py'
                path.write_text(RUNNER_SOURCE.replace("{'memory_free_mib': 5800, 'utilization_percent': 14}", status),
                                encoding='utf-8')
                fake = MagicMock()
                with patch.dict(sys.modules, {'torch': fake, 'omnivoice': fake}):
                    result = runner._load_runtime(root, {'enabled': False})
                self.assertEqual(len(result), 5)


class RequestCarriesGuardTests(unittest.TestCase):
    def request_for(self, enabled):
        import json
        from studio import config
        from studio.tts import omnivoice
        with tempfile.TemporaryDirectory() as tmp, patch.dict(config.CFG, {'omnivoice_gpu_guard': enabled}):
            root = Path(tmp)
            reference = root / 'ref.wav'
            reference.write_bytes(b'original sample')
            base_gxt = root / 'american.gxt'
            base_gxt.write_bytes(b'original GXT')
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            provenance = {
                'source_kind': 'original_game_saat_cache', 'source_path': str(reference), 'source_sha256': sha(reference),
                'base_english_gxt_path': str(base_gxt), 'base_english_gxt_sha256': sha(base_gxt),
                'base_english_text': 'Original English.', 'base_english_gxt_id': 'INTRO1:DEFE0050'}
            output = root / 'out' / 'take.wav'
            command = omnivoice.build_command(
                root, reference, 'Original English.', 'Texto PT.', output, provenance=provenance,
                python_path=sys.executable, script_path=ROOT / 'tools' / 'omnivoice_tts.py')
            return json.loads(Path(command[-1]).read_text(encoding='utf-8'))

    def test_build_command_writes_the_configured_guard_into_the_request(self):
        self.assertTrue(self.request_for(True)['gpu_guard']['enabled'])
        off = self.request_for(False)['gpu_guard']
        self.assertFalse(off['enabled'])
        self.assertEqual((off['min_free_mib'], off['max_utilization_percent']), (5120, 10))


if __name__ == '__main__':
    unittest.main()
