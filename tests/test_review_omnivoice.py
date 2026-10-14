import hashlib
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from studio.projects.review import ReviewStore


def make_wav(path, seconds=4.0):
    rate = 24000
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(
            struct.pack('<h', int(7000 * math.sin(index / 9)))
            for index in range(int(rate * seconds))))
    return path


class OmniVoiceReviewTakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ReviewStore(self.root)
        self.source = make_wav(self.root / 'original-reference.wav', 1.8)
        self.line_id = 'INTRO2|a1912691'
        self.text = 'Calma, está tudo bem.'

    def test_legacy_trimmed_receipt_allows_outer_whitespace_only(self):
        path = make_wav(self.root / 'trimmed.wav')
        receipt = self.receipt(path)
        candidate = self.store.import_omnivoice_candidate(self.line_id, path, '  ' + self.text + '  ', receipt)
        self.assertEqual(candidate['status'], 'pending')
        with self.assertRaises(ValueError):
            self.store.import_omnivoice_candidate(self.line_id, path, self.text + ' Outra fala.', receipt)

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, path, status='QUARANTINED_DURATION', text=None, window=3.0):
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = {
            'schema': 'omnivoice-single-take-result/1', 'engine': 'OmniVoice',
            'status': status, 'output_path': str(path.resolve()), 'output_sha256': output_hash,
            'mission': 'INTRO2', 'jamcrc': 'A1912691', 'target_gxt_id': 'INTRO2:A1912691',
            'duration_seconds': 4.0, 'window_seconds': window,
            'target_text': text or self.text, 'spoken_target_text': text or self.text,
            'sample_rate': 24000, 'rms': 0.15, 'peak': 0.3, 'clipping_ratio': 0,
            'reference_path': str(self.source.resolve()), 'reference_sha256': source_hash,
            'provenance': {
                'source_kind': 'original_game_saat_cache',
                'source_path': str(self.source.resolve()), 'source_sha256': source_hash,
                'base_english_gxt_id': 'INTRO2:A1912691',
            },
        }
        receipt_path = path.with_suffix('.omnivoice.json')
        receipt_path.write_text(json.dumps(payload), encoding='utf-8')
        return receipt_path

    def make_take(self, name='take.wav', **receipt_kwargs):
        path = make_wav(self.root / name, 4.0)
        receipt = self.receipt(path, **receipt_kwargs)
        return path, receipt

    def test_duration_quarantine_is_reviewable_and_explicit_override_accepts(self):
        take, receipt = self.make_take()
        candidate = self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)

        self.assertEqual(candidate['status'], 'pending')
        self.assertTrue(candidate['metadata']['duration_override_required'])
        with self.assertRaisesRegex(ValueError, 'janela'):
            self.store.set_status(self.line_id, candidate['id'], 'approved', self.text)
        self.store.set_status(self.line_id, candidate['id'], 'approved', self.text, duration_override=True)

        self.assertEqual(self.store.list_candidates(self.line_id, self.text)[0]['status'], 'approved')
        self.assertEqual(self.store.approved_path(self.line_id, self.text), self.store.resolve_path(candidate['path']))

    def test_long_complete_take_imports_and_requires_only_window_override(self):
        take = make_wav(self.root / 'long.wav', 12.5)
        receipt = self.receipt(take)
        data = json.loads(receipt.read_text())
        data['duration_seconds'] = 12.5
        receipt.write_text(json.dumps(data))
        candidate = self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)
        with self.assertRaisesRegex(ValueError, 'janela'):
            self.store.set_status(self.line_id, candidate['id'], 'approved', self.text)
        self.store.set_status(self.line_id, candidate['id'], 'approved', self.text, duration_override=True)
        with wave.open(str(self.store.approved_path(self.line_id, self.text))) as audio:
            self.assertEqual(audio.getnframes(), 300000)

    def test_rejected_take_can_be_retried_without_overwriting_prior_take(self):
        first, first_receipt = self.make_take('first.wav')
        one = self.store.import_omnivoice_candidate(self.line_id, first, self.text, first_receipt)
        self.store.set_status(self.line_id, one['id'], 'rejected', self.text)

        second, second_receipt = self.make_take('second.wav')
        two = self.store.import_omnivoice_candidate(self.line_id, second, self.text, second_receipt)

        candidates = self.store.list_candidates(self.line_id, self.text)
        self.assertEqual({row['id'] for row in candidates}, {one['id'], two['id']})
        self.assertEqual({row['status'] for row in candidates}, {'rejected', 'pending'})
        self.assertTrue(self.store.resolve_path(one['path']).is_file())
        self.assertTrue(self.store.resolve_path(two['path']).is_file())
        self.assertNotEqual(one['path'], two['path'])

    def test_stale_text_cannot_be_accepted_even_with_duration_override(self):
        take, receipt = self.make_take()
        candidate = self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)
        with self.assertRaisesRegex(ValueError, 'Texto alterado'):
            self.store.set_status(self.line_id, candidate['id'], 'approved', 'Texto novo', duration_override=True)

    def test_same_text_from_another_gxt_line_cannot_be_attached(self):
        take, receipt = self.make_take()
        payload = json.loads(receipt.read_text(encoding='utf-8'))
        payload['provenance']['base_english_gxt_id'] = 'INTRO1:00000001'
        receipt.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outra fala'):
            self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)

    def test_actual_duration_and_sample_rate_must_match_receipt(self):
        take, receipt = self.make_take()
        payload = json.loads(receipt.read_text(encoding='utf-8'))
        payload['duration_seconds'] = 3.7
        receipt.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duração do WAV'):
            self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)

    def test_original_window_over_ten_seconds_is_allowed_for_short_output(self):
        take, receipt = self.make_take(status='UNREVIEWED_WITHIN_WINDOW', window=12.0)
        candidate = self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)
        self.store.set_status(self.line_id, candidate['id'], 'approved', self.text)
        self.assertEqual(self.store.approved_path(self.line_id, self.text), self.store.resolve_path(candidate['path']))

    def test_review_worker_route_keeps_duration_quarantine_visible(self):
        from studio.ui.audio_review import AudioReviewWindow
        payload = AudioReviewWindow._result_payload([
            'progress: loading',
            json.dumps({'status': 'QUARANTINED_DURATION', 'output_path': 'run/quarantine/take.wav',
                        'duration_seconds': 4.0, 'window_seconds': 3.0}),
        ])
        self.assertEqual(payload['status'], 'QUARANTINED_DURATION')
        self.assertEqual(payload['output_path'], 'run/quarantine/take.wav')

    def test_review_generate_button_retries_after_rejection_with_snapshot_controls(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from studio.ui.audio_review import AudioReviewWindow

        def control(value=None):
            return SimpleNamespace(get=lambda: value, set=lambda new: None, config=lambda **kwargs: None)

        class FakeJob:
            def __init__(self, logs):
                self.logs = logs

            def wait(self):
                return SimpleNamespace(returncode=0, cancelled=False, timed_out=False, error=None, logs=self.logs)

            def cancel(self):
                return None

        first, first_receipt = self.make_take('rejected-first.wav')
        rejected = self.store.import_omnivoice_candidate(self.line_id, first, self.text, first_receipt)
        self.store.set_status(self.line_id, rejected['id'], 'rejected', self.text)
        run = self.root / 'retry-run'
        output = run / 'output' / 'INTRO2' / 'quarantine' / 'take.wav'
        output.parent.mkdir(parents=True)
        make_wav(output, 4.0)
        receipt = self.receipt(output)
        result_line = json.dumps({'status': 'QUARANTINED_DURATION', 'output_path': str(output)})
        request = run / 'request.json'
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(json.dumps({
            'reference_path': str(self.source), 'reference_text': 'original words', 'speed': .8,
            'duration': 4.0, 'seed': 101, 'window_seconds': 3.0,
            'provenance': {'reference_audio_id': 'SCRIPT:B091:S025'},
        }), encoding='utf-8')

        def submit_fake(command, cwd, on_line, timeout):
            on_line('fake worker running')
            return FakeJob([result_line])

        app = SimpleNamespace(g=object(), jobs=SimpleNamespace(submit_exclusive=submit_fake),
            nomes_tabela=lambda *_a, **_k: {0xA1912691: 'A1912691'},
            ui=lambda callback: callback())
        app.g = object()
        review = AudioReviewWindow.__new__(AudioReviewWindow)
        review.app = app
        review.gxt_context = app.g
        review.tab, review.h = 'INTRO2', 0xA1912691
        review.line_id = self.line_id
        review.store = self.store
        review.busy = False
        review.generation_busy = False
        review.generation_job = None
        review.closed = False
        review.candidates = {}
        review.tree = SimpleNamespace(selection=lambda: ())
        review.generate_btn = control()
        review.accept_btn = control()
        review.reject_btn = control()
        review.effect_btn = control()
        review.duration_override_check = control()
        review.duration_override_var = control()
        review.duration_context_var = control()
        review.message_var = control()
        review.win = None
        review.omni_speed_var = control('.8')
        review.omni_duration_var = control('4.0')
        review.omni_seed_var = control('101')
        review.omni_cj_reference_var = control(False)
        review.omni_reference_var = control()
        review.generation_snapshot_text = None
        review.refresh = lambda selected=None: setattr(review, 'refreshed_candidate', selected)
        engine = SimpleNamespace(id='omnivoice', name='OmniVoice')
        app._prepare_generation = lambda mission, selected_hash=None: (
            engine, run, {review.h: self.text})
        captured = {}
        def build_command(*_args, **kwargs):
            captured.update(kwargs['settings'])
            return ['fake-python', str(request)]
        app._omnivoice_command_for_selection = build_command

        with patch('studio.tts.registry.select_engine', return_value=engine):
            review.generate_omnivoice_take()
            review.generation_thread.join(5)

        self.assertEqual(review.refreshed_candidate, self.store.list_candidates(self.line_id, self.text)[1]['id'])
        candidates = self.store.list_candidates(self.line_id, self.text)
        self.assertEqual([item['status'] for item in candidates], ['rejected', 'pending'])
        self.assertEqual(review.generation_snapshot_text, self.text)
        self.assertEqual(captured, {'speed': '.8', 'duration': '4.0', 'seed': 101,
                                    'use_cj_reference': False})
        self.assertIn('fake worker running', (run / 'tts.log').read_text(encoding='utf-8'))

    def test_source_hash_failure_cannot_be_overridden(self):
        take, receipt = self.make_take()
        candidate = self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)
        self.source.write_bytes(b'changed reference')
        with self.assertRaisesRegex(ValueError, 'Fonte original inválida'):
            self.store.set_status(self.line_id, candidate['id'], 'approved', self.text, duration_override=True)

    def test_silence_quarantine_and_invalid_source_are_not_reviewable(self):
        take, receipt = self.make_take(status='QUARANTINED_SILENCE')
        with self.assertRaisesRegex(ValueError, 'silêncio'):
            self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)

        take, receipt = self.make_take('bad-source.wav')
        payload = json.loads(receipt.read_text(encoding='utf-8'))
        payload['provenance']['source_sha256'] = '0' * 64
        receipt.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Fonte original inválida'):
            self.store.import_omnivoice_candidate(self.line_id, take, self.text, receipt)


if __name__ == '__main__':
    unittest.main()
