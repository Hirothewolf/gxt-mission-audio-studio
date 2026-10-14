import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


def make_wav(path):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        w.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i / 8))) for i in range(800)))
    return path


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wav = make_wav(self.root / 'original.wav')

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        from studio.projects.review import ReviewStore
        return ReviewStore(self.root)

    def test_candidates_preserve_source_and_previous_take(self):
        store = self.store()
        before = self.wav.read_bytes()
        a = store.import_candidate('RYDER1|12345678', self.wav, 'texto', 'import')
        b = store.import_candidate('RYDER1|12345678', self.wav, 'texto', 'effect')
        self.assertNotEqual(a['id'], b['id'])
        self.assertNotEqual(a['path'], b['path'])
        self.assertEqual(before, self.wav.read_bytes())
        self.assertEqual(len(self.store().list_candidates('RYDER1|12345678', 'texto')), 2)

    def test_effect_source_validation_detects_modified_valid_wav(self):
        store = self.store()
        candidate = store.import_candidate('L', self.wav, 'A', 'import')
        self.assertEqual(store.validate_candidate_source('L', candidate['id'])['text'], 'A')
        path = store.resolve_path(candidate['path'])
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        with self.assertRaisesRegex(ValueError, 'alterado'):
            store.validate_candidate_source('L', candidate['id'])

    def test_failed_approval_save_does_not_approve_in_memory(self):
        store = self.store()
        candidate = store.import_candidate('L', self.wav, 'A', 'import')
        with patch.object(store, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.set_status('L', candidate['id'], 'approved', 'A')
        self.assertIsNone(store.approved_path('L', 'A'))
        self.assertEqual(store.list_candidates('L', 'A')[0]['status'], 'pending')
        self.assertEqual(self.store().list_candidates('L', 'A')[0]['status'], 'pending')

    def test_approval_rejection_and_stale_are_separate(self):
        store = self.store()
        a = store.import_candidate('L', self.wav, 'A', 'import')
        store.set_status('L', a['id'], 'approved', 'A')
        self.assertTrue(store.list_candidates('L', 'B')[0]['stale'])
        self.assertIsNone(store.approved_path('L', 'B'))
        with self.assertRaises(ValueError):
            store.set_status('L', a['id'], 'approved', 'B')
        b = store.import_candidate('L', self.wav, 'A', 'import')
        store.set_status('L', b['id'], 'rejected', 'A')
        self.assertEqual(store.approved_path('L', 'A'), store.resolve_path(a['path']))
        store.set_status('L', a['id'], 'rejected', 'A')
        self.assertIsNone(store.approved_path('L', 'A'))

    def test_modified_candidate_cannot_be_approved(self):
        store = self.store()
        a = store.import_candidate('L', self.wav, 'A', 'import')
        store.resolve_path(a['path']).write_bytes(b'not WAV')
        with self.assertRaises(ValueError):
            store.set_status('L', a['id'], 'approved', 'A')

    def test_reference_keeps_transcript_provenance_and_manual_identity(self):
        store = self.store()
        ref = store.add_reference('Ryder', self.wav, 'original English', {'stable_audio_id': 'SCRIPT:B000:S000'})
        store.assign_character('L', 'Ryder')
        reopened = self.store()
        self.assertEqual(reopened.character('L'), 'Ryder')
        self.assertEqual(reopened.references('Ryder')[0]['transcript'], 'original English')
        self.assertEqual(ref['provenance']['stable_audio_id'], 'SCRIPT:B000:S000')
        self.assertTrue(reopened.resolve_path(ref['path']).is_file())

    def test_failed_reference_save_rolls_back_record_and_removes_only_new_copy(self):
        store = self.store()
        existing = store.add_reference('Ryder', self.wav, 'existing transcript')
        existing_path = store.resolve_path(existing['path'])

        with patch.object(store, '_save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                store.add_reference('Ryder', self.wav, 'new transcript')

        self.assertEqual([r['id'] for r in store.references('Ryder')], [existing['id']])
        self.assertEqual(len(list((store.directory / 'references').glob('*.wav'))), 1)
        self.assertTrue(existing_path.is_file())
        self.assertEqual([r['id'] for r in self.store().references('Ryder')], [existing['id']])

    def test_failed_first_reference_save_does_not_leave_empty_voice_profile(self):
        store = self.store()

        with patch.object(store, '_save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                store.add_reference('Ryder', self.wav, 'new transcript')

        self.assertEqual(store.characters(), [])
        self.assertEqual(store.references('Ryder'), [])
        self.assertEqual(list((store.directory / 'references').glob('*.wav')), [])

    def test_failed_reference_removal_restores_record_and_preserves_file(self):
        store = self.store()
        reference = store.add_reference('Ryder', self.wav, 'original English')
        saved = self.store()
        reference_path = saved.resolve_path(reference['path'])

        with patch.object(saved, '_save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                saved.remove_reference('Ryder', reference['id'])

        self.assertEqual([r['id'] for r in saved.references('Ryder')], [reference['id']])
        self.assertEqual([r['id'] for r in self.store().references('Ryder')], [reference['id']])
        self.assertTrue(reference_path.is_file())

    def test_corrupt_store_is_not_silently_reset(self):
        p = self.root / 'workspace' / 'studio_review' / 'review.json'
        p.parent.mkdir(parents=True)
        p.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store()
        self.assertEqual(p.read_text(encoding='utf-8'), '{broken')

    def test_invalid_input_is_not_imported(self):
        bad = self.root / 'bad.wav'
        bad.write_bytes(b'RIFF broken')
        store = self.store()
        with self.assertRaises(ValueError):
            store.import_candidate('L', bad, 'A', 'import')
        self.assertEqual(store.list_candidates('L', 'A'), [])

    def test_nonfinite_float_wav_is_not_imported(self):
        raw = struct.pack('<f', float('nan'))
        data = b'fmt ' + struct.pack('<IHHIIHH', 16, 3, 1, 8000, 32000, 4, 32)
        data += b'data' + struct.pack('<I', len(raw)) + raw
        bad = self.root / 'float.wav'
        bad.write_bytes(b'RIFF' + struct.pack('<I', len(data) + 4) + b'WAVE' + data)
        with self.assertRaises(ValueError):
            self.store().import_candidate('L', bad, 'A', 'import')

    def test_store_rejects_nested_corruption_and_escaping_paths(self):
        store = self.store()
        a = store.import_candidate('L', self.wav, 'A', 'import')
        store.data['lines']['L']['candidates'][0]['path'] = '../../external.wav'
        store._save()
        with self.assertRaises(ValueError):
            self.store()
        with self.assertRaises(ValueError):
            store.resolve_path('../../external.wav')

    def test_pending_candidate_cannot_be_played_as_approved(self):
        store = self.store()
        a = store.import_candidate('L', self.wav, 'A', 'import')
        store.data['lines']['L']['approved'] = a['id']
        self.assertIsNone(store.approved_path('L', 'A'))


if __name__ == '__main__':
    unittest.main()
