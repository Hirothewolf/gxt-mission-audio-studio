import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from studio.ui.audio_library import AudioLibraryWindow, annotation_key, row_identity
from studio.audio.library_context import base_texts, gxt_entries
from studio.gxt.model import Gxt
from test_app_generation import fixture_gxt


class AudioLibraryIdentityTests(unittest.TestCase):
    def record(self):
        return {'stable_audio_id': 'SCRIPT:B000:S000', 'source': 'project_original',
                'path': 'C:/local/SCRIPT/Bank_001/sound_001.wav', 'status': 'mapped_caption',
                'links': [{'table': 'MAIN', 'hash': 123, 'method': 'gxt_stable_id',
                           'base_text': 'Original source', 'mod_text': 'Texto modificado'}]}

    def test_user_identity_overrides_wiki(self):
        row = self.record()
        store = SimpleNamespace(character=lambda key: 'Identidade manual')
        with patch('studio.ui.audio_library.resolve_speaker') as lookup:
            result = row_identity(row, store)
        self.assertEqual(result['identity'], 'Identidade manual')
        lookup.assert_not_called()

    def test_user_gxt_identity_is_used_when_no_per_audio_override(self):
        row = self.record()
        store = SimpleNamespace(character=lambda key: 'CJ manual' if key == 'MAIN|0000007b' else '')
        self.assertEqual(row_identity(row, store)['identity'], 'CJ manual')

    def test_ambiguous_caption_cannot_be_promoted_to_named_character(self):
        row = self.record()
        row['status'] = 'ambiguous_caption'
        with patch('studio.ui.audio_library.resolve_speaker', return_value={'status': 'matched', 'identity': 'CJ'}):
            self.assertIsNone(row_identity(row)['identity'])

    def test_modded_text_alone_is_not_a_wiki_source(self):
        row = self.record()
        row['links'][0]['base_text'] = None
        with patch('studio.ui.audio_library.resolve_speaker') as lookup:
            self.assertIsNone(row_identity(row)['identity'])
        lookup.assert_not_called()

    def test_generic_voice_keeps_its_kind_and_context_limit(self):
        claim = {'status': 'matched', 'identity': 'Pedestrian', 'speaker_kind': 'voice_archetype',
                 'mission_context_verified': False}
        with patch('studio.ui.audio_library.resolve_speaker', return_value=claim):
            result = row_identity(self.record())
        self.assertEqual(result['speaker_kind'], 'voice_archetype')
        self.assertFalse(result['mission_context_verified'])

    def test_manual_notes_do_not_merge_different_files_with_same_coordinates(self):
        row = self.record()
        another = dict(row, path='C:/another/SCRIPT/Bank_001/sound_001.wav')
        self.assertNotEqual(annotation_key(row), annotation_key(another))


class LibraryContextTests(unittest.TestCase):
    def test_stale_library_never_queries_or_displays_previous_captions(self):
        window = Mock(closed=False, path=Mock(is_file=lambda: True))
        window.context_current.return_value = False
        AudioLibraryWindow.refresh(window)
        window.clear_stale.assert_called_once()
        window.inventory.query.assert_not_called()

    def test_actions_cannot_use_a_selected_row_after_gxt_changes(self):
        window = Mock()
        window.app.commit.return_value = True
        window.context_current.return_value = False
        self.assertIsNone(AudioLibraryWindow.selected(window))
        window.clear_stale.assert_called_once()
        window.tree.selection.assert_not_called()

    def test_base_text_comes_from_original_file_not_modded_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'american.gxt'
            path.write_bytes(fixture_gxt())
            translated = Gxt(path)
            translated.t['MAIN']['cur'][123] = b'Translated source'
            app = SimpleNamespace(jogo=SimpleNamespace(info={'gxt': path}), g=translated)
            self.assertEqual(base_texts(app)[('MAIN', 123)], 'Old text')
            self.assertEqual(gxt_entries(translated)[0]['text'], 'Translated source')


if __name__ == '__main__':
    unittest.main()
