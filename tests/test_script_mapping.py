import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ScriptMappingTests(unittest.TestCase):
    def test_cross_table_crc_collision_keeps_exact_links_and_suppresses_any_fallback(self):
        import studio.config as config
        import studio.script.mapping as mapping
        from studio.ui.app import App

        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory) / 'manifests'
            manifest_dir.mkdir()
            manifest = manifest_dir / 'script_mapping_v3.jsonl'
            rows = [
                {
                    'gxt_stable_id': 'MISSION_A:1234ABCD',
                    'stable_audio_id': 'SCRIPT:B010:S001',
                    'event_name': 'MISSION_A_LINE',
                },
                {
                    'gxt_stable_id': 'MISSION_B:1234ABCD',
                    'stable_audio_id': 'SCRIPT:B011:S002',
                    'event_name': 'MISSION_B_LINE',
                },
            ]
            manifest.write_text(
                ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8'
            )

            previous_cache = mapping._SCRIPT_CACHE
            try:
                mapping._SCRIPT_CACHE = None
                with patch.object(config, 'get_project_root', return_value=Path(directory)):
                    db = mapping.carregar_script_cache()

                self.assertEqual(
                    mapping.consultar_script_info(db, 'MISSION_A', 0x1234ABCD)['stable_audio_id'],
                    'SCRIPT:B010:S001',
                )
                self.assertEqual(
                    mapping.consultar_script_info(db, 'MISSION_B', 0x1234ABCD)['stable_audio_id'],
                    'SCRIPT:B011:S002',
                )
                self.assertNotIn(('ANY', 0x1234ABCD), db)
                self.assertIsNone(mapping.consultar_script_info(db, 'OTHER', 0x1234ABCD))
                self.assertIs(
                    mapping.consultar_script_info(db, 'MISSION_B', 0xFFFFFFFF, 'MISSION_B_LINE'),
                    db[('MISSION_B', 0x1234ABCD)],
                )

                app = object.__new__(App)
                app.script_db = db
                app._speaker_info = lambda _tab, _crc, record: record
                self.assertIsNone(App.info_script(app, 'OTHER', 0x1234ABCD))
                self.assertIs(App.info_script(app, 'MISSION_A', 0x1234ABCD), db[('MISSION_A', 0x1234ABCD)])
            finally:
                mapping._SCRIPT_CACHE = previous_cache

    def test_unique_explicit_table_record_never_falls_back_to_another_table(self):
        from studio.script.mapping import build_script_index, consultar_script_info

        row = {
            'gxt_stable_id': 'MISSION_A:1234ABCD',
            'jamcrc': '22222222',
            'stable_audio_id': 'SCRIPT:B010:S001',
            'event_name': 'ONLY_A_EVENT',
        }
        db = build_script_index([row])

        self.assertIs(consultar_script_info(db, 'MISSION_A', 0x1234ABCD), row)
        self.assertIs(consultar_script_info(db, 'MISSION_A', 0x22222222), row)
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0x1234ABCD))
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0x22222222))
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0xFFFFFFFF, 'ONLY_A_EVENT'))

    def test_exact_gxt_key_wins_over_a_jamcrc_alias_from_another_record(self):
        from studio.script.mapping import build_script_index, consultar_script_info

        first = {
            'gxt_stable_id': 'MISSION_A:11111111',
            'jamcrc': '22222222',
            'stable_audio_id': 'SCRIPT:B010:S001',
        }
        second = {
            'gxt_stable_id': 'MISSION_A:22222222',
            'jamcrc': '33333333',
            'stable_audio_id': 'SCRIPT:B010:S002',
        }
        db = build_script_index([first, second])

        self.assertIs(consultar_script_info(db, 'MISSION_A', 0x22222222), second)
        self.assertIs(consultar_script_info(db, 'MISSION_A', 0x33333333), second)
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0x22222222))
        self.assertNotIn(('ANY', 0x22222222), db)

    def test_legacy_table_fields_guard_unique_crc_and_event_fallbacks(self):
        from studio.script.mapping import build_script_index, consultar_script_info

        row = {
            'table': 'MISSION_A',
            'jamcrc': '22222222',
            'event_name': 'ONLY_A_EVENT',
            'stable_audio_id': 'SCRIPT:B010:S001',
        }
        db = build_script_index([row])

        self.assertIs(consultar_script_info(db, 'MISSION_A', 0x22222222), row)
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0x22222222))
        self.assertIsNone(consultar_script_info(db, 'MISSION_B', 0xFFFFFFFF, 'ONLY_A_EVENT'))

    def test_conflicting_duplicate_exact_and_event_aliases_are_suppressed(self):
        from studio.script.mapping import build_script_index, consultar_script_info

        rows = [
            {
                'gxt_stable_id': 'MISSION_A:1234ABCD',
                'stable_audio_id': 'SCRIPT:B010:S001',
                'event_name': 'DUPLICATE_EVENT',
            },
            {
                'gxt_stable_id': 'MISSION_A:1234ABCD',
                'stable_audio_id': 'SCRIPT:B011:S002',
                'event_name': 'OTHER_EVENT',
            },
        ]
        db = build_script_index(rows)

        self.assertNotIn(('MISSION_A', 0x1234ABCD), db)
        self.assertNotIn(('ANY', 0x1234ABCD), db)
        self.assertIsNone(consultar_script_info(db, 'MISSION_A', 0x1234ABCD))
        self.assertIsNone(consultar_script_info(db, 'MISSION_A', 0x1234ABCD, 'DUPLICATE_EVENT'))

        event_rows = [
            {'gxt_stable_id': 'MISSION_A:11111111', 'event_name': 'SHARED_EVENT'},
            {'gxt_stable_id': 'MISSION_A:22222222', 'event_name': 'SHARED_EVENT'},
        ]
        event_db = build_script_index(event_rows)
        self.assertNotIn(('EVENT', 'SHARED_EVENT'), event_db)
        self.assertIsNone(consultar_script_info(event_db, 'MISSION_A', 0xFFFFFFFF, 'SHARED_EVENT'))


if __name__ == '__main__':
    unittest.main()
