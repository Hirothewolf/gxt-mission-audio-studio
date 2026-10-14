import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from test_review import make_wav
from studio.projects.review import ReviewStore


class GenerationResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / 'run' / 'output' / 'RYDER1'
        self.out.mkdir(parents=True)
        self.store = ReviewStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self):
        from studio.tts.results import import_generation_run
        return import_generation_run(self.store, self.out, 'RYDER1', {123: 'Olá'}, 'Qwen3-TTS')

    def test_valid_report_imports_pending_candidate(self):
        make_wav(self.out / 'take.wav')
        (self.out / 'redub_relatorio.json').write_text(json.dumps([
            {'jamcrc': '0000007B', 'arquivo_audio': 'take.wav', 'personagem': 'RYDER'}]), encoding='utf-8')
        result = self.run_import()
        self.assertEqual(result['valid'], 1)
        self.assertEqual(result['missing'], 0)
        candidate = self.store.list_candidates('RYDER1|0000007b', 'Olá')[0]
        self.assertEqual(candidate['status'], 'pending')
        self.assertEqual(candidate['metadata']['personagem'], 'RYDER')
        self.assertIsNone(self.store.approved_path('RYDER1|0000007b', 'Olá'))

    def test_missing_or_bad_wav_is_not_success(self):
        (self.out / 'redub_relatorio.json').write_text(json.dumps([
            {'jamcrc': '0000007B', 'arquivo_audio': 'missing.wav'}]), encoding='utf-8')
        result = self.run_import()
        self.assertEqual(result['valid'], 0)
        self.assertEqual(result['missing'], 1)

    def test_high_wer_is_reviewable_and_run_candidates_are_identified(self):
        make_wav(self.out / 'take.wav')
        (self.out / 'redub_relatorio.json').write_text(json.dumps([
            {'jamcrc': '0000007B', 'arquivo_audio': 'take.wav', 'wer': .9}]), encoding='utf-8')
        result = self.run_import()
        self.assertEqual(result['valid'], 1)
        item = result['imported'][0]
        self.assertEqual(item['line_id'], 'RYDER1|0000007b')
        self.store.set_status(item['line_id'], item['candidate_id'], 'approved', 'Olá')
        self.assertIsNotNone(self.store.approved_path(item['line_id'], 'Olá'))

    def test_duration_difference_is_manual_review_not_lost_audio(self):
        import wave
        make_wav(self.out / 'take.wav')
        with wave.open(str(self.out / 'take.wav'), 'rb') as stream:
            frames = stream.readframes(stream.getnframes())
            params = stream.getparams()
        with wave.open(str(self.out / 'take.wav'), 'wb') as stream:
            stream.setparams(params)
            stream.writeframes(frames * 22)
        (self.out / 'redub_relatorio.json').write_text(json.dumps([
            {'jamcrc': '0000007B', 'arquivo_audio': 'take.wav'}]), encoding='utf-8')
        from studio.tts.results import import_generation_run
        result = import_generation_run(self.store, self.out, 'RYDER1', {123: 'Olá'}, 'Qwen3-TTS', {123: .5})
        self.assertEqual(result['valid'], 1)
        item = result['imported'][0]
        with self.assertRaises(ValueError):
            self.store.set_status(item['line_id'], item['candidate_id'], 'approved', 'Olá')
        self.store.set_status(item['line_id'], item['candidate_id'], 'approved', 'Olá', duration_override=True)
        self.assertIsNotNone(self.store.approved_path(item['line_id'], 'Olá'))

    def test_report_cannot_import_outside_run_or_unknown_identity(self):
        make_wav(self.root / 'external.wav')
        (self.out / 'redub_relatorio.json').write_text(json.dumps([
            {'jamcrc': '0000007B', 'arquivo_audio': '../../../external.wav'},
            {'jamcrc': '0000007C', 'arquivo_audio': 'take.wav'}]), encoding='utf-8')
        result = self.run_import()
        self.assertEqual(result['valid'], 0)
        self.assertEqual(result['quarantined'], 2)

    def test_omnivoice_report_imports_receipt_pinned_take_for_review(self):
        from studio.core.utils import sha256_arquivo

        (self.out / 'quarantine').mkdir(exist_ok=True)
        take = make_wav(self.out / 'quarantine' / 'take.wav')
        reference = make_wav(self.root / 'original.wav')
        digest = hashlib.sha256(reference.read_bytes()).hexdigest()
        receipt = {
            'schema': 'omnivoice-single-take-result/1', 'engine': 'OmniVoice',
            'status': 'QUARANTINED_DURATION', 'output_path': str(take.resolve()),
            'mission': 'RYDER1', 'jamcrc': '0000007B', 'target_gxt_id': 'RYDER1:0000007B',
            'output_sha256': sha256_arquivo(take), 'duration_seconds': .1,
            'window_seconds': .05, 'sample_rate': 8000, 'rms': .1,
            'peak': .2, 'clipping_ratio': 0, 'target_text': 'Olá',
            'reference_path': str(reference.resolve()), 'reference_sha256': digest,
            'provenance': {
                'source_kind': 'original_game_saat_cache', 'source_path': str(reference.resolve()),
                'source_sha256': digest, 'base_english_gxt_id': 'RYDER1:0000007B',
            },
        }
        receipt_path = self.out / 'quarantine' / 'take.omnivoice.json'
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        (self.out / 'redub_relatorio.json').write_text(json.dumps([{
            'jamcrc': '0000007B', 'arquivo_audio': 'quarantine/take.wav',
            'omnivoice_receipt': 'quarantine/take.omnivoice.json',
        }]), encoding='utf-8')

        from studio.tts.results import import_generation_run
        result = import_generation_run(self.store, self.out, 'RYDER1', {123: 'Olá'}, 'OmniVoice')

        self.assertEqual(result['valid'], 1)
        candidate = self.store.list_candidates('RYDER1|0000007b', 'Olá')[0]
        self.assertEqual(candidate['metadata']['engine'], 'OmniVoice')
        self.assertTrue(candidate['metadata']['duration_override_required'])
        with self.assertRaises(ValueError):
            self.store.set_status('RYDER1|0000007b', candidate['id'], 'approved', 'Olá')
        self.store.set_status('RYDER1|0000007b', candidate['id'], 'approved', 'Olá', duration_override=True)


if __name__ == '__main__':
    unittest.main()
