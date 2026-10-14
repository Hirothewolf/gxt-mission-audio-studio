import unittest
import json
import threading
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from studio import config
from studio.ui.voice_identity import (
    ALL_SCRIPT_SCOPE, VoiceIdentityWindow, _score_text, _status_text,
)


class _FakeWidget:
    def __init__(self):
        self.values = {}
        self.text = None

    def configure(self, **kwargs):
        self.values.update(kwargs)

    def config(self, **kwargs):
        self.values.update(kwargs)
        self.text = kwargs.get('text', self.text)

    def stop(self):
        self.stopped = True


class _FakeProgress:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.logs = []
        self.pb = _FakeWidget()
        self.lbl_status = _FakeWidget()
        self.lbl_detalhes = _FakeWidget()
        self._indeterminate = True

    def _registrar_log(self, line):
        self.logs.append((line, threading.current_thread().name))

    def atualizar(self, *_args, **_kwargs):
        raise AssertionError('Polling must preserve the streamed progress state.')


class VoiceIdentityUiTests(unittest.TestCase):
    def test_scores_are_raw_values_and_never_formatted_as_probability(self):
        rendered = _score_text({'score': 0.91, 'margin': 0.17})
        self.assertEqual(rendered, 'score 0.9100 · margem 0.1700')
        self.assertNotIn('%', rendered)
        self.assertNotIn('probabilidade', rendered.casefold())

    def test_short_clip_and_ambiguous_results_are_spelled_out(self):
        self.assertIn('curto demais', _status_text({'status': 'UNANALYSABLE', 'reason': 'too_short'}))
        self.assertIn('ambígua', _status_text({'status': 'AMBIGUOUS'}))

    def test_scope_requires_a_known_mission_or_explicit_all_script_choice(self):
        window = VoiceIdentityWindow.__new__(VoiceIdentityWindow)
        window.scope_values = ['INTRO1', 'CASIN10', ALL_SCRIPT_SCOPE]
        window.scope_var = SimpleNamespace(get=lambda: 'INTRO1')
        self.assertEqual(window._selected_tables(), ['INTRO1'])
        window.scope_var = SimpleNamespace(get=lambda: ALL_SCRIPT_SCOPE)
        self.assertIsNone(window._selected_tables())
        window.scope_var = SimpleNamespace(get=lambda: '*')
        with self.assertRaisesRegex(ValueError, 'missão válida'):
            window._selected_tables()

    def test_group_confirmation_calls_overlay_store_with_latest_report_id(self):
        context = object()
        window = VoiceIdentityWindow.__new__(VoiceIdentityWindow)
        window.closed = False
        refresh_voice_identities = Mock()
        window.app = SimpleNamespace(g=context, refresh_voice_identities=refresh_voice_identities)
        window.gxt_context = context
        window.project_root = config.get_project_root()
        window.report = {'id': 'report-7', 'groups': []}
        window.current_group = 'voice-1'
        window.character_var = SimpleNamespace(get=lambda: 'CJ')
        window.store = SimpleNamespace(assign_group=Mock())
        window.refresh_report = Mock()
        window.group_tree = SimpleNamespace(exists=lambda _iid: True, item=Mock(), selection_set=Mock())
        window.group_title_var = SimpleNamespace(set=Mock())
        window.groups = {'voice-1': {'count': 2}}
        window.history_var = SimpleNamespace(set=Mock())

        with patch('studio.ui.voice_identity.messagebox.showerror') as showerror:
            window.confirm_group()

        window.store.assign_group.assert_called_once_with('report-7', 'voice-1', 'CJ')
        window.refresh_report.assert_called_once_with()
        window.group_tree.item.assert_called_once()
        refresh_voice_identities.assert_called_once_with()
        showerror.assert_not_called()

    def test_streamed_progress_survives_more_lines_than_job_log_capacity(self):
        window = VoiceIdentityWindow.__new__(VoiceIdentityWindow)
        window.closed = False
        window._poll_id = None
        window._job_output = queue.Queue()
        window._has_progress_event = False
        window._last_job_line = ''
        window.progress = _FakeProgress()
        window.job = SimpleNamespace(
            done=False,
            cancel=Mock(),
            wait=Mock(),
        )
        window.progress.cancel_event.clear()
        window.win = SimpleNamespace(after=Mock(return_value='next-poll'))

        lines = ['log line %d' % i for i in range(600)]
        lines.insert(25, json.dumps({'event': 'progress', 'done': 4, 'total': 8,
                                     'message': 'Analisando missão INTRO1'}))
        worker = threading.Thread(target=lambda: [window._queue_job_line(line) for line in lines])
        worker.start()
        worker.join()

        self.assertEqual(window.progress.logs, [])
        window._poll_job()

        self.assertEqual(len(window.progress.logs), 601)
        self.assertTrue(all(thread_name == threading.current_thread().name
                            for _line, thread_name in window.progress.logs))
        self.assertEqual(window.progress.lbl_status.text, 'Analisando missão INTRO1')
        self.assertEqual(window.progress.pb.values['value'], 50)
        window.win.after.assert_called_once_with(150, window._poll_job)

    def test_close_cancels_active_process_and_stops_audio(self):
        window = VoiceIdentityWindow.__new__(VoiceIdentityWindow)
        window.closed = False
        window._poll_id = None
        window.job = SimpleNamespace(cancel=Mock())
        progress = SimpleNamespace(fechar=Mock())
        window.progress = progress
        window.app = SimpleNamespace(parar_audio=Mock())
        window.win = SimpleNamespace(destroy=Mock())

        window.close()

        self.assertTrue(window.closed)
        window.job.cancel.assert_called_once_with()
        progress.fechar.assert_called_once_with()
        window.app.parar_audio.assert_called_once_with()
        window.win.destroy.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
