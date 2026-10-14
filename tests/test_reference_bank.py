import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from studio.audio.reference_bank import load_reference_bank, reference_for_actor


def write_hashed(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class NativeReferenceBankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.bank_path = self.project / 'workspace' / 'studio_review' / 'native_reference_bank.json'
        self.reference = self.project / 'workspace' / 'studio_review' / 'native_references' / 'Officer_A.wav'
        self.native = self.project / 'workspace' / 'native' / 'Track.wav'
        self.review = self.project / 'workspace' / 'review.json'
        self.reference_hash = write_hashed(self.reference, b'original-reference-wave')
        self.native_hash = write_hashed(self.native, b'native-audio')
        self.review_hash = write_hashed(self.review, b'review-evidence')
        self.bank_path.parent.mkdir(parents=True, exist_ok=True)
        self.payload = {
            'schema': 1,
            'source_policy': 'ROOT_VANILLA_GAME_ONLY_NO_SYNTHETIC_REFERENCES',
            'actors': [{
                'actor_id': 'Officer A',
                'reference_path': str(self.reference),
                'reference_sha256': self.reference_hash,
                'reference_text': 'Reviewed native words.',
                'reference_kind': 'NATIVE_GAME_CROP',
                'source_native_path': str(self.native),
                'source_native_sha256': self.native_hash,
                'voice_crop_review': {
                    'suitable': True, 'single_speaker': True,
                    'starts_clipped': False, 'ends_clipped': False,
                    'review_path': str(self.review), 'review_sha256': self.review_hash,
                },
                'identity_status': 'MODEL_AND_SCRIPT_CONTEXT_PROPOSAL_NOT_BIOMETRIC_CONFIRMATION',
            }],
        }
        self.save_bank()

    def tearDown(self):
        self.temp.cleanup()

    def save_bank(self):
        self.bank_path.write_text(json.dumps(self.payload), encoding='utf-8')

    def test_explicit_actor_selection_returns_hash_pinned_reference_with_provenance(self):
        actors = load_reference_bank(self.project)
        self.assertEqual([item['actor_id'] for item in actors], ['Officer A'])

        reference = reference_for_actor(self.project, 'Officer A')

        self.assertEqual(reference['actor_id'], 'Officer A')
        self.assertEqual(reference['sha256'], self.reference_hash)
        self.assertEqual(reference['path'], str(self.reference.resolve()))
        self.assertEqual(reference['reference_kind'], 'NATIVE_GAME_CROP')
        self.assertEqual(reference['reference_transcript'], 'Reviewed native words.')
        self.assertEqual(reference['provenance']['source_native_sha256'], self.native_hash)
        self.assertEqual(reference['provenance']['review_sha256'], self.review_hash)
        self.assertIn('NOT_BIOMETRIC', reference['provenance']['identity_status'])

    def test_unknown_actor_is_never_inferred_or_selected_implicitly(self):
        with self.assertRaisesRegex(ValueError, 'explícita'):
            reference_for_actor(self.project, 'Unknown')

    def test_changed_reference_crop_is_rejected(self):
        self.reference.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '(?i)hash'):
            reference_for_actor(self.project, 'Officer A')

    def test_unsuitable_or_multispeaker_record_is_not_offered(self):
        self.payload['actors'][0]['voice_crop_review']['single_speaker'] = False
        self.save_bank()
        with self.assertRaisesRegex(ValueError, 'revisão'):
            load_reference_bank(self.project)

    def test_bank_with_unrecognized_source_policy_is_rejected(self):
        self.payload['source_policy'] = 'SYNTHETIC_OR_UNVERIFIED'
        self.save_bank()
        with self.assertRaisesRegex(ValueError, 'origem'):
            load_reference_bank(self.project)


if __name__ == '__main__':
    unittest.main()
