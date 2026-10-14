import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from studio import config
from studio.audio import voice_runtime
from studio.ui.app import App
from studio.ui.audio_library import annotation_key, row_identity


class VoiceIdentityPrecedenceTests(unittest.TestCase):
    def app(self, manual=''):
        app = App.__new__(App)
        app.review_store = SimpleNamespace(character=lambda _line_id: manual)
        app.voice_identity_for = Mock(return_value=None)
        return app

    def test_manual_line_identity_precedes_verified_manifest_identity(self):
        app = self.app('CJ manual')
        result = app._speaker_info('MAIN', 0x1234, {
            'speaker_identity': 'Ryder verified',
            'speaker_identity_status': 'VERIFIED',
            'speaker_kind': 'named_character',
        })
        self.assertEqual(result['speaker_identity'], 'CJ manual')
        self.assertEqual(result['speaker_identity_status'], 'MANUAL')
        self.assertEqual(result['speaker_kind'], 'user_override')
        app.voice_identity_for.assert_not_called()

    def test_verified_manifest_identity_precedes_voice_overlay(self):
        app = self.app()
        app.voice_identity_for.return_value = {'identity': 'CJ voice', 'status': 'VOICE_CONFIRMED'}
        result = app._speaker_info('MAIN', 0x1234, {
            'speaker_identity': 'Ryder verified',
            'speaker_identity_status': 'VERIFIED',
        })
        self.assertEqual(result['speaker_identity'], 'Ryder verified')
        self.assertEqual(result['speaker_identity_status'], 'VERIFIED')
        app.voice_identity_for.assert_not_called()

    def test_confirmed_and_suggested_voice_precede_wiki_fallback(self):
        record = {'speaker_identity_status': 'PENDING_LISTENING'}
        for status in ('VOICE_CONFIRMED', 'VOICE_SUGGESTED'):
            with self.subTest(status=status):
                app = self.app()
                app.voice_identity_for.return_value = {'identity': 'Ryder', 'status': status}
                with patch('studio.audio.library_context.base_texts', return_value={}), \
                        patch('studio.script.speakers.resolve_speaker') as resolve:
                    result = app._speaker_info('MAIN', 0x1234, record)
                self.assertEqual(result['speaker_identity'], 'Ryder')
                self.assertEqual(result['speaker_identity_status'], status)
                resolve.assert_not_called()

    def test_wiki_is_used_only_when_manual_verified_and_voice_are_absent(self):
        app = self.app()
        with patch('studio.audio.library_context.base_texts', return_value={('MAIN', 0x1234): 'base text'}), \
                patch('studio.script.speakers.resolve_speaker', return_value={
                    'status': 'matched', 'identity': 'CJ wiki', 'speaker_kind': 'named_character'
                }) as resolve:
            result = app._speaker_info('MAIN', 0x1234, {})
        self.assertEqual(result['speaker_identity'], 'CJ wiki')
        self.assertEqual(result['speaker_identity_status'], 'WIKI_EXACT_TEXT')
        resolve.assert_called_once_with('MAIN', 'base text')


class AudioLibraryVoiceIdentityTests(unittest.TestCase):
    def test_audio_level_manual_annotation_overrides_voice_identity(self):
        record = {'source': 'original_cache', 'stable_audio_id': 'SCRIPT:B001:S010',
                  'path': r'C:\audio\voice.wav', 'status': 'unlinked', 'links': []}
        store = SimpleNamespace(character=lambda key: 'Manual audio' if key == annotation_key(record) else '')
        result = row_identity(record, store, {'identity': 'Suggested voice', 'status': 'VOICE_SUGGESTED'})
        self.assertEqual(result['identity'], 'Manual audio')
        self.assertEqual(result['status'], 'manual')

    def test_line_level_manual_annotation_overrides_voice_identity(self):
        record = {'source': 'original_cache', 'stable_audio_id': 'SCRIPT:B001:S010',
                  'path': r'C:\audio\voice.wav', 'status': 'mapped_caption',
                  'links': [{'table': 'INTRO1', 'hash': 0x1234}]}

        def character(key):
            return 'Manual line' if key == 'INTRO1|00001234' else ''

        result = row_identity(record, SimpleNamespace(character=character),
                              {'identity': 'Confirmed voice', 'status': 'VOICE_CONFIRMED'})
        self.assertEqual(result['identity'], 'Manual line')
        self.assertEqual(result['status'], 'manual')

    def test_voice_suggestion_and_confirmation_work_without_gxt_links(self):
        record = {'source': 'original_cache', 'stable_audio_id': 'SCRIPT:B001:S010',
                  'path': r'C:\audio\voice.wav', 'status': 'unlinked', 'links': []}
        for status in ('VOICE_SUGGESTED', 'VOICE_CONFIRMED'):
            with self.subTest(status=status):
                result = row_identity(record, None, {'identity': 'CJ', 'status': status})
                self.assertEqual(result['identity'], 'CJ')
                self.assertEqual(result['status'], status)
                self.assertEqual(result['speaker_kind'], 'named_character')


class VoiceRuntimeStatusTests(unittest.TestCase):
    def _fake_python(self, project_root):
        candidate = project_root / '.venv-speakers' / 'Scripts' / 'python.exe'
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b'placeholder; runtime discovery must not execute it')
        return candidate.resolve()

    def test_missing_runtime_is_reported_without_creating_setup_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(config.CFG, {'speaker_python': ''}):
            project_root = Path(temp)
            status = voice_runtime.runtime_status(project_root)
            self.assertFalse(status['ready'])
            self.assertEqual(status['state'], 'missing_runtime')
            self.assertFalse(status['can_setup'])
            self.assertFalse(voice_runtime.runtime_root(project_root).exists())

    def test_missing_model_and_setup_command_build_are_read_only(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(config.CFG, {'speaker_python': ''}):
            project_root = Path(temp)
            python = self._fake_python(project_root)
            status = voice_runtime.runtime_status(project_root)
            command = voice_runtime.build_setup_command(project_root)
            self.assertFalse(status['ready'])
            self.assertEqual(status['state'], 'missing_model')
            self.assertEqual(status['python'], str(python))
            self.assertIn('setup', command)
            self.assertFalse(voice_runtime.runtime_root(project_root).exists())

    def test_changed_model_file_invalidates_readiness_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(config.CFG, {'speaker_python': ''}):
            project_root = Path(temp)
            python = self._fake_python(project_root)
            runtime_root = voice_runtime.runtime_root(project_root)
            model_dir = runtime_root / 'model'
            model_dir.mkdir(parents=True)
            signatures = {}
            for name in voice_runtime.MODEL_FILES:
                path = model_dir / name
                path.write_bytes(b'valid model placeholder')
                stat = path.stat()
                signatures['model/' + name] = [stat.st_size, stat.st_mtime_ns]
            ready = {
                'python': str(python),
                'revision': voice_runtime.MODEL_REVISION,
                'model_id': voice_runtime.MODEL_REPO + '@' + voice_runtime.MODEL_REVISION,
                'inference_checked': True,
                'files': signatures,
            }
            (runtime_root / 'ready.json').write_text(json.dumps(ready), encoding='utf-8')
            changed = model_dir / voice_runtime.MODEL_FILES[0]
            changed.write_bytes(b'modified model placeholder')
            before = changed.read_bytes()

            status = voice_runtime.runtime_status(project_root)

            self.assertFalse(status['ready'])
            self.assertIn('alterado', status['reason'].casefold())
            self.assertEqual(changed.read_bytes(), before)
            self.assertTrue((runtime_root / 'ready.json').is_file())


if __name__ == '__main__':
    unittest.main()
