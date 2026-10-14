import unittest
from types import SimpleNamespace
from unittest.mock import patch

from studio.ui.audio_review import AudioReviewWindow
from studio.ui.app import App
from studio.config import CFG


class ResidentModelUiTests(unittest.TestCase):
    def test_review_shortcut_selects_generation_tab(self):
        review = AudioReviewWindow.__new__(AudioReviewWindow)
        calls = []
        review.notebook = SimpleNamespace(select=lambda tab: calls.append(tab))
        review.generation_tab = 'generation-tab'

        review.open_generation_tab()

        self.assertEqual(calls, ['generation-tab'])

    def test_keep_model_toggle_persists_and_updates_job_queue(self):
        previous = CFG.get('omnivoice_keep_model_loaded')
        app = App.__new__(App)
        app.var_omnivoice_keep_model_loaded = SimpleNamespace(get=lambda: True)
        app.jobs = SimpleNamespace(set_keep_model_loaded=lambda value: calls.append(value))
        calls = []
        try:
            with patch('studio.ui.app.cfg_save') as save:
                app.set_omnivoice_keep_model_loaded()

            self.assertTrue(CFG['omnivoice_keep_model_loaded'])
            self.assertEqual(calls, [True])
            save.assert_called_once_with()
        finally:
            if previous is None:
                CFG.pop('omnivoice_keep_model_loaded', None)
            else:
                CFG['omnivoice_keep_model_loaded'] = previous


if __name__ == '__main__':
    unittest.main()
