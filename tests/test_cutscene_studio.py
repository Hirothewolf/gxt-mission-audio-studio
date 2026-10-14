import hashlib
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from studio.audio import cutscene_studio as studio


def write_pcm(path, frames, rate=8000, channels=2):
    import struct
    samples = [sample for frame in frames for sample in frame]
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack('<%dh' % len(samples), *samples))


def read_pcm(path):
    import struct
    with wave.open(str(path), 'rb') as source:
        channels, rate, frames = source.getnchannels(), source.getframerate(), source.readframes(source.getnframes())
    values = struct.unpack('<%dh' % (len(frames) // 2), frames)
    return channels, rate, [values[i:i + channels] for i in range(0, len(values), channels)]


class CutsceneStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.wav'
        self.replacement = self.root / 'replacement.wav'
        self.output = self.root / 'rendered.wav'
        self.source_frames = [(1000 + i, -1000 - i) for i in range(8)]
        write_pcm(self.source, self.source_frames, rate=8)
        write_pcm(self.replacement, [(7000, -7000), (8000, -8000)], rate=8)
        self.track = {
            'source_path': str(self.source), 'source_sha256': studio.digest(self.source),
            'sample_rate': 8, 'channels': 2, 'frames': 8, 'duration': 1.0,
            'pcm': self._source_bytes(), 'archive_sha256': None,
        }
        self.item = studio.import_replacement(self.replacement)

    def tearDown(self):
        self.temp.cleanup()

    def _source_bytes(self):
        return self.source.read_bytes()[44:]

    def test_waveform_and_range_validation_use_frames(self):
        self.assertEqual(studio.waveform_peaks(self.track['pcm'], 2, 4), [
            1001 / 32768, 1003 / 32768, 1005 / 32768, 1007 / 32768,
        ])
        self.assertEqual(studio._frame_bounds(.25, .5, 8, 8), (2, 4))
        with self.assertRaisesRegex(ValueError, 'intervalos'):
            studio.validate_ranges(self.track, [{'start': .1, 'end': .5}, {'start': .4, 'end': .6}])

    def test_render_replaces_only_selected_frames_and_preserves_stereo_duration(self):
        self.assertEqual(shutil.which('ffmpeg') is not None, True, 'FFmpeg must be available for PCM normalization')
        result = studio.render_track(self.track, [self.item], [{'start': .25, 'end': .75}], self.output,
                                     background_mode='preserve_original')
        channels, rate, frames = read_pcm(self.output)
        self.assertEqual((channels, rate, len(frames)), (2, 8, 8))
        self.assertEqual(frames[:2], self.source_frames[:2])
        self.assertEqual(frames[2:4], [(7000, -7000), (8000, -8000)])
        self.assertEqual(frames[4:6], [(0, 0), (0, 0)])
        self.assertEqual(frames[6:], self.source_frames[6:])
        self.assertEqual(result['duration'], 1.0)
        self.assertEqual(result['channels'], 2)

    def test_oversize_requires_explicit_fit_and_fit_keeps_track_length(self):
        long_path = self.root / 'long.wav'
        write_pcm(long_path, [(1, -1)] * 6, rate=8)
        long_item = studio.import_replacement(long_path)
        interval = [{'start': .25, 'end': .5}]
        with self.assertRaisesRegex(ValueError, 'excede'):
            studio.validate_replacements(self.track, [long_item], interval, fit=False)
        studio.render_track(self.track, [long_item], interval, self.output, fit=True)
        channels, rate, frames = read_pcm(self.output)
        self.assertEqual((channels, rate, len(frames)), (2, 8, 8))

    def test_new_mix_defaults_to_only_ptbr_takes_and_silent_background(self):
        result = studio.render_track(self.track, [self.item], [{'start': .25, 'end': .75}], self.output)
        _, _, frames = read_pcm(self.output)
        self.assertEqual(frames, [(0, 0)] * 2 + [(7000, -7000), (8000, -8000)] + [(0, 0)] * 4)
        self.assertEqual(result['background_mode'], 'voice_only')
        self.assertEqual(json.loads(self.output.with_suffix('.mix.json').read_text())['background_mode'], 'voice_only')

    def test_empty_voice_only_mix_is_silent_for_entire_original_duration(self):
        result = studio.render_track(self.track, [], [], self.output, missing_ptbr=[{'key': 'missing'}])
        channels, rate, frames = read_pcm(self.output)
        self.assertEqual((channels, rate, len(frames)), (2, 8, 8))
        self.assertEqual(frames, [(0, 0)] * 8)
        self.assertEqual(result['missing_ptbr'], [{'key': 'missing'}])

    def test_overlap_sums_both_voices_and_silent_tail_does_not_erase_other(self):
        a = self.root / 'voice_a.wav'
        b = self.root / 'voice_b.wav'
        write_pcm(a, [(1000, -1000)] * 4, rate=8)
        write_pcm(b, [(2000, -2000), (0, 0)], rate=8)
        replacements = [studio.import_replacement(a), studio.import_replacement(b)]
        ranges = [{'start': .125, 'end': .625}, {'start': .375, 'end': .75}]
        with self.assertRaisesRegex(ValueError, 'sobrep'):
            studio.render_track(self.track, replacements, ranges, self.output)
        result = studio.render_track(self.track, replacements, ranges, self.output, allow_overlap=True,
                                     background_mode='preserve_original')
        _, _, frames = read_pcm(self.output)
        self.assertEqual(frames, [self.source_frames[0], (1000, -1000), (1000, -1000),
                                 (3000, -3000), (1000, -1000), (0, 0), *self.source_frames[6:]])
        self.assertEqual(result['mix_gain'], 1.0)
        self.assertTrue(result['allow_overlap'])
        self.assertEqual(json.loads(self.output.with_suffix('.mix.json').read_text(encoding='utf-8')), result)

    def test_overlap_peak_protection_only_scales_replaced_union(self):
        a = self.root / 'loud_a.wav'
        b = self.root / 'loud_b.wav'
        write_pcm(a, [(30000, -30000)] * 4, rate=8)
        write_pcm(b, [(30000, -30000)] * 2, rate=8)
        result = studio.render_track(self.track, [studio.import_replacement(a), studio.import_replacement(b)],
            [{'start': .125, 'end': .625}, {'start': .375, 'end': .625}], self.output, allow_overlap=True,
            background_mode='preserve_original')
        _, _, frames = read_pcm(self.output)
        self.assertAlmostEqual(result['mix_gain'], 32767 / 60000)
        self.assertEqual(frames[0], self.source_frames[0])
        self.assertEqual(frames[5:], self.source_frames[5:])
        self.assertEqual(frames[3], (32767, -32767))
        self.assertEqual(frames[1], (16384, -16384))

    def test_source_change_refuses_export(self):
        self.source.write_bytes(self.source.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'alterada'):
            studio.render_track(self.track, [self.item], [{'start': .25, 'end': .75}], self.output)
        self.assertFalse(self.output.exists())

    def test_draft_roundtrip_records_signatures_and_rejects_changed_audio(self):
        project = self.root / 'project'
        archive = self.root / 'CUTSCENE'
        archive.write_bytes(b'archive')
        archive_hash = studio.digest(archive)
        cache_root = project / '.cache' / 'cutscene_original' / archive_hash[:20]
        cache = cache_root / 'CUTSCENE'
        cache.mkdir(parents=True)
        cached_track = cache / 'Track_001.ogg'
        ogg_source = self.root / 'long_source.wav'
        write_pcm(ogg_source, [(100, -100)] * 8000, rate=8000)
        import subprocess
        subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-y', '-i', str(ogg_source),
                        '-c:a', 'libvorbis', str(cached_track)], check=True)
        cached_hash = studio.digest(cached_track)
        (cache_root / 'source.json').write_text(json.dumps({'archive_sha256': archive_hash,
            'files': {'CUTSCENE/Track_001.ogg': {'sha256': cached_hash,
                                                   'bytes': cached_track.stat().st_size}}}), encoding='utf-8')
        draft_track = dict(self.track, source_path=str(cached_track), source_sha256=cached_hash,
                           sample_rate=8000, channels=2, frames=8000, duration=1.0,
                           archive_sha256=archive_hash)
        index = project / 'workspace' / 'native_audio' / 'index.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'sources': {'stream': {'path': str(archive)}}}), encoding='utf-8')
        ref = self.root / 'reference.wav'
        write_pcm(ref, [(1, -1)] * 2000, rate=8000)
        reference = studio.import_replacement(ref)
        gxt = self.root / 'american.gxt'
        gxt.write_bytes(b'gxt')
        snapshot = {'stable_id': 'INTRO1:12345678', 'gxt_path': str(gxt),
                    'original_gxt_path': str(gxt), 'hypertranslated_text': 'fala pt-br'}
        segments = [{'start': .1, 'end': .5, 'gxt': snapshot, 'reference': reference,
              'replacement': self.item, 'reference_transcript': 'texto original',
              'actor_id': 'Officer A', 'gxt_row_id': 'row-a'},
             {'start': .3, 'end': .8, 'reference': reference, 'replacement': self.item,
              'actor_id': 'Officer B', 'reference_transcript': 'another voice'}]
        with self.assertRaisesRegex(ValueError, 'sobrep'):
            studio.save_draft(project, draft_track, segments, [snapshot])
        draft = studio.save_draft(project, draft_track, segments,
            [snapshot], draft_id='1' * 32, reference_row_id='row-authoring', allow_overlap=True)
        report = {'sources': {'stream': {'path': str(archive), 'sha256': archive_hash}}}
        loaded = studio.load_draft(draft, project, report)
        self.assertEqual(loaded['status'], 'DRAFT')
        self.assertEqual(loaded['segments'][0]['reference_transcript'], 'texto original')
        self.assertEqual(loaded['segments'][0]['actor_id'], 'Officer A')
        self.assertEqual(loaded['segments'][0]['gxt_row_id'], 'row-a')
        self.assertEqual(loaded['reference_authoring']['gxt_row_id'], 'row-authoring')
        self.assertEqual(loaded['gxt_rows'][0]['gxt_sha256'], studio.digest(gxt))
        self.assertEqual(loaded['background_mode'], 'voice_only')
        self.assertTrue(loaded['allow_overlap'])
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in loaded['segments']],
                         [(.1, .5), (.3, .8)])
        payload = json.loads(draft.read_text(encoding='utf-8'))
        payload['allow_overlap'] = False
        draft.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sobrep'):
            studio.load_draft(draft, project, report)
        self.assertTrue(studio.load_draft(draft, project, report, allow_overlap=True)['allow_overlap'])
        payload['allow_overlap'] = True
        payload.pop('background_mode')
        draft.write_text(json.dumps(payload), encoding='utf-8')
        self.assertEqual(studio.load_draft(draft, project, report)['background_mode'], 'preserve_original')
        self.replacement.write_bytes(self.replacement.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'WAV replacement'):
            studio.load_draft(draft, project, report)

    def test_draft_identity_cannot_escape_drafts_directory(self):
        project = self.root / 'project'
        with self.assertRaisesRegex(ValueError, 'ID do rascunho'):
            studio.save_draft(project, self.track, [], [], draft_id='..\\..\\outside')

    def test_open_rejects_manifest_identity_or_path_outside_drafts(self):
        project = self.root / 'project'
        archive = self.root / 'CUTSCENE'
        archive.write_bytes(b'archive')
        archive_hash = studio.digest(archive)
        cache_root = project / '.cache' / 'cutscene_original' / archive_hash[:20]
        cache = cache_root / 'CUTSCENE'
        cache.mkdir(parents=True)
        cached_track = cache / 'Track_001.ogg'
        ogg_source = self.root / 'source_for_manifest.wav'
        write_pcm(ogg_source, [(100, -100)] * 8000, rate=8000)
        import subprocess
        subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-y', '-i', str(ogg_source),
                        '-c:a', 'libvorbis', str(cached_track)], check=True)
        cached_hash = studio.digest(cached_track)
        (cache_root / 'source.json').write_text(json.dumps({'archive_sha256': archive_hash,
            'files': {'CUTSCENE/Track_001.ogg': {'sha256': cached_hash}}}), encoding='utf-8')
        index = project / 'workspace' / 'native_audio' / 'index.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'sources': {'stream': {'path': str(archive)}}}), encoding='utf-8')
        track = dict(self.track, source_path=str(cached_track), source_sha256=cached_hash,
                     sample_rate=8000, channels=2, frames=8000, duration=1.0,
                     archive_sha256=archive_hash)
        draft = studio.save_draft(project, track, [], [], draft_id='a' * 32)
        payload = json.loads(draft.read_text(encoding='utf-8'))
        report = {'sources': {'stream': {'path': str(archive), 'sha256': archive_hash}}}
        payload['draft_id'] = '..\\..\\outside'
        draft.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'ID do rascunho'):
            studio.load_draft(draft, project, report)
        payload['draft_id'] = 'a' * 32
        draft.write_text(json.dumps(payload), encoding='utf-8')
        outside = self.root / 'outside.json'
        outside.write_text(draft.read_text(encoding='utf-8'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'pasta permitida'):
            studio.load_draft(outside, project, report)


if __name__ == '__main__':
    unittest.main()
