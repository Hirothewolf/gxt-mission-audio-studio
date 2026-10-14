import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import wave
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from studio.ui_qt.context import QtContext
from studio.ui_qt.batch import BatchGenerationDialog
from studio.ui_qt.scene import prepare_scene
from studio.audio.timeline_project import TimelineProject, digest

app = QApplication.instance() or QApplication([])


def wait(predicate):
    limit = time.monotonic() + 5
    while not predicate() and time.monotonic() < limit:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def wav(path, count=2400):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes((b'\x64\x00' * count))


def test_original_fallback_unpacks_resolver_tuple():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'original.wav'
        wav(source)
        context = QtContext.__new__(QtContext)
        context.info = lambda record: {'stable_audio_id': 'SCRIPT:B000:S000'}
        context.game_audio = SimpleNamespace(original_para=lambda info: None)
        context.base_audio = SimpleNamespace(resolve=lambda *args: (source, 'exact', 'verified'))
        context._resolver_ready = True
        assert context.original_path({'table': 'MAIN', 'hash': 42, 'name': 'KEY'}) == source
        context.base_audio.resolve = lambda *args: (None, None, None)
        assert context.original_path({'table': 'MAIN', 'hash': 42}) is None


def test_install_rejects_text_changed_before_bank_call():
    context = QtContext.__new__(QtContext)
    context.info = lambda record: {}
    context.current_text = lambda record: 'new text'
    with patch('studio.ui_qt.context.install_rows') as install:
        try:
            context.install([dict(table='MAIN', hash=1, text='old text')])
        except ValueError as exc:
            assert 'texto mudou' in str(exc)
        else:
            assert False, 'stale text was accepted'
        install.assert_not_called()


