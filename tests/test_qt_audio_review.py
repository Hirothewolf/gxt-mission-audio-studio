"""Isolated Qt review behavior. Never accesses the user's game or synthesis model."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import math
import struct
import tempfile
import time
import unittest
import wave
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from studio.projects.review import ReviewStore
from studio.ui_qt.audio_review import AudioReviewDialog, BulkAudioReviewDialog


class Player:
    def __init__(self):
        self.events = []
    def play(self, path):
        self.events.append(('play', Path(path)))
    def compare(self, paths):
        self.events.append(('compare', list(paths)))
    def stop(self):
        self.events.append(('stop',))


class QtReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wav = self.root / 'source.wav'
        with wave.open(str(self.wav), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b''.join(struct.pack('<h', int(math.sin(i / 7) * 5000)) for i in range(2400)))
        self.store = ReviewStore(self.root)
        self.record = {'table': 'MAIN', 'hash': 42, 'text': 'Olá', 'name': 'HELLO', 'info': {}}
        self.text = 'Olá'
        self.context = SimpleNamespace(project_root=self.root, review_store=self.store,
            player=Player(), original_path=lambda record: self.wav,
            installed_path=lambda record: None, approved_path=self.store.approved_path,
            info=lambda record: {}, current_text=lambda record: self.text,
            generation_request=lambda *args, **kwargs: self.fail('No incidental generation'),
            install=lambda records: self.fail('No incidental install'))
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            for probe in getattr(dialog, 'probes', {}).values():
                probe.cancel()
                probe.wait(5000)
            generation = getattr(dialog, 'generation', None)
            if generation:
                for thread in (generation.preparation, generation.import_worker):
                    if thread:
                        thread.cancel()
                        thread.wait(5000)
            worker = getattr(dialog, 'worker', None)
            if worker and worker.isRunning():
                worker.cancel()
                worker.wait(5000)
            analysis_worker = getattr(dialog, 'analysis_worker', None)
            if analysis_worker:
                dialog.analysis_pending = None
                analysis_worker.wait(5000)
            dialog.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def wait(self, dialog):
        deadline = time.monotonic() + 5
        while dialog.busy() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(dialog.busy())

    def single(self):
        dialog = AudioReviewDialog(self.context, self.record, self.text)
        self.dialogs.append(dialog)
        return dialog

    def bulk(self, records=None):
        dialog = BulkAudioReviewDialog(self.context, records or [self.record])
        self.dialogs.append(dialog)
        self.wait(dialog)
        return dialog

    def test_import_approve_reopen_and_ab(self):
        dialog = self.single()
        candidate = dialog.import_source(self.wav)
        dialog.set_status('approved')
        approved = self.store.approved_path('MAIN|0000002a', 'Olá')
        self.assertTrue(approved.is_file())
        self.assertNotEqual(approved, self.wav)
        dialog.compare()
        self.wait_single(dialog)
        self.assertEqual(self.context.player.events[-1][0], 'compare')
        reopened = ReviewStore(self.root)
        self.assertEqual(reopened.list_candidates('MAIN|0000002a', 'Olá')[0]['id'], candidate['id'])
        self.assertEqual(reopened.approved_path('MAIN|0000002a', 'Olá'), approved)

    def test_live_text_change_blocks_approval(self):
        dialog = self.single()
        dialog.import_source(self.wav)
        self.text = 'Outro texto'
        dialog.set_status('approved')
        self.assertIn('Texto alterado', dialog.status_label.text())
        self.assertIsNone(self.store.approved_path('MAIN|0000002a', 'Olá'))

    def test_duration_override_is_explicit(self):
        self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'importado',
            {'duration_override_required': True})
        dialog = self.single()
        dialog.table.selectRow(2)
        dialog.set_status('approved')
        self.assertIsNone(self.store.approved_path('MAIN|0000002a', self.text))
        dialog.override.setChecked(True)
        dialog.set_status('approved')
        self.assertIsNotNone(self.store.approved_path('MAIN|0000002a', self.text))

    def test_reference_transcript_and_character_persist(self):
        dialog = self.single()
        dialog.character.setCurrentText('CJ')
        dialog.transcript.setPlainText('Hello there')
        dialog.add_original_reference()
        self.wait_single(dialog)
        reopened = ReviewStore(self.root)
        self.assertEqual(reopened.character('MAIN|0000002a'), 'CJ')
        reference = reopened.references('CJ')[0]
        self.assertEqual(reference['transcript'], 'Hello there')
        self.assertEqual(reference['provenance']['source'], 'original')
        self.assertTrue(self.wav.exists())

    def test_bulk_only_current_nonrejected_takes(self):
        records = [dict(self.record, hash=i, line_id='MAIN|%08x' % i) for i in range(3)]
        for index, record in enumerate(records):
            candidate = self.store.import_candidate(record['line_id'], self.wav,
                self.text if index != 1 else 'old', 'OmniVoice-like imported fixture')
            if index == 2:
                self.store.set_status(record['line_id'], candidate['id'], 'rejected', self.text)
        dialog = self.bulk(records)
        self.assertEqual(len(dialog.rows), 3)
        dialog.confirm()
        self.wait(dialog)
        self.assertIsNotNone(self.store.approved_path(records[0]['line_id'], self.text))
        self.assertIsNone(self.store.approved_path(records[1]['line_id'], self.text))
        self.assertIsNone(self.store.approved_path(records[2]['line_id'], self.text))

    def test_bulk_detects_edit_after_listing(self):
        self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'importado')
        dialog = self.bulk()
        self.text = 'New text'
        dialog.confirm()
        self.assertIn('Texto alterado', dialog.status_label.text())
        self.assertIsNone(self.store.approved_path('MAIN|0000002a', 'Olá'))

    def test_bulk_preserves_success_on_invalid_other_wav(self):
        records = [dict(self.record, hash=i, line_id='MAIN|%08x' % i) for i in range(2)]
        candidates = [self.store.import_candidate(r['line_id'], self.wav, self.text, 'importado') for r in records]
        dialog = self.bulk(records)
        self.store.resolve_path(candidates[1]['path']).write_bytes(b'corrupt')
        dialog.confirm()
        self.wait(dialog)
        self.assertIsNotNone(self.store.approved_path(records[0]['line_id'], self.text))
        self.assertIsNone(self.store.approved_path(records[1]['line_id'], self.text))
        self.assertIn('1 erros', dialog.status_label.text())

    def test_cancelled_generation_never_imports(self):
        dialog = self.single()
        generation = dialog.generation
        generation.job = SimpleNamespace(done=True, wait=lambda: SimpleNamespace(
            cancelled=True, timed_out=False, returncode=0, error=None, logs=[]))
        generation.poll()
        self.assertEqual(self.store.list_candidates('MAIN|0000002a', self.text), [])
        self.assertIn('cancelada', generation.logs.toPlainText())

    def test_close_during_generation_waits_for_completion(self):
        dialog = self.single()
        requests = []
        generation = dialog.generation
        generation.job = SimpleNamespace(done=False, cancel=lambda: requests.append(True))
        dialog.show()
        dialog.close()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(requests, [True])
        generation.job = None
        self.wait_single(dialog)
        dialog.close()
        self.assertFalse(dialog.isVisible())

    def test_generation_uses_shared_queue_and_explicit_parameters(self):
        dialog = self.single()
        prepared_calls, submissions = [], []
        def prepare(record, text, **options):
            prepared_calls.append((record, text, options))
            return {'argv': ['never-started-fake-worker'], 'request': {
                'reference_path': str(self.wav), 'reference_text': 'English original',
                'output_path': str(self.root / 'result.wav')}, 'receipt': self.root / 'result.omnivoice.json'}
        fake_job = SimpleNamespace(done=False, cancel=lambda: None)
        def submit(argv, **kwargs):
            submissions.append((argv, kwargs))
            return fake_job
        self.context.generation_request = prepare
        self.context.jobs = SimpleNamespace(submit_exclusive=submit)
        generation = dialog.generation
        generation.speed.setValue(.95)
        generation.duration.setText('livre')
        generation.seed.setText('7')
        generation.start()
        deadline = time.monotonic() + 5
        while not submissions and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(generation.busy)
        self.assertEqual(prepared_calls[0][2], {'speed': .95, 'duration': 'livre', 'seed': 7})
        self.assertEqual(submissions[0][0], ['never-started-fake-worker'])
        self.assertIn('English original', generation.reference.text())
        generation.timer.stop()
        generation.job = None
        generation.finish()

    def test_generated_take_keeps_snapshot_when_editor_changes(self):
        dialog = self.single()
        generation = dialog.generation
        output = self.root / 'result.wav'
        receipt = self.root / 'result.omnivoice.json'
        generation.snapshot = self.text
        generation.request = {}
        generation.prepared = {'output': output, 'receipt': receipt}
        calls = []
        self.store.import_omnivoice_candidate = lambda *args: calls.append(args) or {'id': 'new'}
        generation.job = SimpleNamespace(done=True, wait=lambda: SimpleNamespace(
            cancelled=False, timed_out=False, returncode=0, error=None, logs=[]))
        self.text = 'Edited while working'
        generation.poll()
        self.wait_single(dialog)
        self.assertEqual(calls, [('MAIN|0000002a', output, 'Olá', receipt)])

    def test_missing_original_is_safe(self):
        self.context.original_path = lambda record: None
        dialog = self.single()
        dialog.compare()
        self.wait_single(dialog)
        self.assertIn('Original indisponível', dialog.status_label.text())
        dialog.add_original_reference()
        self.assertEqual(self.store.references('CJ'), [])

    def test_generated_long_take_uses_receipt_next_to_actual_quarantine_output(self):
        import json
        dialog = self.single()
        generation = dialog.generation
        planned = self.root / 'result.wav'
        actual = self.root / 'quarantine' / 'result.wav'
        generation.snapshot = self.text
        generation.request = {}
        generation.prepared = {'output': planned, 'receipt': planned.with_suffix('.omnivoice.json')}
        calls = []
        self.store.import_omnivoice_candidate = lambda *args: calls.append(args) or {'id': 'new'}
        generation.job = SimpleNamespace(done=True, wait=lambda: SimpleNamespace(
            cancelled=False, timed_out=False, returncode=0, error=None,
            logs=[json.dumps({'output_path': str(actual), 'status': 'QUARANTINED_DURATION'})]))
        generation.poll()
        self.wait_single(dialog)
        self.assertEqual(calls, [('MAIN|0000002a', str(actual), 'Olá', actual.with_suffix('.omnivoice.json'))])

    def wait_single(self, dialog):
        deadline = time.monotonic() + 5
        idle = 0
        while idle < 2 and time.monotonic() < deadline:
            self.app.processEvents()
            idle = 0 if dialog.operation_active() else idle + 1
            time.sleep(.005)
        self.assertFalse(dialog.operation_active())

    def test_waveform_analyzes_selected_take(self):
        dialog = self.single()
        dialog.import_source(self.wav)
        self.wait_single(dialog)
        self.assertGreater(len(dialog.waveform.peaks), 0)
        self.assertIn('24000 Hz', dialog.analysis_label.text())
        self.assertLessEqual(len(dialog.waveform.peaks), 256)

    def test_effect_preserves_source_text_and_file(self):
        from studio.audio.analyzer import analyze_wav
        dialog = self.single()
        source = dialog.import_source(self.wav)
        source_bytes = self.store.resolve_path(source['path']).read_bytes()
        self.text = 'New editor text'
        dialog.effect_speed.setValue(.8)
        parameters = []
        def render(src, dest, **options):
            parameters.append(options)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            return analyze_wav(dest)
        with patch('studio.ui_qt.audio_review.render_effect', render):
            dialog.effect()
            self.wait_single(dialog)
        candidates = self.store.list_candidates('MAIN|0000002a', self.text)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[1]['text'], 'Olá')
        self.assertTrue(candidates[1]['stale'])
        self.assertEqual(candidates[1]['metadata']['effects']['source_candidate_id'], source['id'])
        self.assertEqual(self.store.resolve_path(source['path']).read_bytes(), source_bytes)
        self.assertEqual(parameters[0]['speed'], .8)
        self.assertEqual(candidates[1]['status'], 'pending')

    def test_close_cancels_effect_and_waits_for_worker(self):
        dialog = self.single()
        dialog.import_source(self.wav)
        started = threading.Event()
        def render(src, dest, cancel_event, **options):
            started.set()
            cancel_event.wait(3)
            raise RuntimeError('Effect cancelled')
        with patch('studio.ui_qt.audio_review.render_effect', render):
            dialog.show()
            dialog.effect()
            self.assertTrue(started.wait(2))
            dialog.close()
            self.assertTrue(dialog.isVisible())
            self.wait_single(dialog)
            dialog.close()
            self.assertFalse(dialog.isVisible())
        self.assertEqual(len(self.store.list_candidates('MAIN|0000002a', self.text)), 1)

    def test_install_result_is_friendly_and_preserves_error_detail(self):
        dialog = self.single()
        dialog.installed({'installed': ['MAIN|0000002a'], 'errors': [{'reason': 'Game running'}], 'skipped': []})
        self.assertIn('1 instalados', dialog.status_label.text())
        self.assertIn('1 erros', dialog.status_label.text())
        self.assertIn('Game running', dialog.status_label.text())
        self.assertNotIn("'installed':", dialog.status_label.text())

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_explicit_effect_runs_real_backend_in_isolated_workspace(self):
        dialog = self.single()
        source = dialog.import_source(self.wav)
        before = self.wav.read_bytes()
        dialog.effect_speed.setValue(.5)
        dialog.effect()
        self.wait_single(dialog)
        candidates = self.store.list_candidates('MAIN|0000002a', self.text)
        self.assertEqual(len(candidates), 2, dialog.status_label.text())
        derivative = candidates[-1]
        self.assertTrue(derivative['analysis']['valid'])
        self.assertGreater(derivative['analysis']['duration'], .15)
        self.assertTrue(derivative['metadata']['duration_override_required'])
        self.assertEqual(derivative['metadata']['effects']['source_sha256'], source['sha256'])
        self.assertEqual(self.wav.read_bytes(), before)
        self.assertIsNone(self.store.approved_path('MAIN|0000002a', self.text))

    def test_preparation_runs_off_ui_thread_and_cancel_prevents_submission(self):
        from PySide6.QtCore import QTimer
        dialog = self.single()
        main_thread = threading.get_ident()
        threads, submits, ticks = [], [], []
        started, release = threading.Event(), threading.Event()
        def prepare(record, text, **options):
            threads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return {'argv': ['fake-worker'], 'request': {'reference_path': str(self.wav)}}
        self.context.generation_request = prepare
        self.context.jobs = SimpleNamespace(submit_exclusive=lambda *args, **kwargs: submits.append(args))
        timer = QTimer(dialog)
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        dialog.generation.start()
        self.assertTrue(started.wait(2))
        self.assertNotEqual(threads, [main_thread])
        self.assertTrue(dialog.generation.busy)
        self.app.processEvents()
        time.sleep(.01)
        self.app.processEvents()
        self.assertTrue(ticks)
        dialog.show()
        dialog.close()
        self.assertTrue(dialog.isVisible())
        release.set()
        self.wait_single(dialog)
        self.assertEqual(submits, [])
        self.assertIn('nenhum processo', dialog.generation.logs.toPlainText())
        timer.stop()

    def test_original_and_installed_probes_run_off_ui_thread(self):
        from PySide6.QtCore import QTimer
        main_thread = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        original_threads, installed_threads, ticks = [], [], []
        def original(record):
            original_threads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return self.wav
        def installed(record):
            installed_threads.append(threading.get_ident())
            return self.wav
        self.context.original_path = original
        self.context.installed_path = installed
        dialog = self.single()
        self.assertTrue(started.wait(2))
        timer = QTimer(dialog)
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        time.sleep(.01)
        self.app.processEvents()
        self.assertTrue(ticks)
        self.assertNotEqual(original_threads, [main_thread])
        release.set()
        self.wait_single(dialog)
        dialog.resolve_audio('installed', dialog.play_path)
        self.wait_single(dialog)
        self.assertNotEqual(installed_threads, [main_thread])
        self.assertEqual(self.context.player.events[-1], ('play', self.wav))
        timer.stop()

    def test_initial_selection_prefers_current_approved_and_refresh_keeps_waveform(self):
        first = self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'importado')
        second = self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'importado')
        self.store.set_status('MAIN|0000002a', second['id'], 'approved', self.text)
        dialog = self.single()
        self.wait_single(dialog)
        self.assertEqual(dialog.selected()['id'], second['id'])
        self.assertEqual(dialog.table.item(2, 2).text(), 'Para ouvir')
        self.assertEqual(dialog.table.item(3, 2).text(), 'Aprovado')
        self.assertTrue(dialog.waveform.peaks)
        token, worker = dialog.analysis_token, dialog.analysis_worker
        dialog.refresh()
        self.assertEqual(dialog.analysis_token, token)
        self.assertIs(dialog.analysis_worker, worker)
        dialog.table.selectRow(2)
        self.wait_single(dialog)
        self.assertEqual(dialog.selected()['id'], first['id'])
        dialog.refresh()
        self.assertEqual(dialog.selected()['id'], first['id'])

    def test_first_take_autoselect_and_bulk_status_labels_preserve_codes(self):
        records = [dict(self.record, hash=i, line_id='MAIN|%08x' % i) for i in range(3)]
        for index, record in enumerate(records):
            candidate = self.store.import_candidate(record['line_id'], self.wav, self.text, 'importado')
            if index == 1:
                self.store.set_status(record['line_id'], candidate['id'], 'approved', self.text)
            if index == 2:
                self.store.set_status(record['line_id'], candidate['id'], 'rejected', self.text)
        self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'importado')
        single = self.single()
        self.wait_single(single)
        self.assertEqual(single.table.currentRow(), 2)
        self.assertTrue(single.waveform.peaks)
        bulk = self.bulk(records)
        self.assertEqual([bulk.table.item(i, 1).text() for i in range(3)],
                         ['Para ouvir', 'Aprovado', 'Recusado'])
        self.assertEqual([row['state'] for row in bulk.rows], ['PENDING', 'APPROVED', 'REJECTED'])

    def test_unified_sources_are_visible_and_generating_selects_without_extra_accept(self):
        self.context.installed_path = lambda record: self.wav
        dialog = self.single()
        choices = []
        dialog.selectionChanged.connect(lambda record, choice: choices.append(choice))
        self.wait_single(dialog)
        self.assertEqual(dialog.table.item(0, 0).text(), 'Original do jogo')
        self.assertEqual(dialog.table.item(1, 0).text(), 'Instalado no mod')
        self.assertEqual(choices, [])
        candidate = self.store.import_candidate('MAIN|0000002a', self.wav, self.text, 'Gerado')
        dialog.generated(candidate)
        self.wait_single(dialog)
        self.assertEqual(dialog.selected()['id'], candidate['id'])
        self.assertEqual(choices[-1]['id'], candidate['id'])
        self.assertIsNone(self.store.approved_path('MAIN|0000002a', self.text))
        before = len(choices)
        dialog.refresh()
        self.wait_single(dialog)
        self.assertEqual(len(choices), before)


if __name__ == '__main__':
    unittest.main()
