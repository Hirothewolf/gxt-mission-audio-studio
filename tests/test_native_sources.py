import unittest
import queue
from types import SimpleNamespace
from unittest.mock import patch

from studio.ui.native_sources import NativeSourcesWindow, filter_classifications, index_report


class FakeWindow:
    def __init__(self):
        self.callbacks = []

    def after(self, delay, callback):
        self.callbacks.append((delay, callback))
        return len(self.callbacks)


class FakeStatus:
    def __init__(self):
        self.value = ''

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self, selection=()):
        self.selected = selection
        self.deleted = []

    def selection(self):
        return self.selected

    def delete(self, *items):
        self.deleted.extend(items)

    def get_children(self):
        return ()


def fake_window_state():
    window = NativeSourcesWindow.__new__(NativeSourcesWindow)
    window.closed = False
    window.ui_queue = queue.Queue()
    window.win = FakeWindow()
    window.status = FakeStatus()
    window._load_generation = 1
    window._filter_generation = 1
    window._listen_generation = 1
    window._search_after = None
    window._render_after = None
    window._job_poll_after = None
    window._ui_poll_after = None
    window._render_generation = 1
    window.report = None
    window.rows = {}
    window.rows_by_id = {}
    window.records_by_id = {}
    window.merged_records_by_id = {}
    window.tree = FakeTree()
    window._context_matches = lambda: True
    return window


class NativeSourcesUiDataTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            'classifications': [
                {'gxt_stable_id': 'MAIN:00000001', 'table': 'MAIN', 'names': ['WELCOME'],
                 'category': 'unknown', 'source_text': 'Welcome back', 'uses': []},
                {'gxt_stable_id': 'MAIN:00000002', 'table': 'MAIN', 'names': ['MISSION'],
                 'category': 'script', 'source_text': 'Start mission', 'uses': []},
                {'gxt_stable_id': 'CUTSCENE:00000003', 'table': 'CUTSCENE', 'names': ['INTRO'],
                 'category': 'cutscene', 'source_text': 'Opening', 'uses': []},
            ],
            'records': [
                {'gxt_stable_id': 'CUTSCENE:00000003', 'pak': 'CUTSCENE', 'scene': 'INTRO',
                 'caption_start_ms': 100, 'caption_duration_ms': 300},
            ],
        }

    def test_search_matches_names_table_and_caption_case_insensitively(self):
        found = filter_classifications(self.report, None, '  welcome BACK ')
        self.assertEqual([row['gxt_stable_id'] for row in found], ['MAIN:00000001'])
        self.assertEqual(filter_classifications(self.report, None, 'cutscene'), [self.report['classifications'][2]])

    def test_category_filter_and_all_categories(self):
        self.assertEqual(len(filter_classifications(self.report, 'script', '')), 1)
        self.assertEqual(len(filter_classifications(self.report, None, '')), 3)

    def test_index_prepares_constant_time_detail_and_listen_lookups(self):
        rows, records, merged = index_report(self.report)
        gid = 'CUTSCENE:00000003'
        self.assertIs(rows[gid], self.report['classifications'][2])
        self.assertIs(records[gid][0], self.report['records'][0])
        self.assertEqual(merged[gid]['scene'], 'INTRO')
        self.assertFalse(merged[gid]['cutscene_ambiguous'])


class NativeSourcesAsyncUiTests(unittest.TestCase):
    def test_stale_filter_result_is_discarded(self):
        window = fake_window_state()
        window._filter_generation = 2
        window.ui_queue.put(('filter', 1, [{'gxt_stable_id': 'A:1'}], 0.1, None))

        window._drain_ui()

        self.assertEqual(window.tree.deleted, [])
        self.assertEqual(window.status.value, '')

    def test_closed_window_drops_queued_results_without_touching_tk(self):
        window = fake_window_state()
        window.closed = True
        window.ui_queue.put(('filter', 1, [], 0.0, None))

        window._drain_ui()

        self.assertEqual(window.win.callbacks, [])
        self.assertEqual(window.tree.deleted, [])

    def test_current_load_error_is_shown_without_losing_context(self):
        window = fake_window_state()
        window.ui_queue.put(('loaded', 1, None, None, 0.2, 'index unreadable'))

        window._drain_ui()

        self.assertIn('index unreadable', window.status.value)

    def test_refresh_invalidates_old_report_while_new_installation_loads(self):
        window = fake_window_state()
        window.root = __import__('pathlib').Path('project').resolve()
        window.game = None
        window.report = {'version': 1, 'classifications': ['stale']}
        window.output = queue.Queue()

        class ThreadStub:
            def __init__(self, **kwargs):
                self.target = kwargs['target']

            def start(self):
                pass

        with patch('studio.ui.native_sources.config.get_project_root', return_value=str(window.root)), \
                patch.dict('studio.ui.native_sources.config.CFG', {'game_dir': None}), \
                patch('studio.ui.native_sources.threading.Thread', ThreadStub):
            window.refresh()

        self.assertIsNone(window.report)
        self.assertIn('Preparando relatório', window.status.value)

    def test_loaded_result_for_changed_project_is_rejected(self):
        window = fake_window_state()
        window._context_matches = lambda: False
        window.ui_queue.put(('loaded', 1, {'classifications': []}, ({}, {}, {}), 0.2, None))

        window._drain_ui()

        self.assertIsNone(window.report)
        self.assertIn('mudou', window.status.value)

    def test_resolved_audio_is_started_on_ui_thread_only(self):
        window = fake_window_state()
        gid = 'CUTSCENE:00000001'
        window.tree = FakeTree((gid,))
        calling_thread = __import__('threading').get_ident()
        calls = []
        window.app = SimpleNamespace(_tocar=lambda path, title: calls.append((path, title, __import__('threading').get_ident())))
        window.ui_queue.put(('listen', 1, gid, 'caption.wav', None))

        window._drain_ui()

        self.assertEqual(calls, [('caption.wav', 'CUTSCENE original — intervalo da legenda', calling_thread)])

    def test_cancelled_index_job_preserves_previous_report(self):
        window = fake_window_state()
        report = {'version': 1, 'records': ['existing']}
        window.report = report
        window.job_kind = 'index'

        class Job:
            done = True
            cancelled = False

            def cancel(self):
                self.cancelled = True

            def wait(self, timeout):
                return SimpleNamespace(returncode=None, cancelled=True, error=None, logs=[])

        class Progress:
            cancel_event = SimpleNamespace(is_set=lambda: True)

            def fechar(self):
                pass

        window.job = Job()
        window.progress = Progress()
        window.output = queue.Queue()
        with patch('studio.ui.native_sources.messagebox.showerror'):
            window.poll()

        self.assertIs(window.report, report)
        self.assertIsNone(window.job)
        self.assertIn('cancelada', window.status.value)


if __name__ == '__main__':
    unittest.main()
