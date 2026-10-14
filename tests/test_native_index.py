import json
from pathlib import Path
import struct
import tempfile
import unittest

from studio.script.scm import argument, decode_segment, segments
from studio.script.native_index import event_coordinates, read_cuts, load_current, merge_records


class ScmTests(unittest.TestCase):
    def test_variable_script_arguments_do_not_desynchronize_following_opcode(self):
        # START_NEW_SCRIPT label + arbitrary typed arguments + terminator, then WAIT.
        raw = bytes.fromhex('4f00012000000004020303000001000400')
        rows, error = decode_segment(raw, 0, len(raw))
        self.assertIsNone(error)
        self.assertEqual([row['name'] for row in rows], ['START_NEW_SCRIPT', 'WAIT'])
        self.assertEqual(rows[0]['params'][1]['value'], 2)
        self.assertEqual(rows[1]['params'][0]['value'], 0)

    def test_debug_string_is_untyped_fixed_length_and_keeps_next_instruction(self):
        raw = bytes.fromhex('b605') + b'debug'.ljust(128, b'\0') + bytes.fromhex('01000400')
        rows, error = decode_segment(raw, 0, len(raw))
        self.assertIsNone(error)
        self.assertEqual(rows[0]['params'][0]['value'], 'debug')
        self.assertEqual(rows[1]['name'], 'WAIT')

    def test_unknown_opcode_stops_instead_of_scanning_for_plausible_bytes(self):
        raw = bytes.fromhex('ffff0100040001000400')
        rows, error = decode_segment(raw, 0, len(raw))
        self.assertEqual(rows, [])
        self.assertEqual(error['offset'], 0)
        self.assertEqual(error['unparsed_bytes'], len(raw))

    def test_truncated_parameters_and_invalid_header_fail_boundedly(self):
        with self.assertRaises(ValueError):
            argument(b'\x01\x00', 0, 2)
        with self.assertRaises(ValueError):
            segments(bytes.fromhex('020001ffffffff00'))

    def test_negated_opcode_preserves_instruction_identity(self):
        rows, error = decode_segment(bytes.fromhex('d0830401'), 0, 4)
        self.assertIsNone(error)
        self.assertTrue(rows[0]['negated'])
        self.assertEqual(rows[0]['opcode'], '03D0')


class NativeIndexTests(unittest.TestCase):
    def test_event_boundaries_keep_global_offset_out_of_archive_local_bank(self):
        a, b = event_coordinates(2199), event_coordinates(2200)
        self.assertEqual((a['bank_num'], a['sound_num']), (1, 200))
        self.assertEqual((b['bank_num'], b['sound_num'], b['global_bank']), (2, 1, 148))
        self.assertIsNone(event_coordinates(1800))
        self.assertIsNone(event_coordinates(45401))
        self.assertIsNone(event_coordinates(True))

    def test_img_caption_key_and_timing_are_extracted_without_speaker_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cuts.img'
            body = b'info\nend\ntext\n100,200,PRO@29\nend\nmodel\n1,cj,cj\nend\0'
            header = struct.pack('<4sI', b'VER2', 1)
            entry = struct.pack('<IHH24s', 1, 1, 0, b'prolog1.cut')
            path.write_bytes((header + entry).ljust(2048, b'\0') + body.ljust(2048, b'\0'))
            cuts = read_cuts(path)
            self.assertEqual(cuts['PROLOG1']['captions'][0]['key'], 'PRO@29')
            self.assertEqual(cuts['PROLOG1']['captions'][0]['start_ms'], 100)
            self.assertNotIn('speaker_identity', cuts['PROLOG1'])
            path.write_bytes(header + struct.pack('<IHH24s', 400, 1, 0, b'prolog1.cut'))
            with self.assertRaises(ValueError):
                read_cuts(path)

    def test_merge_preserves_production_coordinate_and_verified_identity(self):
        old = {'gxt_stable_id': 'A:00000001', 'bank_num': 1, 'sound_num': 1,
               'stable_audio_id': 'SCRIPT:B000:S000', 'speaker_identity': 'CJ', 'speaker_identity_status': 'VERIFIED'}
        new = {'gxt_stable_id': 'A:00000001', 'pak': 'SCRIPT', 'stable_audio_id': 'SCRIPT:B002:S003'}
        result = merge_records([old], {'records': [new]})[0]
        self.assertEqual(result['stable_audio_id'], old['stable_audio_id'])
        self.assertEqual(result['speaker_identity'], 'CJ')
        self.assertEqual(result['native_evidence'][0]['stable_audio_id'], new['stable_audio_id'])

    def test_multiple_cutscene_windows_remain_ambiguous(self):
        rows = [{'pak': 'CUTSCENE', 'gxt_stable_id': 'A:00000001', 'scene': scene,
                 'caption_start_ms': 100, 'caption_duration_ms': 200} for scene in ('ONE', 'TWO')]
        merged = merge_records([], {'records': rows})[0]
        self.assertTrue(merged['cutscene_ambiguous'])
        self.assertEqual(len(merged['cutscene_options']), 2)

    def test_changed_game_sources_or_selected_installation_invalidate_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / 'game'
            game.mkdir()
            source = game / 'main.scm'
            source.write_bytes(b'original')
            stat = source.stat()
            dest = root / 'workspace/native_audio/index.json'
            dest.parent.mkdir(parents=True)
            report = {'version': 1, 'sources': {'scm': {'path': str(source), 'signature': [stat.st_size, stat.st_mtime_ns]}}, 'records': []}
            dest.write_text(json.dumps(report))
            self.assertIsNotNone(load_current(root, game))
            self.assertIsNone(load_current(root, root / 'other_game'))
            source.write_bytes(b'changed game source')
            self.assertIsNone(load_current(root, game))


if __name__ == '__main__':
    unittest.main()
