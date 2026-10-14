import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from studio.ui.audio_review import AudioReviewWindow


class ReviewControlTests(unittest.TestCase):
    def review(self, candidate=None, available=True):
        review = AudioReviewWindow.__new__(AudioReviewWindow)
        review.closed = False
        review.busy = review.generation_busy = False
        review.paths = {'take': 'take.wav'} if available else {}
        review.candidates = {'take': candidate} if candidate else {}
        review.selected = lambda: 'take'
        review.original = None
        review.tree = SimpleNamespace(selection=lambda: ['take'])
        review.action_buttons = {label: Mock() for label in ('Ouvir gerado', 'Importar WAV', 'Comparar A/B')}
        for name in ('accept_btn', 'reject_btn', 'generate_btn', 'effect_btn'):
            setattr(review, name, Mock())
        review.state_var = Mock()
        return review

    def test_missing_audio_and_legacy_cannot_be_accepted_or_effected(self):
        review = self.review(available=False)
        review._sync_review_controls()
        review.accept_btn.config.assert_called_with(state='disabled')
        review.effect_btn.config.assert_called_with(state='disabled')
        review.action_buttons['Ouvir gerado'].config.assert_called_with(state='disabled')

    def test_valid_take_enables_review_and_busy_disables_mutations(self):
        review = self.review({'stale': False})
        review._sync_review_controls()
        review.accept_btn.config.assert_called_with(state='normal')
        review.effect_btn.config.assert_called_with(state='normal')
        review.busy = True
        review._sync_review_controls()
        review.accept_btn.config.assert_called_with(state='disabled')
        review.generate_btn.config.assert_called_with(text='Gerar take / nova tentativa', state='disabled')
        review.effect_btn.config.assert_called_with(state='normal')  # cancellation stays reachable
        review.action_buttons['Importar WAV'].config.assert_called_with(state='disabled')

    def test_closed_window_ignores_status_callbacks(self):
        review = self.review()
        review.closed = True
        review._set_state('Gerando áudio…')
        review.state_var.set.assert_not_called()

    def test_worker_stage_updates_only_active_generation(self):
        from unittest.mock import patch
        review = self.review()
        review.generation_busy = True
        review.generation_run = 'run'
        review.app = SimpleNamespace(ui=lambda callback: callback())
        with patch('studio.core.utils.escrever_log'):
            review._generation_log('{"event":"stage","stage":"loading"}', 'run')
            review.state_var.set.assert_called_with('Carregando modelo…')
            review.generation_busy = False
            review.state_var.reset_mock()
            review._generation_log('{"event":"stage","stage":"saving"}', 'run')
            review.state_var.set.assert_not_called()

    def test_old_analysis_callback_is_ignored(self):
        review = self.review()
        review.analysis_token = 2
        review.analyses = {}
        review.show_analysis = Mock()
        review._analyses_ready({'old': {}}, token=1)
        self.assertEqual(review.analyses, {})
        review.show_analysis.assert_not_called()
