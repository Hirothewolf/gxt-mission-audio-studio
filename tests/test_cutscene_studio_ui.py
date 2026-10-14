from pathlib import Path
from types import SimpleNamespace
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from studio.ui.cutscene_studio import CutsceneStudioWindow

class CutsceneStudioStateTests(unittest.TestCase):
    def window(self):
        window = CutsceneStudioWindow.__new__(CutsceneStudioWindow)
        window.segments = [{'id':'first','start':0,'end':3}, {'id':'second','start':4,'end':7}]
        window.edit_revision = 0
        window.selected_segment = 1
        window.job_target_segment = 'first'
        for name in ('status','tts_status','fit_info'):
            setattr(window,name,SimpleNamespace(set=Mock()))
        window._render_segments = Mock()
        window.reference_text = SimpleNamespace(get=Mock(return_value='Original words'))
        return window

    def test_generated_take_attaches_to_captured_identity_even_if_selection_changes(self):
        window = self.window()
        take = {'path':'new.wav','duration':2}
        window._job_done('tts_import', take)
        self.assertIs(window.segments[0]['replacement'], take)
        self.assertNotIn('replacement', window.segments[1])

    def test_overlap_choice_reaches_render_and_allows_updating_simultaneous_segment(self):
        window = self.window()
        window.win = None
        window.job = window.tts_job = None
        window.track_data = {'sample_rate': 10, 'frames': 100}
        window._current_bounds = Mock(return_value=(2, 5))
        window._draw_waveform = Mock()
        window.overlap_var = SimpleNamespace(get=Mock(return_value=False))
        with patch('studio.ui.cutscene_studio.messagebox.showerror') as error:
            window._update_segment()
            error.assert_called_once()
        self.assertEqual(window.segments[1]['start'], 4)
        window.overlap_var.get.return_value = True
        window._update_segment()
        self.assertEqual((window.segments[1]['start'], window.segments[1]['end']), (2, 5))
        window.project = Path(tempfile.gettempdir())
        window.track = {'track': 'Track_001'}
        window.fit_var = SimpleNamespace(get=lambda: False)
        for segment in window.segments:
            segment['replacement'] = {'path': 'voice.wav'}
        window._submit = Mock()
        window._render_mix()
        self.assertTrue(window._submit.call_args.kwargs['allow_overlap'])
        self.assertEqual(window._submit.call_args.kwargs['background_mode'], 'voice_only')

    def test_voice_only_render_reports_missing_take_without_using_original(self):
        window = self.window()
        window.track_data = {'sample_rate': 10, 'frames': 100}
        window.track = {'track': 'Track_001'}
        window.project = Path(tempfile.gettempdir())
        window.fit_var = SimpleNamespace(get=lambda: False)
        window._submit = Mock()
        window._render_mix()
        self.assertEqual(window._submit.call_args.args[3:5], ([], []))
        self.assertEqual(len(window._submit.call_args.kwargs['missing_ptbr']), 2)
        self.assertEqual(window._submit.call_args.kwargs['background_mode'], 'voice_only')

    def test_omnivoice_original_duration_and_decimal_speed_reach_native_request(self):
        from studio.audio.cutscene_studio import digest
        with tempfile.TemporaryDirectory() as temporary:
            window = self.window()
            window.project = Path(temporary)
            source = window.project / '.cache/cutscene_original/native/Track_001.ogg'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'fixture source')
            gxt = window.project / 'american.gxt'
            gxt.write_bytes(b'fixture gxt')
            archive = str(window.project / 'CUTSCENE')
            window.job = window.tts_job = None
            window.selected_segment = 0
            window.segments = [{'id': 'line', 'gxt_row_id': 'line', 'start': 1., 'end': 3.47}]
            window.gxt_rows = {'line': {'native_source_text': 'Original words', 'native_gxt_path': str(gxt),
                                       'native_gxt_sha256': digest(gxt), 'gid': 'INTRO1:12345678'}}
            window.gxt_tree = SimpleNamespace(selection=lambda: ('line',))
            window.track_data = {'source_path': str(source), 'source_sha256': digest(source),
                                 'archive_path': archive, 'archive_sha256': 'archive',
                                 'sample_rate': 100, 'frames': 1000}
            window.report = {'sources': {'stream': {'path': archive, 'sha256': 'archive'}}}
            window.target_text = SimpleNamespace(get=lambda *args: 'Fala traduzida')
            window._current_bounds = lambda: (1., 3.47)
            window.omni_speed_var = SimpleNamespace(get=lambda: '1,0')
            window.omni_duration_var = SimpleNamespace(get=lambda: 'original')
            window.progress = SimpleNamespace(start=Mock())
            window.app = SimpleNamespace(jobs=SimpleNamespace(submit=Mock()))
            with patch('studio.ui.cutscene_studio.audio.export_reference', return_value={'sha256': 'reference', 'duration': 2.47}), \
                    patch('studio.tts.omnivoice.build_command', return_value=['mock-command']) as command:
                window._generate_omnivoice()
            command.assert_called_once()
            self.assertEqual(command.call_args.kwargs['duration'], 'original')
            self.assertEqual(command.call_args.kwargs['speed'], 1.0)
            self.assertAlmostEqual(command.call_args.kwargs['window_seconds'], 2.47)

    def test_result_for_removed_segment_never_attaches_to_other_segment(self):
        window = self.window()
        window.job_target_segment = 'removed'
        window._job_done('tts_import', {'path':'new.wav','duration':2})
        self.assertTrue(all('replacement' not in row for row in window.segments))

    def test_reference_can_be_chosen_before_adding_a_replacement_segment(self):
        window = self.window()
        window.segments = []
        window.job_target_segment = None
        reference = {'path':'reference.wav','duration':2}
        window._job_done('reference',reference)
        self.assertIs(window.current_reference, reference)
        self.assertEqual(window.current_reference_transcript,'Original words')

    def test_exported_reference_before_add_is_scoped_to_captured_caption(self):
        window = self.window()
        window.segments = []
        window.job_target_segment = None
        window.reference_target_row_id = 'caption-a'
        window.gxt_tree = SimpleNamespace(selection=lambda: ('caption-a',))
        window._job_done('reference', {'path': 'reference.wav', 'sha256': 'abc', 'duration': 2})
        self.assertEqual(window.current_reference_row_id, 'caption-a')
        self.assertTrue(window.reference_pending)

    def test_late_reference_export_does_not_replace_current_caption_reference(self):
        window = self.window()
        window.segments = []
        window.job_target_segment = None
        window.reference_target_row_id = 'caption-a'
        window.gxt_tree = SimpleNamespace(selection=lambda: ('caption-b',))
        window.current_reference = None
        window.reference_pending = False
        window._job_done('reference', {'path': 'reference-a.wav', 'sha256': 'abc', 'duration': 2})
        self.assertIsNone(window.current_reference)
        self.assertFalse(window.reference_pending)

    def test_add_segment_does_not_mutate_during_tts(self):
        window = self.window()
        window.job = None
        window.tts_job = object()
        window._add_segment()
        self.assertEqual(len(window.segments),2)
        self.assertEqual(window.selected_segment,1)

    def test_selecting_different_caption_clears_unbound_reference(self):
        window = self.window()
        window.gxt_tree = SimpleNamespace(selection=lambda: ('row-b',))
        window.gxt_rows = {'row-b': {'original': 'New caption', 'hypertranslation': 'Nova fala'}}
        window.current_reference = {'path': 'old.wav', 'sha256': 'old'}
        window.current_reference_row_id = 'row-a'
        window.reference_text = SimpleNamespace(delete=Mock(), insert=Mock())
        window.target_text = SimpleNamespace(delete=Mock(), insert=Mock())

        window._gxt_selected()

        self.assertIsNone(window.current_reference)
        self.assertEqual(window.current_reference_row_id, None)
        window.reference_text.insert.assert_called_once_with('1.0', 'New caption')
        window.target_text.insert.assert_called_once_with('1.0', 'Nova fala')

    def test_selecting_same_caption_keeps_explicitly_bound_reference(self):
        window = self.window()
        window.gxt_tree = SimpleNamespace(selection=lambda: ('row-a',))
        window.gxt_rows = {'row-a': {'original': 'Caption A', 'hypertranslation': 'Fala A'}}
        reference = {'path': 'a.wav', 'sha256': 'pinned'}
        window.current_reference = reference
        window.current_reference_row_id = 'row-a'
        window.reference_text = SimpleNamespace(delete=Mock(), insert=Mock())
        window.target_text = SimpleNamespace(delete=Mock(), insert=Mock())

        window._gxt_selected()

        self.assertIs(window.current_reference, reference)

    def test_add_segment_does_not_carry_reference_to_another_caption(self):
        window = self.window()
        window.segments = []
        window.job = window.tts_job = None
        window.track_data = {'sample_rate': 10, 'frames': 100}
        window._current_bounds = Mock(return_value=(1, 2))
        window.gxt_tree = SimpleNamespace(selection=lambda: ('row-b',))
        window.gxt_rows = {'row-b': {'gid': 'CUTSCENE:22222222', 'table': 'CUTSCENE',
            'hash': '22222222', 'scene': 'SCENE_B', 'key': '22222222', 'original': 'Caption B',
            'comparison': '', 'hypertranslation': 'Fala B', 'gxt_path': '', 'original_gxt_path': ''}}
        window.current_reference = {'path': 'row-a.wav', 'sha256': 'a', 'actor_id': 'Officer A'}
        window.current_reference_transcript = 'Caption A'
        window.current_reference_row_id = 'row-a'
        window.reference_pending = False
        window.current_actor_id = 'Officer A'
        window.target_text = SimpleNamespace(get=Mock(return_value='Fala B'), delete=Mock(), insert=Mock())
        window.reference_text = SimpleNamespace(get=Mock(return_value='Caption B'),
                                                 delete=Mock(), insert=Mock())
        window.segment_tree = SimpleNamespace(delete=Mock(), insert=Mock(), selection_set=Mock(),
            selection=lambda: (), selection_remove=Mock())
        window._mark_dirty = Mock()
        window._draw_waveform = Mock()
        window.win = None

        window._gxt_selected()
        window._add_segment()

        self.assertIsNone(window.segments[0]['reference'])
        self.assertIsNone(window.segments[0]['actor_id'])
        self.assertEqual(window.segments[0]['gxt_row_id'], 'row-b')

    def test_applying_reference_to_new_caption_preserves_old_segments_reference(self):
        window = self.window()
        window.selected_segment = 0
        old_reference = {'path': 'row-a.wav', 'sha256': 'old', 'actor_id': 'Officer A'}
        window.segments[0].update({'gxt_row_id': 'row-a', 'actor_id': 'Officer A',
            'reference': old_reference, 'reference_transcript': 'A transcript',
            'gxt': {'stable_id': 'CUTSCENE:11111111'}})
        window.current_reference = old_reference
        window.current_reference_row_id = 'row-a'
        window.reference_pending = False
        window.current_actor_id = 'Officer A'
        window.gxt_tree = SimpleNamespace(selection=lambda: ('row-b',))
        window.gxt_rows = {'row-b': {'gid': 'CUTSCENE:22222222', 'table': 'CUTSCENE',
            'hash': '22222222', 'scene': 'SCENE_B', 'key': '22222222', 'original': 'Caption B',
            'comparison': '', 'hypertranslation': 'Fala B', 'gxt_path': '', 'original_gxt_path': ''}}
        window.reference_text = SimpleNamespace(delete=Mock(), insert=Mock())
        window.target_text = SimpleNamespace(delete=Mock(), insert=Mock())
        window.reference_actor_var = SimpleNamespace(get=lambda: 'Officer C', set=Mock())
        window.segment_tree = SimpleNamespace(selection=lambda: ('0',), selection_remove=Mock())
        window.tts_status = SimpleNamespace(set=Mock())
        window.job = window.tts_job = None
        window._mark_dirty = Mock()
        window._render_segments = Mock()
        window.project = Path('.')
        new_reference = {'path': 'row-c.wav', 'sha256': 'new', 'actor_id': 'Officer C',
                         'reference_transcript': 'C transcript'}

        with patch('studio.audio.reference_bank.reference_for_actor', return_value=new_reference):
            window._gxt_selected()
            window._use_bank_reference()

        self.assertIs(window.segments[0]['reference'], old_reference)
        self.assertEqual(window.segments[0]['actor_id'], 'Officer A')
        self.assertEqual(window.segments[0]['gxt']['stable_id'], 'CUTSCENE:11111111')
        self.assertIsNone(window.selected_segment)
        window.segment_tree.selection_remove.assert_called_once_with('0')
        self.assertIs(window.current_reference, new_reference)
        self.assertEqual(window.current_reference_row_id, 'row-b')
        self.assertTrue(window.reference_pending)

    def test_generation_passes_hash_pin_and_stale_reference_is_rejected_before_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.window()
            window.project = Path(directory)
            reference = window.project/'reference.wav'
            reference.write_bytes(b'fixture')
            import hashlib
            pinned = hashlib.sha256(b'fixture').hexdigest()
            window.segments[1]['reference'] = {'path':str(reference),'sha256':pinned}
            window.segments[1]['actor_id'] = 'Officer A'
            window.segments[1]['reference']['actor_id'] = 'Officer A'
            window.models = [{'label':'1.7B','path':'installed-model'}]
            window.model_var = SimpleNamespace(get=lambda:'1.7B')
            window.target_text = SimpleNamespace(get=lambda *a:'Texto hipertraduzido')
            window.reference_text = SimpleNamespace(get=lambda *a:'Original words')
            window.job = window.tts_job = None
            window.progress = SimpleNamespace(start=Mock())
            window.messages = queue.Queue()
            jobs = SimpleNamespace(submit=Mock(return_value=object()))
            window.app = SimpleNamespace(jobs=jobs)
            with patch('studio.tts.cutscene.python_path', return_value=Path('python.exe')), patch('studio.tts.cutscene.build_command', return_value=['python.exe','task.py']) as build:
                window._generate_tts()
            self.assertEqual(build.call_args.kwargs['reference_sha256'], pinned)
            self.assertEqual(jobs.submit.call_args.args[0], ['python.exe','task.py'])

            window.segments[1]['actor_id'] = 'Officer B'
            jobs.submit.reset_mock()
            window.tts_job = None
            window.job_kind = None
            with patch('studio.tts.cutscene.python_path', return_value=Path('python.exe')), patch('studio.tts.cutscene.build_command') as build:
                window._generate_tts()
            build.assert_not_called()
            jobs.submit.assert_not_called()

            window.segments[1]['actor_id'] = 'Officer A'
            reference.write_bytes(b'changed')
            jobs.submit.reset_mock()
            window.tts_job = None
            window.job_kind = None
            with patch('studio.tts.cutscene.python_path', return_value=Path('python.exe')), patch('studio.tts.cutscene.build_command') as build:
                window._generate_tts()
            build.assert_not_called()
            jobs.submit.assert_not_called()

    def test_ui_generation_uses_explicit_reference_and_shared_job_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.window()
            window.project = Path(directory)
            reference = window.project/'reference.wav'
            reference.write_bytes(b'fixture')
            import hashlib
            window.segments[1]['reference'] = {'path':str(reference),'sha256':hashlib.sha256(b'fixture').hexdigest()}
            window.models = [{'label':'1.7B','path':'installed-model'}]
            window.model_var = SimpleNamespace(get=lambda:'1.7B')
            window.target_text = SimpleNamespace(get=lambda *a:'Texto hipertraduzido')
            window.reference_text = SimpleNamespace(get=lambda *a:'Original words')
            window.job = window.tts_job = None
            window.progress = SimpleNamespace(start=Mock())
            window.messages = queue.Queue()
            jobs = SimpleNamespace(submit=Mock(return_value=object()))
            window.app = SimpleNamespace(jobs=jobs)
            with patch('studio.tts.cutscene.python_path', return_value=Path('python.exe')), patch('studio.tts.cutscene.build_command', return_value=['python.exe','task.py']) as build:
                window._generate_tts()
            self.assertEqual(build.call_args.args[1:5],('1.7B',reference,'Original words','Texto hipertraduzido'))
            self.assertEqual(window.job_target_segment,'second')
            self.assertEqual(jobs.submit.call_args.args[0],['python.exe','task.py'])
            jobs.submit.call_args.kwargs['on_line']('Loading model')
            self.assertEqual(window.messages.get_nowait(),('log','Loading model'))

if __name__ == '__main__': unittest.main()
