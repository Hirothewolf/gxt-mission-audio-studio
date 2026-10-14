import math
import tempfile
import unittest
import wave
from pathlib import Path

from studio.audio.voice_identity import (
    VoiceIdentityStore,
    analyze_items,
    collect_original_items,
)


def make_wav(path, *, frequency=220, seconds=1.4):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        samples = [int(9000 * math.sin(2 * math.pi * frequency * i / 8000))
                   for i in range(int(seconds * 8000))]
        output.writeframes(b''.join(value.to_bytes(2, 'little', signed=True) for value in samples))
    return path


class FakeGameAudio:
    def __init__(self, path):
        self.indice = {('SCRIPT', 1, 1): path}

    def _cache_original_atual(self, pak):
        return pak == 'SCRIPT'


class CountingEmbedder:
    model_id = 'layout-test'
    version = '1'

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(str(path))
        return [1.0, 0.0]


class VoiceIdentityLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_multi_speaker_layout_is_preserved_and_abstains_before_embedding(self):
        path = make_wav(self.root / 'multi.wav')
        manifest = [{
            'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'CUTSCENE',
            'speaker_layout': 'multi_speaker', 'speaker_count': 2,
            'speaker_identity': 'CJ', 'speaker_identity_status': 'VERIFIED',
        }]
        item = collect_original_items(FakeGameAudio(path), manifest)[0]
        self.assertEqual(item['links'][0]['speaker_layout'], 'multi_speaker')
        self.assertEqual(item['links'][0]['speaker_count'], 2)

        embedder = CountingEmbedder()
        report = analyze_items([item], embedder, VoiceIdentityStore(self.root))

        self.assertEqual(embedder.calls, [])
        result = report['items'][0]
        self.assertEqual(result['status'], 'MULTI_SPEAKER')
        self.assertEqual(result['speaker_layout'], 'multi_speaker')
        self.assertEqual(result['known_identity'], 'CJ')
        self.assertEqual(report['stats']['unique_embeddings'], 0)
        self.assertEqual(report['stats']['reference_identities'], 0)

    def test_unreviewed_multi_caption_audio_remains_unknown_and_is_embedded(self):
        path = make_wav(self.root / 'captions.wav')
        records = [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'CUTSCENE_A', 'source_text': 'First caption'},
            {'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'CUTSCENE_B', 'source_text': 'Second caption'},
        ]
        item = collect_original_items(FakeGameAudio(path), records)[0]
        embedder = CountingEmbedder()

        report = analyze_items([item], embedder, VoiceIdentityStore(self.root))

        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(report['items'][0]['status'], 'UNKNOWN')
        self.assertNotIn('speaker_layout', report['items'][0])

    def test_conflicting_confirmed_annotations_remain_seed_conflict_not_multi_voice(self):
        path = make_wav(self.root / 'conflict.wav')
        item = collect_original_items(FakeGameAudio(path), [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'line_id': 'a',
             'speaker_identity': 'CJ', 'speaker_identity_status': 'VERIFIED'},
            {'stable_audio_id': 'SCRIPT:B000:S000', 'line_id': 'b',
             'speaker_identity': 'Ryder', 'speaker_identity_status': 'VERIFIED'},
        ])[0]
        embedder = CountingEmbedder()

        report = analyze_items([item], embedder, VoiceIdentityStore(self.root))

        self.assertEqual(report['items'][0]['status'], 'SEED_CONFLICT')
        self.assertEqual(report['items'][0]['reason'], 'conflicting_known_identities')
        self.assertEqual(item['evidence']['seed_conflict']['source'], 'manifest_verified')
        self.assertNotEqual(report['items'][0]['status'], 'MULTI_SPEAKER')

    def test_manual_annotation_still_precedes_conflicting_verified_annotation(self):
        path = make_wav(self.root / 'manual.wav')

        class Reviews:
            def character(self, line_id):
                return 'CJ manual' if line_id == 'CUTSCENE|00000001' else ''

        item = collect_original_items(FakeGameAudio(path), [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'CUTSCENE', 'jamcrc': '1',
             'speaker_identity': 'Ryder', 'speaker_identity_status': 'VERIFIED'},
        ], Reviews())[0]
        self.assertEqual(item['known_identity'], 'CJ manual')
        self.assertEqual(item['evidence'], {'seed_source': 'manual_review'})


if __name__ == '__main__':
    unittest.main()
