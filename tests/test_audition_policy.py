import unittest

from studio.ui.audition import wer_warning, collective_wer_candidates


class AuditionPolicyTests(unittest.TestCase):
    def test_wer_is_advisory_and_optional(self):
        self.assertIn('80', wer_warning({'metadata': {'wer': .8}}))
        self.assertEqual(wer_warning({'metadata': {'wer': .1}}), '')
        self.assertEqual(wer_warning({'metadata': {}}), '')
        self.assertEqual(wer_warning({'metadata': {'wer': 'nan'}}), '')

    def test_collective_accept_only_pending_wer_without_other_review(self):
        good = {'id': 'a', 'status': 'pending', 'metadata': {'wer': .8}}
        duration = dict(good, id='b', metadata={'wer': .8, 'duration_override_required': True})
        stale = dict(good, id='c', stale=True)
        rejected = dict(good, id='d', status='rejected')
        plain = dict(good, id='e', metadata={})
        self.assertEqual(collective_wer_candidates([good, duration, stale, rejected, plain]), [good])


if __name__ == '__main__':
    unittest.main()
