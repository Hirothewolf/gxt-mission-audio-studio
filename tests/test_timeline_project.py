import copy
import json
import threading
import wave

import numpy as np
import pytest

from studio.audio.timeline_project import TimelineProject, digest, read_pcm


def wav(path, rate=24000, count=240, value=1000, channels=1):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.full((count, channels), value, dtype='<i2').tobytes())
    return path


def test_overlap_source_policy_and_neighbor_samples(tmp_path):
    source = wav(tmp_path / 'english.wav', count=1000, value=10000)
    voice = wav(tmp_path / 'pt.wav', count=100, value=2000)
    project = TimelineProject(24000, 1)
    original = project.add_wav(source, role='original')
    a = project.add_wav(voice, position=100)
    b = project.add_wav(voice, position=150)
    original_hash, voice_hash = digest(source), digest(voice)
    # Even explicit unmute/solo of original cannot leak English into the PT render.
    project.update_track(project.clip(original)['track_id'], mute=False, solo=True)
    result = project.render(tmp_path / 'mixed.wav')
    samples, rate = read_pcm(tmp_path / 'mixed.wav')
    pcm = np.rint(samples[:, 0] * 32768).astype(int)
    assert rate == 24000 and result['frames'] == 1000 and result['mixed_clips'] == 2
    assert np.all(pcm[:100] == 0)
    assert np.all(pcm[100:150] == 2000)
    assert np.all(pcm[150:200] == 4000)
    assert np.all(pcm[200:250] == 2000)
    assert np.all(pcm[250:] == 0)
    assert digest(source) == original_hash and digest(voice) == voice_hash
    comparison = project.render(tmp_path / 'comparison.wav', include_comparison=True)
    assert comparison['mixed_clips'] == 1


def test_split_trim_move_history_and_roundtrip(tmp_path):
    source = wav(tmp_path / 'voice.wav', count=500)
    project = TimelineProject(24000, 1)
    clip = project.add_wav(source, metadata={'text': 'Olá', 'character': 'CJ', 'certainty': 'manual',
        'reference_interval': [10, 20], 'subtitle_window': [8, 12], 'take': 'v2', 'tts': {'speed': 1.0}})
    before = copy.deepcopy(project.data)
    right = project.split(clip, 200)
    split_state = copy.deepcopy(project.data)
    assert project.clip(clip)['source_out'] == 200 and project.clip(right)['source_in'] == 200
    project.update_clip(right, source_in=250, source_out=450, position=300, gain=.5, fade_in=10, fade_out=20)
    edited = copy.deepcopy(project.data)
    project.undo()
    assert project.data == split_state
    project.undo()
    assert project.data == before
    project.redo()
    project.redo()
    assert project.data == edited
    project.save(tmp_path / 'draft.json')
    assert not project.dirty
    restored = TimelineProject.load(tmp_path / 'draft.json')
    assert restored.data == edited
    project.render(tmp_path / 'a.wav')
    restored.render(tmp_path / 'b.wav')
    assert (tmp_path / 'a.wav').read_bytes() == (tmp_path / 'b.wav').read_bytes()


def test_native_rates_stereo_fades_gain_and_source_restore(tmp_path):
    voice = wav(tmp_path / 'voice.wav', rate=22050, count=2205, value=4000)
    project = TimelineProject(44100, 2)
    clip = project.add_wav(voice)
    assert project.clip_frames(project.clip(clip)) == 4410
    project.update_clip(clip, fade_in=100, fade_out=100, gain=.5)
    result = project.render(tmp_path / 'preview.wav')
    samples, rate = read_pcm(result['path'])
    assert rate == 44100 and samples.shape == (4410, 2)
    assert np.all(samples[0] == 0) and np.all(samples[-1] == 0)
    assert np.all(samples[100:4000] == 2000 / 32768)
    project.update_clip(clip, source_in=100, source_out=1000)
    project.update_clip(clip, source_in=0, source_out=2205)
    assert project.clip(clip)['source_out'] == 2205


