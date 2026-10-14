import unittest
from types import SimpleNamespace

from studio.ui.audio_review import AudioReviewWindow
from studio.gxt.codec import encode_text


class ReviewContextTests(unittest.TestCase):
    def review(self):
        review = AudioReviewWindow.__new__(AudioReviewWindow)
        review.tab, review.h = 'MAIN', 123
        review.app = SimpleNamespace(g=SimpleNamespace(t={'MAIN': {'cur': {123: encode_text('Texto B')[0]}}}),
                                     sel=None)
        review.gxt_context = review.app.g
        review.candidates = {'take-a': {'text': 'Texto A'}}
        review.line_id = 'MAIN|0000007b'
        review.store = SimpleNamespace(validate_candidate_source=lambda line, cid: review.candidates[cid])
        return review

    def test_effect_preserves_source_text_after_editor_change(self):
        review = self.review()
        self.assertEqual(review.current_text(), 'Texto B')
        self.assertEqual(review.effect_source_text('take-a'), 'Texto A')

    def test_unattributed_original_cannot_be_relabelled_as_translated_take(self):
        with self.assertRaises(ValueError):
            self.review().effect_source_text('original')

    def test_replaced_gxt_is_detected_before_reading_another_file(self):
        review = self.review()
        review.app.g = SimpleNamespace(t={})
        with self.assertRaisesRegex(ValueError, 'GXT mudou'):
            review.current_text()

    def test_effect_refuses_externally_modified_registered_take(self):
        def modified(*args):
            raise ValueError('WAV alterado')
        review = self.review()
        review.store.validate_candidate_source = modified
        with self.assertRaisesRegex(ValueError, 'alterado'):
            review.effect_source_text('take-a')


if __name__ == '__main__':
    unittest.main()
