import tempfile
import unittest
from pathlib import Path

from studio.tts.legacy import LegacyEngine


class LegacyCommandTests(unittest.TestCase):
    def make_engine(self, engine_id):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yield LegacyEngine(engine_id, engine_id, Path('python'), Path('script.py'), root)

    def test_qwen_command_receives_snapshot_and_isolated_output(self):
        engine = next(self.make_engine('qwen'))
        command = engine.build_command(
            mission='INTRO1', gxt_path=engine.root / 'snapshot.gxt',
            output_dir=engine.root / 'studio_runs' / 'abc' / 'output',
            jamcrc='1234ABCD', event='VO_AA', limit=1, max_speedup=1.25,
            exaggeration=0.7,
        )
        self.assertEqual(command[2:], [
            '--mission', 'INTRO1', '--gxt', str(engine.root / 'snapshot.gxt'),
            '--output-dir', str(engine.root / 'studio_runs' / 'abc' / 'output'),
            '--jamcrc', '1234ABCD', '--event', 'VO_AA', '--limit', '1',
            '--max-speedup', '1.25',
        ])
        self.assertNotIn('--exaggeration', command)

    def test_chatterbox_exposes_only_its_real_cli_controls(self):
        engine = next(self.make_engine('chatterbox'))
        command = engine.build_command(
            mission='INTRO2', gxt_path='snapshot.gxt', output_dir='run/out',
            max_speedup=1.5, exaggeration=0.6,
        )
        self.assertIn('--exaggeration', command)
        self.assertIn('0.60', command)
        self.assertIn('--max-speedup', command)
        self.assertNotIn('--reference-audio', command)
        self.assertNotIn('--seed', command)

    def test_unproven_engine_has_no_command_adapter(self):
        with self.assertRaises(ValueError):
            engine = next(self.make_engine('piper'))
            engine.build_command('INTRO1', 'a.gxt', 'out')

    def test_speed_controls_reject_nan_and_infinity(self):
        engine = next(self.make_engine('qwen'))
        for value in (float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.build_command('INTRO1', 'a.gxt', 'out', max_speedup=value)

    def test_relative_paths_are_resolved_from_engine_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = LegacyEngine('qwen', 'Qwen', Path('python'), Path('script.py'), root)
            command = engine.build_command('INTRO1', 'snapshot.gxt', 'run/output')
            self.assertEqual(command[command.index('--gxt') + 1], str(root / 'snapshot.gxt'))
            self.assertEqual(command[command.index('--output-dir') + 1], str(root / 'run' / 'output'))


if __name__ == '__main__':
    unittest.main()