def test_finite_batch_preserves_approved_and_reports_failed_targets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = [dict(table='MAIN', hash=i, text=str(i), name=str(i), info={'pak': 'SCRIPT'}) for i in (1, 2, 3)]
        prepared = []
        imported = []
        def request(record, text, *args):
            assert args == (1.0, 'livre', None)
            prepared.append(record['hash'])
            if record['hash'] == 2:
                raise ValueError('missing reference')
            request_path = root / 'request.json'
            request_path.write_text(json.dumps({'seed': 12345, 'duration': None,
                'reference_path': 'exact/original.wav', 'reference_sha256': 'original-hash'}))
            return dict(argv=['fake'], run=root / 'run', output=root / 'output.wav', receipt=root / 'receipt.json', request=request_path)
        quarantine = root / 'quarantine'
        quarantine.mkdir()
        (quarantine / 'output.wav').write_bytes(b'preserved longer audio')
        (quarantine / 'receipt.json').write_text('{}')
        queue = SimpleNamespace(submit_exclusive=lambda *a, **k: SimpleNamespace(
            wait=lambda: SimpleNamespace(cancelled=False, returncode=0), cancel=lambda: None))
        def import_candidate(line_id, *args):
            assert args[0] == quarantine / 'output.wav'
            assert args[2] == quarantine / 'receipt.json'
            imported.append(line_id)
            return {'id': 'candidate'}
        context = SimpleNamespace(project_root=root, current_text=lambda r: r['text'],
            approved_path=lambda line_id, text: root / 'approved.wav' if text == '1' else None,
            generation_request=request, jobs=queue,
            review_store=SimpleNamespace(import_omnivoice_candidate=import_candidate))
        dialog = BatchGenerationDialog(context, rows)
        candidate_events = []
        dialog.candidatesReady.connect(candidate_events.extend)
        dialog.start()
        wait(lambda: dialog.job is None)
        report = json.loads(next((root / 'workspace/qt_batches').glob('*.json')).read_text(encoding='utf-8'))
        assert report['complete']
        assert [r['status'] for r in report['records']] == ['preserved_approved', 'failed', 'generated_for_review']
        assert report['records'][2]['seed'] == 12345
        assert report['records'][2]['request'] == str(root / 'request.json')
        assert report['records'][2]['reference_path'] == 'exact/original.wav'
        assert report['records'][2]['reference_sha256'] == 'original-hash'
        assert report['records'][2]['receipt'] == str(quarantine / 'receipt.json')
        assert prepared == [2, 3]
        assert imported == ['MAIN|00000003']
        assert candidate_events[0]['record']['line_id'] == 'MAIN|00000003'
        assert candidate_events[0]['candidate']['id'] == 'candidate'
        assert dialog.progress.value() == 3
        assert '1 gerado(s)' in dialog.logs.toPlainText()
        dialog.close()
        # Releasing the dialog before its finished Task's DeferredDelete used
        # to abort the process on the next unrelated timeline event pump.
        del dialog
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_regenerating_approved_is_explicit_and_keeps_old_take():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_take = root / 'approved.wav'
        old_take.write_bytes(b'approved take')
        row = dict(table='MAIN', hash=1, text='current', name='KEY', info={'pak': 'SCRIPT'})
        request_calls = []
        request_path = root / 'request.json'
        request_path.write_text(json.dumps({'seed': 45, 'duration': None}))
        output, receipt = root / 'output.wav', root / 'receipt.json'
        output.write_bytes(b'new take')
        receipt.write_text('{}')
        candidate = {'id': 'new-candidate', 'sha256': 'new-hash', 'text_hash': 'text-hash'}
        queue = SimpleNamespace(submit_exclusive=lambda *a, **k: SimpleNamespace(
            wait=lambda: SimpleNamespace(cancelled=False, returncode=0), cancel=lambda: None))
        def generate(record, text, *args):
            request_calls.append((record['line_id'], text, args))
            return dict(argv=['fake'], run=root / 'run', output=output, receipt=receipt, request=request_path)
        context = SimpleNamespace(project_root=root, current_text=lambda r: r['text'],
            approved_path=lambda *args: old_take, generation_request=generate, jobs=queue,
            review_store=SimpleNamespace(import_omnivoice_candidate=lambda *args: candidate,
                validate_candidate_source=lambda *args: candidate))
        dialog = BatchGenerationDialog(context, [row])
        assert dialog.regenerate_approved.isChecked() is False
        dialog.regenerate_approved.setChecked(True)
        dialog.candidatesReady.connect(lambda items: None)
        dialog.start()
        wait(lambda: dialog.job is None)
        report = json.loads(next((root / 'workspace/qt_batches').glob('*.json')).read_text(encoding='utf-8'))
        assert request_calls == [('MAIN|00000001', 'current', (1.0, 'livre', None))]
        assert report['records'][0]['status'] == 'generated_for_review'
        assert old_take.read_bytes() == b'approved take'
        dialog.close()
        del dialog
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_batch_freezes_current_editor_text_and_skips_if_it_changes_in_queue():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        row = dict(table='MAIN', hash=9, text='stale row text', name='KEY', info={'pak': 'SCRIPT'})
        text_reads = iter(('current editor text', 'changed while queued'))
        requested = []
        context = SimpleNamespace(project_root=root, current_text=lambda _record: next(text_reads),
            approved_path=lambda *args: None,
            generation_request=lambda *args: requested.append(args),
            jobs=SimpleNamespace(), review_store=SimpleNamespace())
        dialog = BatchGenerationDialog(context, [row])
        dialog.start()
        wait(lambda: dialog.job is None)
        report = json.loads(next((root / 'workspace/qt_batches').glob('*.json')).read_text(encoding='utf-8'))
        assert report['records'][0]['text'] == 'current editor text'
        assert report['records'][0]['status'] == 'skipped_text_changed'
        assert requested == []
        assert '1 texto(s) alterado(s)' in dialog.logs.toPlainText()
        dialog.close()
        del dialog
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_batch_cancel_preserves_completed_ledger_and_counts_unstarted_rows():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = [dict(table='MAIN', hash=i, text=str(i), name=str(i), info={'pak': 'SCRIPT'})
                for i in (1, 2)]
        dialog_box = []
        class ChildJob:
            cancelled = False
            def cancel(self):
                self.cancelled = True
            def wait(self):
                dialog_box[0].cancel()
                return SimpleNamespace(cancelled=True, returncode=-1, error='', logs=[])
        child = ChildJob()
        queue = SimpleNamespace(submit_exclusive=lambda *a, **k: child)
        context = SimpleNamespace(project_root=root, current_text=lambda record: record['text'],
            approved_path=lambda *args: None,
            generation_request=lambda *args: dict(argv=['fake'], run=root, output=root / 'out.wav',
                receipt=root / 'receipt.json'), jobs=queue,
            review_store=SimpleNamespace())
        dialog = BatchGenerationDialog(context, rows)
        dialog_box.append(dialog)
        dialog.start()
        wait(lambda: dialog.job is None)
        report = json.loads(next((root / 'workspace/qt_batches').glob('*.json')).read_text(encoding='utf-8'))
        assert report['cancelled'] is True
        assert report['complete'] is False
        assert report['counts'] == {'generated': 0, 'preserved_approved': 0, 'failed': 0,
            'skipped_text_changed': 0, 'cancelled': 1, 'not_started': 1}
        assert 'cancelado: 0 gerado(s)' in dialog.logs.toPlainText()
        assert child.cancelled is True
        dialog.close()
        del dialog
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_full_scene_keeps_overlap_and_approved_takes_without_original_mix():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source, take = root / 'source.wav', root / 'pt.wav'
        wav(source, 24000)
        wav(take, 12000)
        rows = [dict(table='INTRO1', hash=i, name='PRO@%d' % i, text='fala %d' % i,
            line_id='INTRO1|%08x' % i, info={'id': i}) for i in (1, 2)]
        route = dict(family='streams', container='BEATS', track='Track_010')
        def lookup(info, project_root):
            start = 0 if info['id'] == 1 else .25
            return dict(source_path=str(source), source_sha256=digest(source), start=start, end=start+.5,
                        status='ALIGNMENT_CANDIDATE', timing_authority='saved')
        context = SimpleNamespace(project_root=root, approved_path=lambda *args: take)
        unrelated = dict(table='OTHER', hash=9, name='AMBIGUOUS', text='Outra cena',
            line_id='OTHER|00000009', info={'ambiguous': True})
        def resolve_route(info):
            if info.get('ambiguous'):
                raise ValueError('Ambiguous unrelated scene')
            return route
        import threading
        native = dict(source_path=str(source), source_sha256=digest(source), archive_path=str(source), archive_sha256=digest(source))
        with patch('studio.ui_qt.scene.audio_route', side_effect=resolve_route), patch('studio.ui_qt.scene.lookup_window', side_effect=lookup), patch('studio.ui_qt.scene_sources.full_track', side_effect=[native, None]):
            result = prepare_scene(context, rows[0], rows + [unrelated], lambda text: None, threading.Event())
        project = TimelineProject.load(result['path'])
        assert result['linked'] == result['approved'] == 2, project.data['scene']['unavailable_windows']
        assert len(project.data['clips']) == 5
        assert len([track for track in project.data['tracks'] if track['role'] == 'pt']) == 1
        assert project.data['scene']['unroutable_catalog_records'][0]['line_id'] == unrelated['line_id']
        output = root / 'render.wav'
        rendered = project.render(output)
        assert rendered['mixed_clips'] == 2
        with wave.open(str(output), 'rb') as handle:
            assert handle.getnframes() == 24000
        assert digest(source) == lookup(rows[0]['info'], root)['source_sha256']
