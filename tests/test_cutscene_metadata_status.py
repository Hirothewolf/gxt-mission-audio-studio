import unittest
from pathlib import Path

from studio.ui.app import _intro_caption_audio_status


class CutsceneMetadataStatusTests(unittest.TestCase):
    def setUp(self):
        self.caption = {
            'state': 'MAPPED_CUTSCENE_INTRO',
            'gxt_stable_id': 'INTRO1:12345678',
            'stable_audio_id': 'POLICE:PRO@40',
            'bank_num': None,
            'sound_num': None,
            'source_wav_path': None,
        }

    def test_intro_caption_without_script_reference_is_described_as_metadata(self):
        original, mod = _intro_caption_audio_status(self.caption)

        self.assertIn('Original BEATS/Track_010', original)
        self.assertIn('PRO@40', original)
        self.assertIn('trecho ainda não vinculado', original)
        self.assertIn('montagem da faixa necessária', mod)
        self.assertNotIn('WAV não localizado', original)
        self.assertNotIn('não extraído', mod)

    def test_existing_audio_paths_remain_visible_and_script_rows_keep_old_flow(self):
        original, mod = _intro_caption_audio_status(
            self.caption,
            original_path=Path('manual_original.wav'),
            mod_path=Path('installed_mod.wav'),
            mod_name='MeuMod',
        )

        self.assertIn('[WAV ✓ manual_original.wav]', original)
        self.assertEqual(mod, 'ModLoader (MeuMod): [WAV ✓ installed_mod.wav]')

        script_line = dict(self.caption, state='MAPPED_SCRIPT', bank_num=1, sound_num=2)
        self.assertIsNone(_intro_caption_audio_status(script_line))


if __name__ == '__main__':
    unittest.main()
