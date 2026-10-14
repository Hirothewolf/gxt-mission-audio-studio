import unittest

from studio.script.mapping import PAKS
from studio.ui.archive_extract import (
    EFFECT_PAKS, MISSION_PAKS, NPC_PAKS, archives_for_preset, selection_for_preset,
)


class ArchiveExtractionPresetTests(unittest.TestCase):
    def test_default_mission_preset_selects_only_script(self):
        selected = {pak for pak, checked in selection_for_preset('missoes').items() if checked}
        self.assertEqual(selected, {'SCRIPT'})
        self.assertEqual(MISSION_PAKS, ('SCRIPT',))

    def test_npc_preset_selects_all_spc_archives_only(self):
        selected = {pak for pak, checked in selection_for_preset('npcs').items() if checked}
        self.assertEqual(selected, set(NPC_PAKS))
        self.assertTrue(all(pak.startswith('SPC_') for pak in selected))
        self.assertFalse(selected & set(EFFECT_PAKS))

    def test_dialogue_preset_selects_script_and_npc_archives(self):
        selected = set(archives_for_preset('dialogos'))
        self.assertEqual(selected, {'SCRIPT'} | set(NPC_PAKS))
        self.assertFalse(selected & set(EFFECT_PAKS))

    def test_every_preset_returns_complete_pak_map_with_effects_unchecked(self):
        for name in ('missoes', 'npcs', 'dialogos'):
            selection = selection_for_preset(name)
            self.assertEqual(set(selection), set(PAKS))
            self.assertFalse(any(selection[pak] for pak in EFFECT_PAKS))

    def test_unknown_preset_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, 'desconhecida'):
            archives_for_preset('streams')


if __name__ == '__main__':
    unittest.main()
