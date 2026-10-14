import json
import tempfile
import unittest
from pathlib import Path

from studio.script.speakers import dialogue_fingerprint, normalize_dialogue, resolve_speaker
from tools.build_speaker_database import extract_records


class SpeakerNormalizationTests(unittest.TestCase):
    def test_normalizes_tags_case_and_typographic_punctuation(self):
        self.assertEqual(
            normalize_dialogue("~z~“Hello…  CJ!”"),
            normalize_dialogue('"hello... CJ!"'),
        )

    def test_keeps_word_spelling_and_punctuation_significant(self):
        self.assertNotEqual(dialogue_fingerprint("its here"), dialogue_fingerprint("it's here"))
        self.assertNotEqual(dialogue_fingerprint("go!"), dialogue_fingerprint("go?"))


class SpeakerDatabaseTests(unittest.TestCase):
    def database(self, records):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "speakers.json"
        path.write_text(json.dumps({"records": records}), encoding="utf-8")
        return path

    def record(self, text, identity, *, confidence="high"):
        return {"fingerprint": dialogue_fingerprint(text), "speaker_identity": identity,
                "confidence": confidence, "evidence": "test source heading",
                "source_url": "https://example.test/wiki#speaker"}

    def test_unique_exact_normalized_match_includes_evidence(self):
        result = resolve_speaker("Big Smoke", "~z~HELLO…", database_path=self.database([
            self.record("Hello...", "Big Smoke")]))
        self.assertEqual((result["status"], result["identity"]), ("matched", "Big Smoke"))
        self.assertFalse(result["mission_context_verified"])
        self.assertIn("source_url", result)

    def test_conflicting_speakers_are_ambiguous(self):
        result = resolve_speaker(None, "Move!", database_path=self.database([
            self.record("Move!", "Speaker A"), self.record("Move!", "Speaker B")]))
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["identity"])

    def test_explicit_override_wins(self):
        result = resolve_speaker(None, "Unknown", overrides={"unknown": "Manual choice"},
                                 database_path=self.database([]))
        self.assertEqual((result["status"], result["identity"]), ("override", "Manual choice"))

    def test_unknown_text_stays_unknown(self):
        result = resolve_speaker(None, "No exact line", database_path=self.database([]))
        self.assertEqual(result["status"], "unknown")

    def test_parser_tracks_section_identity_and_excludes_multi_speaker_line(self):
        source = """==Police==\n==[[Carl Johnson]]==\n*First line!\n==Clerks==\n===[[Ammu-Nation (3D Universe)|Ammu-Nation]]===\n*Second line.\n===Police helicopter===\n*'''Pilot:''' third line. '''Co-Pilot:''' fourth line.\n"""
        records, stats = extract_records(source)
        self.assertEqual({r["speaker_identity"] for r in records}, {"Carl Johnson", "Ammu-Nation (3D Universe)"})
        self.assertEqual(next(r for r in records if r["speaker_identity"] == "Carl Johnson")["speaker_kind"], "named_character")
        self.assertEqual(next(r for r in records if r["speaker_identity"] != "Carl Johnson")["speaker_kind"], "voice_archetype")
        self.assertEqual(stats["excluded_multi_speaker_bullets"], 1)

    def test_embedded_link_heading_joins_adjacent_suffix(self):
        records, _ = extract_records("==Pedestrians==\n=== [[Samoa]]n pedestrian ===\n*Hello!\n")
        self.assertEqual(records[0]["speaker_identity"], "Samoan pedestrian")

    def test_embedded_link_heading_preserves_spaces(self):
        records, _ = extract_records("==Pedestrians==\n===Jamaican [[Taxi Drivers|taxi driver]]===\n*Hello!\n")
        self.assertEqual(records[0]["speaker_identity"], "Jamaican taxi driver")

    def test_explicit_generic_group_is_not_a_named_character(self):
        records, _ = extract_records("==Gangs==\n===[[Grove Street Families]]===\n*Move!\n")
        self.assertEqual(records[0]["speaker_kind"], "group_or_ensemble")

    def test_inline_role_is_marked_as_role_label(self):
        records, _ = extract_records("==Police==\n===Police helicopter===\n*'''Pilot:''' Stop!\n")
        self.assertEqual((records[0]["speaker_identity"], records[0]["speaker_kind"]), ("Pilot", "role_label"))

    def test_action_subheading_keeps_named_parent_and_records_context(self):
        source = "==Main characters==\n===[[Big Smoke]]===\n====Carjacking====\n*Give me that.\n"
        records, _ = extract_records(source)
        self.assertEqual(records[0]["speaker_identity"], "Big Smoke")
        self.assertEqual(records[0]["speaker_kind"], "named_character")
        self.assertEqual(records[0]["source_context"], "Carjacking")

    def test_location_and_setting_subheadings_keep_generic_parent_category(self):
        source = "==Clerks==\n===Clothing shops===\n====Female====\n*Welcome to my shop!\n==Rednecks==\n===Bone County (3D Universe)===\n*Howdy!\n"
        records, _ = extract_records(source)
        by_identity = {record["speaker_identity"]: record for record in records}
        clerk, redneck = by_identity["Clerks"], by_identity["Rednecks"]
        self.assertEqual((clerk["speaker_identity"], clerk["speaker_kind"]), ("Clerks", "voice_archetype"))
        self.assertEqual(clerk["source_context"], "Clothing shops > Female")
        self.assertEqual((redneck["speaker_identity"], redneck["speaker_kind"]), ("Rednecks", "voice_archetype"))
        self.assertEqual(redneck["source_context"], "Bone County (3D Universe)")


if __name__ == "__main__":
    unittest.main()