def test_invalid_operations_rollback_and_locked_source(tmp_path):
    path = wav(tmp_path / 'source.wav')
    project = TimelineProject()
    original = project.add_wav(path, role='original', native_format=True)
    before = copy.deepcopy(project.data)
    for action in [lambda: project.update_clip(original, position=10), lambda: project.delete(original), lambda: project.split(original, 10)]:
        with pytest.raises(ValueError):
            action()
        assert project.data == before
    track = project.clip(original)['track_id']
    project.update_track(track, locked=False)
    before = copy.deepcopy(project.data)
    with pytest.raises(ValueError):
        project.update_clip(original, source_out=999999)
    assert project.data == before
    project.undo()
    project.undo()
    assert project.data['sample_rate'] == 48000 and not project.data['clips']


def test_source_tamper_schema_cancel_atomic_and_protected_destination(tmp_path):
    path = wav(tmp_path / 'source.wav')
    project = TimelineProject()
    project.add_wav(path)
    draft = tmp_path / 'draft.json'
    project.save(draft)
    with pytest.raises(ValueError):
        project.render(path)
    with pytest.raises(ValueError):
        project.save(path)
    cancel = threading.Event()
    cancel.set()
    destination = tmp_path / 'existing.wav'
    destination.write_bytes(b'preserve')
    with pytest.raises(InterruptedError):
        project.render(destination, cancel_event=cancel)
    assert destination.read_bytes() == b'preserve'
    wav(path, value=1001)
    with pytest.raises(ValueError, match='Fonte'):
        TimelineProject.load(draft)
    with pytest.raises(ValueError, match='Fonte'):
        project.render(destination)
    invalid = json.loads(draft.read_text())
    invalid['clips'][0]['position'] = -1
    draft.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        TimelineProject.load(draft, verify_sources=False)


def test_duplicate_and_track_mute_solo(tmp_path):
    voice = wav(tmp_path / 'voice.wav')
    project = TimelineProject(24000, 1)
    clip = project.add_wav(voice)
    second_track = project.add_track('Outra voz')
    second = project.duplicate(clip, 20, second_track)
    project.update_track(second_track, solo=True, gain=2)
    result = project.render(tmp_path / 'solo.wav')
    assert result['mixed_clips'] == 1
    assert len(project.data['sources']) == 1
    project.update_track(second_track, mute=True)
    assert project.render(tmp_path / 'silent.wav')['mixed_clips'] == 0
    project.delete(second)
    project.undo()
    assert project.clip(second)['position'] == 20


def test_split_preserves_resampling_phase_and_fade_envelope(tmp_path):
    voice = tmp_path / 'voice.wav'
    with wave.open(str(voice), 'wb') as output:
        output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
        output.writeframes((np.sin(np.arange(22050) * .1) * 8000).astype('<i2').tobytes())
    project = TimelineProject(24000, 1)
    clip = project.add_wav(voice)
    project.update_clip(clip, fade_in=5000, fade_out=5000)
    project.render(tmp_path / 'before.wav')
    right = project.split(clip, 1234)
    project.split(right, 23001)
    project.render(tmp_path / 'after.wav')
    assert (tmp_path / 'before.wav').read_bytes() == (tmp_path / 'after.wav').read_bytes()
    project.save(tmp_path / 'project.json')
    restored = TimelineProject.load(tmp_path / 'project.json')
    restored.render(tmp_path / 'restored.wav')
    assert (tmp_path / 'after.wav').read_bytes() == (tmp_path / 'restored.wav').read_bytes()


def test_cancel_during_mix_keeps_previous_destination(tmp_path):
    source = wav(tmp_path / 'voice.wav')
    project = TimelineProject()
    project.add_wav(source)
    project.add_wav(source, position=100)
    output = tmp_path / 'render.wav'
    output.write_bytes(b'previous render')
    cancel = threading.Event()
    def progress(done, total, message):
        cancel.set()
    with pytest.raises(InterruptedError):
        project.render(output, cancel_event=cancel, on_progress=progress)
    assert output.read_bytes() == b'previous render'
    assert not list(tmp_path.glob('.timeline_*'))
