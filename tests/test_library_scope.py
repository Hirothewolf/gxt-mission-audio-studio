import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studio.audio.inventory import Inventory, build_inventory
from studio.audio.library_context import SCOPE_PROFILES, inventory_roots
from studio.config import CFG


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        out.writeframes(b"\x00\x00\x01\x00")


class FakeGameAudio:
    def __init__(self, root):
        self.root = Path(root)

    def estado_cache(self, _pak):
        return {"indexado": True, "desatualizado": False}

    def cache_dir(self):
        return self.root / "original-cache"

    def cache_dir_mod(self):
        return self.root / "mod-cache"

    def _cache_mod_atual(self, _mod, _pak):
        return True

    def mod_ativo(self):
        return "active"

    def modloader_dir(self):
        return self.root / "game" / "modloader"


class LibraryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_custom = CFG.get("audio_search_roots")
        CFG["audio_search_roots"] = []

    def tearDown(self):
        if self.old_custom is None:
            CFG.pop("audio_search_roots", None)
        else:
            CFG["audio_search_roots"] = self.old_custom
        self.temp.cleanup()

    def test_profiles_cover_script_npc_spcs_dialogue_and_all_sfx(self):
        spcs = {"SPC_EA", "SPC_FA", "SPC_GA", "SPC_NA", "SPC_PA"}
        self.assertEqual(set(SCOPE_PROFILES["script"]), {"SCRIPT"})
        self.assertEqual(set(SCOPE_PROFILES["npc_spcs"]), spcs)
        self.assertEqual(set(SCOPE_PROFILES["dialogue"]), spcs | {"SCRIPT"})
        self.assertEqual(set(SCOPE_PROFILES["all_sfx"]), {
            "FEET", "GENRL", "PAIN_A", "SCRIPT", *spcs,
        })

    def test_default_roots_only_include_script_and_never_walk_spc_directories(self):
        project = self.root / "project"
        script = project / "extracted" / "audio" / "sfx" / "SCRIPT"
        spc = project / "extracted" / "audio" / "sfx" / "SPC_GA"
        write_wav(script / "Bank_001" / "sound_001.wav")
        write_wav(spc / "Bank_001" / "sound_001.wav")
        roots = inventory_roots(None, project)
        self.assertEqual({Path(item["path"]) for item in roots}, {script.resolve()})

        real_walk = __import__("os").walk
        walked = []

        def recording_walk(path):
            walked.append(Path(path).resolve())
            return real_walk(path)

        with patch("studio.audio.inventory.os.walk", side_effect=recording_walk):
            summary = build_inventory(self.root / "index.sqlite3", roots, scope_paks=("SCRIPT",))
        self.assertEqual(summary["sound_count"], 1)
        self.assertTrue(walked)
        self.assertFalse(any(path == spc.resolve() for path in walked))
        inventory = Inventory(self.root / "index.sqlite3")
        self.assertTrue(inventory.scope_matches(("SCRIPT",)))
        self.assertFalse(inventory.scope_matches(("SCRIPT", "SPC_GA")))
        inventory.close()

    def test_scope_metadata_preserves_explicit_empty_selection(self):
        index = self.root / "empty.sqlite3"
        summary = build_inventory(index, [], scope_paks=())
        self.assertEqual(summary["scope_paks"], [])
        inventory = Inventory(index)
        self.assertTrue(inventory.scope_matches(()))
        self.assertFalse(inventory.scope_matches(("SCRIPT",)))
        inventory.close()

    def test_selected_paks_filter_project_caches_modloader_and_custom_roots(self):
        project = self.root / "project"
        source_roots = [
            project / "extracted" / "audio" / "sfx",
            project / "extracted" / "audio" / "non_mission_spc",
            self.root / "original-cache" / "export",
            self.root / "mod-cache" / "export",
            self.root / "game" / "modloader" / "active" / "audio" / "SFX",
        ]
        for base in source_roots:
            (base / "SCRIPT").mkdir(parents=True)
            (base / "SPC_EA").mkdir(parents=True)
        custom = self.root / "custom-audio"
        (custom / "SCRIPT").mkdir(parents=True)
        (custom / "SPC_EA").mkdir(parents=True)
        (custom / "loose.wav").write_bytes(b"unclassified")
        CFG["audio_search_roots"] = [str(custom)]
        app = SimpleNamespace(jogo=FakeGameAudio(self.root))

        roots = inventory_roots(app, project, paks=("SCRIPT",))
        self.assertTrue(roots)
        self.assertTrue(all(Path(item["path"]).name.upper() == "SCRIPT" for item in roots))
        self.assertFalse(any("SPC_EA" in Path(item["path"]).parts for item in roots))
        self.assertFalse(any(Path(item["path"]) == custom.resolve() for item in roots))
        self.assertTrue(any(item["source"] == "mod" for item in roots))
        self.assertTrue(any(item["source"] == "custom" for item in roots))

    def test_advanced_all_sfx_is_the_only_scope_that_adds_unclassified_custom_audio(self):
        custom = self.root / "custom-audio"
        custom.mkdir()
        (custom / "ambient.wav").write_bytes(b"unclassified")
        CFG["audio_search_roots"] = [str(custom)]
        default_roots = inventory_roots(None, self.root, paks=SCOPE_PROFILES["script"])
        advanced_roots = inventory_roots(None, self.root, paks=SCOPE_PROFILES["all_sfx"])
        self.assertFalse(any(Path(item["path"]) == custom.resolve() for item in default_roots))
        self.assertTrue(any(Path(item["path"]) == custom.resolve() for item in advanced_roots))

    def test_a_custom_root_already_named_as_selected_pak_is_accepted(self):
        custom = self.root / "custom" / "SCRIPT"
        custom.mkdir(parents=True)
        CFG["audio_search_roots"] = [str(custom)]
        roots = inventory_roots(None, self.root, paks=("SCRIPT",))
        custom_rows = [row for row in roots if row["source"] == "custom"]
        self.assertEqual([Path(row["path"]) for row in custom_rows], [custom.resolve()])


if __name__ == "__main__":
    unittest.main()
