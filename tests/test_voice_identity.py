import json
import math
import random
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from studio.audio.voice_identity import (
    VoiceIdentityStore,
    analyze_items,
    collect_original_items,
    coordinate_key,
)


def make_wav(path, *, seconds=1.4, frequency=220, amplitude=9000, noise=False, rate=8000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        samples = []
        for index in range(int(seconds * rate)):
            value = (rng.randint(-amplitude, amplitude) if noise else
                     int(amplitude * math.sin(2 * math.pi * frequency * index / rate)))
            samples.append(max(-32768, min(32767, value)))
        output.writeframes(b''.join(int(value).to_bytes(2, 'little', signed=True) for value in samples))
    return path


class FakeGameAudio:
    def __init__(self, root, *, fresh=True):
        self._fresh = fresh
        self._cache = Path(root)
        self.cache_root = self._cache.parent
        self.indice = {}
        self.game_dir = Path(root)

    def _cache_original_atual(self, pak):
        return pak == 'SCRIPT' and self._fresh

    def cache_dir(self):
        return self._cache


class FakeEmbedder:
    model_id = 'fixture-v1'
    version = '1'

    def __call__(self, path):
        name = Path(path).name
        if name.startswith('a_'):
            return [1.0, 0.0, 0.0]
        if name.startswith('b_'):
            return [0.0, 1.0, 0.0]
        if name.startswith('mix_'):
            return [math.sqrt(0.5), math.sqrt(0.5), 0.0]
        return [0.0, 0.0, 1.0]


class VoiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_coordinate_key_uses_one_based_script_coordinates_only(self):
        self.assertEqual(coordinate_key({'stable_audio_id': 'SCRIPT:B000:S001'}), 'SCRIPT:1:2')
        self.assertEqual(coordinate_key({'stable_audio_id': 'SCRIPT:B001:S001',
                                         'bank_num': 1, 'sound_num': 2}), 'SCRIPT:1:2')
        self.assertEqual(coordinate_key({'pak': 'SPC_GA', 'bank_num': 1, 'sound_num': 2}), None)
        self.assertEqual(coordinate_key({'archive': 'SPC_GA',
                                         'stable_audio_id': 'SCRIPT:B000:S000'}), None)
        self.assertIsNone(coordinate_key({'stable_audio_id': 'EVENT:CAR_HORN'}))

    def test_collection_uses_fresh_original_slots_and_links_verified_and_manual_seeds(self):
        audio_root = self.root / 'cache' / 'export' / 'SCRIPT'
        one = make_wav(audio_root / 'Bank_001' / 'sound_001.wav')
        two = make_wav(audio_root / 'Bank_001' / 'sound_002.wav', frequency=300)
        game = FakeGameAudio(audio_root.parent.parent)
        game.indice = {('SCRIPT', 1, 1): one, ('SCRIPT', 1, 2): two}
        class Reviews:
            def character(self, line_id):
                return 'Ryder' if line_id == 'DIALOG|000000a2' else ''
        records = [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'line_id': 'line-1', 'table': 'DIALOG',
             'jamcrc': 'A1', 'source_text': 'Hey, CJ.', 'speaker_identity': 'BIGSMOKE',
             'speaker_identity_status': 'VERIFIED'},
            {'stable_audio_id': 'SCRIPT:B000:S001', 'line_id': 'line-2', 'table': 'DIALOG',
             'jamcrc': 'A2', 'source_text': 'Come on.'},
        ]
        items = collect_original_items(game, records, Reviews())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['audio_id'], 'SCRIPT:B001:S001')
        self.assertEqual(items[0]['known_identity'], 'Big Smoke')
        self.assertEqual(items[1]['known_identity'], 'Ryder')
        self.assertEqual(items[0]['links'][0]['hash'], 'A1')
        self.assertEqual(items[0]['links'][0]['source_text'], 'Hey, CJ.')

    def test_collection_rejects_stale_cache_and_can_filter_by_table_coordinates(self):
        base = self.root / 'cache' / 'export' / 'SCRIPT'
        wav = make_wav(base / 'Bank_001' / 'sound_001.wav')
        game = FakeGameAudio(base.parent.parent, fresh=False)
        game.indice = {('SCRIPT', 1, 1): wav}
        self.assertEqual(collect_original_items(game, []), [])
        game._fresh = True
        selected = collect_original_items(game, [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'A'},
            {'stable_audio_id': 'SCRIPT:B000:S000', 'table': 'B'},
        ], tables={'B'})
        self.assertEqual(len(selected), 1)
        selected = collect_original_items(game, [], tables={'B'})
        self.assertEqual(selected, [])

    def test_collection_derives_table_from_manifest_gxt_stable_id(self):
        base = self.root / 'cache' / 'export' / 'SCRIPT'
        wav = make_wav(base / 'Bank_001' / 'sound_001.wav')
        game = FakeGameAudio(base.parent.parent)
        game.indice = {('SCRIPT', 1, 1): wav}
        record = {'gxt_stable_id': 'INTRO1:00123456', 'jamcrc': '00123456',
                  'stable_audio_id': 'SCRIPT:B000:S000', 'source_text': 'A line'}
        items = collect_original_items(game, [record], tables={'INTRO1'})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['links'][0]['table'], 'INTRO1')
        self.assertEqual(items[0]['links'][0]['line_id'], 'INTRO1|00123456')

    def test_verified_identity_conflicts_are_explicit_and_manual_identity_wins(self):
        base = self.root / 'cache' / 'export' / 'SCRIPT'
        wav = make_wav(base / 'Bank_001' / 'sound_001.wav')
        game = FakeGameAudio(base.parent.parent)
        game.indice = {('SCRIPT', 1, 1): wav}
        records = [
            {'stable_audio_id': 'SCRIPT:B000:S000', 'line_id': 'l1', 'speaker_identity': 'CJ',
             'speaker_identity_status': 'VERIFIED'},
            {'stable_audio_id': 'SCRIPT:B000:S000', 'line_id': 'l2', 'speaker_identity': 'Ryder',
             'speaker_identity_status': 'VERIFIED'},
        ]
        item = collect_original_items(game, records)[0]
        self.assertIsNone(item['known_identity'])
        self.assertEqual(item['evidence']['seed_conflict']['identities'], ['CJ', 'Ryder'])
        class Reviews:
            def character(self, line_id):
                return 'CJ' if line_id == 'l2' else ''
        item = collect_original_items(game, records, Reviews())[0]
        self.assertEqual(item['known_identity'], 'CJ')
        self.assertEqual(item['evidence']['seed_source'], 'manual_review')

    def test_analysis_scores_seeded_references_and_excludes_same_audio_hash(self):
        paths = [make_wav(self.root / f'{name}.wav', frequency=freq) for name, freq in
                 [('a_seed', 220), ('a_copy', 225), ('b_seed', 330), ('a_unknown', 260)]]
        items = [
            {'audio_id': 'SCRIPT:B001:S001', 'path': str(paths[0]), 'known_identity': 'CJ', 'links': []},
            {'audio_id': 'SCRIPT:B001:S002', 'path': str(paths[1]), 'known_identity': None, 'links': []},
            {'audio_id': 'SCRIPT:B001:S003', 'path': str(paths[2]), 'known_identity': 'Ryder', 'links': []},
            {'audio_id': 'SCRIPT:B001:S004', 'path': str(paths[3]), 'known_identity': None, 'links': []},
        ]
        report = analyze_items(items, FakeEmbedder(), VoiceIdentityStore(self.root))
        result = {item['audio_id']: item for item in report['items']}
        self.assertEqual(result['SCRIPT:B001:S002']['suggested_identity'], 'CJ')
        self.assertNotEqual(result['SCRIPT:B001:S001']['content_sha256'], result['SCRIPT:B001:S002']['content_sha256'])
        self.assertEqual(report['model_id'], 'fixture-v1')
        self.assertTrue(report['groups'])
        self.assertEqual(VoiceIdentityStore(self.root).latest()['id'], report['id'])

    def test_ambiguous_and_unknown_items_abstain_and_identical_content_is_deduplicated(self):
        seed_a = make_wav(self.root / 'a_seed.wav', frequency=220)
        seed_b = make_wav(self.root / 'b_seed.wav', frequency=330)
        ambiguous = make_wav(self.root / 'mix_probe.wav', frequency=440)
        first = make_wav(self.root / 'unknown.wav', frequency=500)
        duplicate = self.root / 'unknown_copy.wav'
        duplicate.write_bytes(first.read_bytes())
        items = [
            {'audio_id': 'a', 'path': str(seed_a), 'known_identity': 'CJ', 'links': []},
            {'audio_id': 'b', 'path': str(seed_b), 'known_identity': 'Ryder', 'links': []},
            {'audio_id': 'm', 'path': str(ambiguous), 'links': []},
            {'audio_id': 'u1', 'path': str(first), 'links': []},
            {'audio_id': 'u2', 'path': str(duplicate), 'links': []},
        ]
        report = analyze_items(items, FakeEmbedder(), VoiceIdentityStore(self.root), min_score=.6, min_margin=.08)
        result = {item['audio_id']: item for item in report['items']}
        self.assertEqual(result['m']['status'], 'AMBIGUOUS')
        self.assertIsNone(result['m']['suggested_identity'])
        self.assertEqual(result['u1']['content_sha256'], result['u2']['content_sha256'])
        self.assertEqual(report['stats']['unique_embeddings'], 4)

    def test_short_silent_and_noise_clips_are_unanalysable(self):
        short = make_wav(self.root / 'short.wav', seconds=.4)
        silent = make_wav(self.root / 'silent.wav', amplitude=0)
        noise = make_wav(self.root / 'noise.wav', noise=True)
        items = [{'audio_id': str(i), 'path': str(path), 'links': []} for i, path in enumerate((short, silent, noise))]
        report = analyze_items(items, FakeEmbedder(), VoiceIdentityStore(self.root))
        self.assertEqual({item['status'] for item in report['items']}, {'UNANALYSABLE'})
        self.assertEqual(report['stats']['unanalysable'], 3)

    def test_cancel_does_not_publish_partial_report_but_completed_embeddings_remain_cached(self):
        wavs = [make_wav(self.root / f'{n}.wav') for n in ('a_seed', 'probe')]
        store = VoiceIdentityStore(self.root)
        class Cancel:
            canceled = False
            def is_set(self):
                return self.canceled
        cancel = Cancel()
        targets = [{'audio_id': str(i), 'path': str(p), 'links': []} for i, p in enumerate(wavs)]
        def progress(done, total, message):
            if done == 1:
                cancel.canceled = True
        with self.assertRaises(Exception):
            analyze_items(targets, FakeEmbedder(), store, on_progress=progress, cancel=cancel)
        self.assertIsNone(store.latest())
        report = analyze_items(targets, FakeEmbedder(), store)
        self.assertEqual(report['stats']['embedding_cache_hits'], 1)

    def test_explicit_reference_pool_holds_verified_targets_out_and_scores_them(self):
        reference = make_wav(self.root / 'a_reference.wav', frequency=220)
        held_out_copy = self.root / 'a_holdout.wav'
        held_out_copy.write_bytes(reference.read_bytes())
        probe = make_wav(self.root / 'a_probe.wav', frequency=225)
        targets = [
            {'audio_id': 'SCRIPT:B001:S001', 'path': str(held_out_copy),
             'known_identity': 'CJ', 'links': []},
            {'audio_id': 'SCRIPT:B001:S002', 'path': str(probe),
             'known_identity': 'CJ', 'links': []},
        ]
        references = [{'audio_id': 'SCRIPT:B099:S001', 'path': str(reference),
                       'known_identity': 'CJ', 'links': []}]
        report = analyze_items(targets, FakeEmbedder(), VoiceIdentityStore(self.root),
                               reference_items=references)
        results = {item['audio_id']: item for item in report['items']}
        self.assertEqual(set(results), {'SCRIPT:B001:S001', 'SCRIPT:B001:S002'})
        self.assertEqual(results['SCRIPT:B001:S001']['status'], 'UNKNOWN')
        self.assertEqual(results['SCRIPT:B001:S002']['suggested_identity'], 'CJ')
        self.assertEqual(results['SCRIPT:B001:S002']['known_identity'], 'CJ')
        self.assertEqual(report['stats']['reference_audio_ids'], ['SCRIPT:B099:S001'])

    def test_embedding_cache_invalidates_when_source_content_changes(self):
        wav = make_wav(self.root / 'probe.wav', frequency=220)
        class CountingEmbedder(FakeEmbedder):
            def __init__(self):
                self.calls = 0
            def __call__(self, path):
                self.calls += 1
                return super().__call__(path)
        embedder = CountingEmbedder()
        store = VoiceIdentityStore(self.root)
        item = {'audio_id': 'SCRIPT:B001:S001', 'path': str(wav), 'links': []}
        analyze_items([item], embedder, store)
        analyze_items([item], embedder, store)
        self.assertEqual(embedder.calls, 1)
        wav.write_bytes(wav.read_bytes() + b'changed')
        analyze_items([item], embedder, store)
        self.assertEqual(embedder.calls, 2)

    def test_lookup_without_a_report_candidate_returns_without_hashing_wav(self):
        wav = make_wav(self.root / 'unindexed.wav')
        store = VoiceIdentityStore(self.root)
        with patch('studio.audio.voice_identity._sha256', side_effect=AssertionError('unexpected WAV hash')):
            self.assertIsNone(store.lookup({'stable_audio_id': 'SCRIPT:B000:S000'}, wav))

    def test_confirmation_is_source_guarded_atomic_and_lookup_prefers_confirmed_overlay(self):
        seed = make_wav(self.root / 'a_seed.wav')
        probe = make_wav(self.root / 'a_probe.wav')
        store = VoiceIdentityStore(self.root)
        report = analyze_items([
            {'audio_id': 'SCRIPT:B001:S001', 'path': str(seed), 'known_identity': 'CJ', 'links': []},
            {'audio_id': 'SCRIPT:B001:S002', 'path': str(probe), 'links': []},
        ], FakeEmbedder(), store)
        probe_item = next(item for item in report['items'] if item['audio_id'] == 'SCRIPT:B001:S002')
        group_id = probe_item['group_id']
        self.assertTrue(group_id)
        store.assign_group(report['id'], group_id, 'Big Smoke')
        self.assertEqual(next(group for group in store.latest()['groups'] if group['id'] == group_id)['confirmed_identity'],
                         'Big Smoke')
        found = store.lookup({'stable_audio_id': 'SCRIPT:B000:S001'}, probe)
        self.assertEqual(found['status'], 'VOICE_CONFIRMED')
        self.assertEqual(found['identity'], 'Big Smoke')
        probe.write_bytes(probe.read_bytes() + b'changed')
        self.assertIsNone(store.lookup({'stable_audio_id': 'SCRIPT:B000:S001'}, probe))
        store.latest_path.write_text('{ invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            store.publish({'id': 'bad'})
        self.assertEqual(store.latest_path.read_text(encoding='utf-8'), '{ invalid')


if __name__ == '__main__':
    unittest.main()
