import tempfile
import unittest
from pathlib import Path

from studio.ui.batch_redub import create_run_workspace, prepare_batch_snapshot, select_current_engine, snapshot_texts


class BatchSnapshotTests(unittest.TestCase):
    def test_workspace_contains_validated_gxt_snapshot_and_is_unique(self):
        class FakeGxt:
            path = 'original.gxt'
            order = ['INTRO1']
            t = {'INTRO1': {'hashes': [0x1234], 'cur': {0x1234: b'fala atual'}}}

            def build(self):
                return b'current edited gxt bytes'

            def validar(self, data):
                if data != self.build():
                    raise ValueError('invalid')

        with tempfile.TemporaryDirectory() as tmp:
            first_run, first_snapshot = create_run_workspace(FakeGxt(), Path(tmp))
            second_run, second_snapshot = create_run_workspace(FakeGxt(), Path(tmp))
            self.assertNotEqual(first_run, second_run)
            self.assertEqual(first_snapshot.read_bytes(), b'current edited gxt bytes')
            self.assertTrue(first_snapshot.is_relative_to(first_run))
            self.assertTrue(second_snapshot.is_relative_to(second_run))
            self.assertEqual(snapshot_texts(FakeGxt(), 'INTRO1'), {0x1234: 'fala atual'})

    def test_invalid_gxt_is_not_written(self):
        class InvalidGxt:
            def build(self):
                return b'invalid'

            def validar(self, data):
                raise ValueError('invalid snapshot')

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'invalid snapshot'):
                create_run_workspace(InvalidGxt(), Path(tmp))
            self.assertEqual(list((Path(tmp) / 'workspace' / 'studio_runs').glob('*')), [])

    def test_batch_commits_editor_before_materializing_snapshot(self):
        events = []

        class FakeGxt:
            path = 'original.gxt'
            order = ['INTRO1']
            t = {'INTRO1': {'hashes': [0x1234], 'cur': {0x1234: b'committed'}}}

            def build(self):
                events.append('build')
                return b'committed snapshot'

            def validar(self, data):
                events.append('validate')
                self.assert_data = data

        class FakeApp:
            g = FakeGxt()

            def commit(self):
                events.append('commit')
                return True

        with tempfile.TemporaryDirectory() as tmp:
            run_dir, snapshot, texts = prepare_batch_snapshot(FakeApp(), 'INTRO1', Path(tmp))
            self.assertEqual(events[:3], ['commit', 'build', 'validate'])
            self.assertEqual(snapshot.read_bytes(), b'committed snapshot')
            self.assertEqual(texts, {0x1234: 'committed'})
            self.assertTrue(run_dir.is_dir())

    def test_failed_editor_commit_does_not_build_snapshot(self):
        class FakeGxt:
            def build(self):
                raise AssertionError('must not snapshot unapplied text')

        class FakeApp:
            g = FakeGxt()

            def commit(self):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'aplicada'):
                prepare_batch_snapshot(FakeApp(), 'INTRO1', Path(tmp))
            self.assertFalse((Path(tmp) / 'workspace').exists())

    def test_batch_engine_resolution_uses_current_project_root(self):
        from unittest.mock import patch
        from studio import config
        current_root = Path('new-project-root')
        with patch.object(config, 'PROJ_ROOT', current_root), patch(
            'studio.ui.batch_redub.select_engine', return_value='selected'
        ) as selector:
            self.assertEqual(select_current_engine('auto'), 'selected')
        selector.assert_called_once_with('auto', root=current_root)


if __name__ == '__main__':
    unittest.main()
