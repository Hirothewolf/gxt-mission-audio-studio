import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from studio.audio.inventory import (
    Inventory,
    build_inventory,
    context_fingerprints,
    update_inventory_context_metadata,
)


def write_wav(path, samples=(0, 100, -100), rate=8000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    return path


class AudioInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "inventory.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, roots, **kwargs):
        return build_inventory(self.db, roots, **kwargs)

    def test_keeps_physical_audio_without_caption_and_links_both_gxt_versions(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_002.wav")
        records = [
            {"stable_audio_id": "SCRIPT:B000:S000", "gxt_stable_id": "DIALOG:00000001"},
            {"stable_audio_id": "SCRIPT:B000:S001", "gxt_stable_id": "DIALOG:00000099"},
        ]
        base = [{"table": "DIALOG", "hash": 1, "text": "Base line"}]
        mod = [{"table": "DIALOG", "hash": 1, "text": "Mod line"}]

        summary = self.scan(
            [{"path": audio_root, "source": "original_cache", "label": "Original"}],
            manifest_records=records,
            base_entries=base,
            mod_entries=mod,
        )

        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(summary["sound_count"], 2)
        self.assertEqual(len(rows), 2)
        linked = next(row for row in rows if row["stable_audio_id"] == "SCRIPT:B000:S000")
        unmatched = next(row for row in rows if row["stable_audio_id"] == "SCRIPT:B000:S001")
        self.assertEqual(linked["status"], "mapped_caption")
        self.assertEqual(len(linked["links"]), 1)
        self.assertEqual(linked["links"][0]["base_text"], "Base line")
        self.assertEqual(linked["links"][0]["mod_text"], "Mod line")
        self.assertTrue(all(link["caption_cooccurrence_verified"] is False for link in linked["links"]))
        self.assertEqual(unmatched["status"], "missing_gxt_entry")
        self.assertEqual(unmatched["links"], [])
        self.assertEqual(inventory.count(status="unlinked"), 1)

    def test_jamcrc_fallback_is_ambiguous_when_table_hash_has_multiple_entries(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        summary = self.scan(
            [{"path": audio_root, "source": "project_original"}],
            manifest_records=[{"stable_audio_id": "SCRIPT:B000:S000", "table": "DIALOG", "jamcrc": "0000002a"}],
            base_entries=[
                {"table": "DIALOG", "hash": 42, "text": "Candidate A"},
                {"table": "DIALOG", "hash": 42, "text": "Candidate B"},
            ],
        )
        inventory = Inventory(self.db)
        row = inventory.query(limit=1)[0]
        inventory.close()
        self.assertEqual(summary["collisions"], 1)
        self.assertEqual(row["status"], "ambiguous_caption")
        self.assertEqual(len(row["links"]), 1)
        self.assertEqual(row["links"][0]["base_texts"], ["Candidate A", "Candidate B"])
        self.assertEqual(row["links"][0]["method"], "jamcrc_ambiguous")

    def test_coordinates_are_canonical_per_pak_and_unknown_paths_stay_unclassified(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        write_wav(audio_root / "nested" / "SPC_GA" / "Bank_001" / "sound_001.WAV")
        write_wav(audio_root / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "project_original"}])
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        by_path = {Path(row["path"]).relative_to(audio_root).as_posix(): row for row in rows}
        self.assertEqual(by_path["SCRIPT/Bank_001/sound_001.wav"]["stable_audio_id"], "SCRIPT:B000:S000")
        self.assertEqual(by_path["nested/SPC_GA/Bank_001/sound_001.WAV"]["stable_audio_id"], "SPC_GA:B000:S000")
        self.assertIsNone(by_path["Bank_001/sound_001.wav"]["stable_audio_id"])
        self.assertEqual(by_path["Bank_001/sound_001.wav"]["status"], "unclassified")

    def test_overlapping_roots_do_not_duplicate_a_physical_file_and_specific_source_wins(self):
        general = self.root / "extract"
        specific = general / "SCRIPT"
        wav = write_wav(specific / "Bank_001" / "sound_001.wav")
        self.scan([
            {"path": general, "source": "unknown", "label": "Wide"},
            {"path": specific, "source": "original_cache", "label": "Specific"},
        ])
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(wav.resolve()))
        self.assertEqual(rows[0]["source"], "original_cache")
        self.assertEqual(rows[0]["label"], "Specific")

    def test_duplicate_coordinates_from_distinct_roots_are_preserved(self):
        original = self.root / "original" / "SCRIPT" / "Bank_001" / "sound_001.wav"
        mod = self.root / "mod" / "SCRIPT" / "Bank_001" / "sound_001.wav"
        write_wav(original)
        write_wav(mod, samples=(1, 2, 3))
        self.scan([
            {"path": self.root / "original", "source": "project_original"},
            {"path": self.root / "mod", "source": "mod"},
        ])
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["stable_audio_id"] for row in rows}, {"SCRIPT:B000:S000"})
        self.assertEqual({row["source"] for row in rows}, {"project_original", "mod"})

    def test_invalid_wav_remains_visible_and_malformed_records_do_not_guess_identity(self):
        audio_root = self.root / "audio"
        bad = audio_root / "SPC_PA" / "Bank_001" / "sound_001.wav"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"RIFF broken")
        write_wav(audio_root / "not-a-pak" / "Bank_009" / "sound_004.wav")
        self.scan(
            [{"path": audio_root, "source": "project_original"}],
            manifest_records=[{}, {"stable_audio_id": "SCRIPT:B000:S000", "gxt_stable_id": "broken"}],
        )
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row["status"] == "invalid" and row["stable_audio_id"] == "SPC_PA:B000:S000" for row in rows))
        self.assertTrue(any(row["status"] == "unclassified" and row["stable_audio_id"] is None for row in rows))

    def test_cancelled_rebuild_preserves_the_previous_inventory(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "original_cache"}])
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            self.scan([{"path": self.root / "missing", "source": "mod"}], cancel_event=cancelled)
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "original_cache")

    def test_cancellation_is_observed_during_file_discovery(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "original_cache"}])
        cancelled = threading.Event()
        progress = []

        def cancel_during_discovery(done, total, detail):
            progress.append((done, total, detail))
            if detail == "Localizando arquivos":
                cancelled.set()

        with self.assertRaisesRegex(RuntimeError, "cancel"):
            build_inventory(
                self.db,
                [{"path": audio_root, "source": "mod"}],
                cancel_event=cancelled,
                on_progress=cancel_during_discovery,
            )
        self.assertTrue(progress)
        self.assertEqual(progress[0][2], "Localizando arquivos")
        inventory = Inventory(self.db)
        rows = inventory.query(limit=10)
        inventory.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "original_cache")

    def test_distinct_gxt_links_for_one_audio_identity_are_ambiguous(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        self.scan(
            [{"path": audio_root, "source": "project_original"}],
            manifest_records=[
                {"stable_audio_id": "SCRIPT:B000:S000", "gxt_stable_id": "DIALOG:00000001"},
                {"stable_audio_id": "SCRIPT:B000:S000", "gxt_stable_id": "DIALOG:00000002"},
            ],
            base_entries=[
                {"table": "DIALOG", "hash": 1, "text": "Candidate A"},
                {"table": "DIALOG", "hash": 2, "text": "Candidate B"},
            ],
        )
        inventory = Inventory(self.db)
        row = inventory.query(limit=1)[0]
        inventory.close()
        self.assertEqual(row["status"], "ambiguous_caption")
        self.assertEqual({link["hash"] for link in row["links"]}, {1, 2})

    def test_nested_distinct_pak_directories_do_not_invent_a_coordinate(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "nested" / "SPC_GA" / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "project_original"}])
        inventory = Inventory(self.db)
        row = inventory.query(limit=1)[0]
        inventory.close()
        self.assertIsNone(row["stable_audio_id"])
        self.assertIsNone(row["pak"])
        self.assertEqual(row["status"], "unclassified")

    def test_invalid_and_unlinked_status_filters_and_file_search(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "custom"}])
        inventory = Inventory(self.db)
        self.assertEqual(len(inventory.query(status="unlinked", search="SCRIPT:B000:S000")), 1)
        self.assertEqual(inventory.count(source="custom", pak="SCRIPT"), 1)
        inventory.close()

    def test_context_fingerprints_are_order_independent_but_track_text_and_duplicates(self):
        base = [
            {"table": "dialog", "hash": 12, "text": "Base"},
            {"table": "dialog", "hash": 12, "text": "Base"},
        ]
        mod = [{"table": "dialog", "hash": 12, "text": "Mod"}]
        expected = context_fingerprints(base, mod)
        self.assertEqual(expected, context_fingerprints(list(reversed(base)), list(reversed(mod))))
        self.assertNotEqual(expected, context_fingerprints(base[:1], mod))
        self.assertNotEqual(expected, context_fingerprints(base, [{"table": "dialog", "hash": 12, "text": "Edited unsaved"}]))
        self.assertEqual(set(expected), {
            "base_entries_fingerprint", "mod_entries_fingerprint", "context_fingerprint",
        })

    def test_build_summary_persists_entry_snapshot_fingerprints(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        base = [{"table": "DIALOG", "hash": 1, "text": "Base snapshot"}]
        mod = [{"table": "DIALOG", "hash": 1, "text": "Mod snapshot"}]
        summary = self.scan([{"path": audio_root, "source": "project_original"}], base_entries=base, mod_entries=mod)
        inventory = Inventory(self.db)
        stored = inventory.summary()
        inventory.close()
        self.assertEqual(summary["context_fingerprint"], context_fingerprints(base, mod)["context_fingerprint"])
        self.assertEqual(stored["base_entries_fingerprint"], context_fingerprints(base, mod)["base_entries_fingerprint"])
        self.assertEqual(stored["mod_entries_fingerprint"], context_fingerprints(base, mod)["mod_entries_fingerprint"])

    def test_explicit_context_metadata_update_supports_existing_published_inventory(self):
        audio_root = self.root / "audio"
        write_wav(audio_root / "SCRIPT" / "Bank_001" / "sound_001.wav")
        self.scan([{"path": audio_root, "source": "project_original"}])
        known_base = [{"table": "DIALOG", "hash": 1, "text": "Known snapshot"}]
        known_mod = []
        updated = update_inventory_context_metadata(self.db, known_base, known_mod)
        inventory = Inventory(self.db)
        stored = inventory.summary()
        self.assertTrue(inventory.context_matches(known_base, known_mod)["matches"])
        self.assertEqual(stored["context_fingerprint"], updated["context_fingerprint"])
        edited_base = [{"table": "DIALOG", "hash": 1, "text": "Unsaved edit"}]
        current = context_fingerprints(edited_base, known_mod)
        self.assertNotEqual(stored["context_fingerprint"], current["context_fingerprint"])
        self.assertFalse(inventory.context_matches(edited_base, known_mod)["matches"])
        inventory.close()


if __name__ == "__main__":
    unittest.main()
