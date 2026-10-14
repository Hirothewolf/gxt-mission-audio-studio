"""Real slot presence and bounded audition extraction, distinct from approval."""
import struct
import wave
import hashlib
import json

from test_qt_audio_sync import game_audio
from studio.ui_qt.audio_sync import sfx_slot, extract_sfx_slot, installed_line_evidence


def bank_bytes(pieces):
    header = bytearray(4804)
    struct.pack_into('<I', header, 0, len(pieces))
    offset = 0
    for index, piece in enumerate(pieces):
        struct.pack_into('<IIHH', header, 4+index*12, offset, 0xffffffff, 8000, 0)
        offset += len(piece)
    return bytes(header)+b''.join(pieces)


def test_std_bank_exact_slot_presence_and_audition_cache(tmp_path):
    ga = game_audio(tmp_path)
    bank = ga.modloader_dir() / 'Target/audio/SFX/SCRIPT/Bank_002/std.bank'
    bank.parent.mkdir(parents=True)
    bank.write_bytes(bank_bytes([b'\x01\x00'*20, b'\x02\x00'*30]))
    info = {'bank_num': 2, 'sound_num': 2}
    evidence = installed_line_evidence(info, tmp_path, ga)
    assert evidence['state'] == 'needs_extract'
    assert not evidence['verified'] and evidence['matches_take'] is None
    assert not (tmp_path / '.cache/qt_audio_slots').exists()
    path = extract_sfx_slot(info, tmp_path, ga, 'Target')
    with wave.open(str(path)) as stream:
        assert stream.readframes(100) == b'\x02\x00'*30
    assert extract_sfx_slot(info, tmp_path, ga, 'Target') == path
    assert installed_line_evidence(info, tmp_path, ga)['state'] == 'available'
    path.write_bytes(b'tampered cache')
    assert installed_line_evidence(info, tmp_path, ga)['state'] == 'needs_extract'
    assert extract_sfx_slot(info, tmp_path, ga, 'Target') == path
    assert sfx_slot({'bank_num': 2, 'sound_num': 3}, ga, 'Target') is None
    bank.write_bytes(bank_bytes([b'\x01\x00'*20, b'\x03\x00'*30]))
    assert extract_sfx_slot(info, tmp_path, ga, 'Target') != path


def test_packed_archive_lookup_and_one_based_coordinates(tmp_path):
    ga = game_audio(tmp_path)
    mod = ga.modloader_dir() / 'Target'
    archive = mod / 'audio/SFX/SCRIPT'
    archive.parent.mkdir(parents=True)
    first = bank_bytes([b'\x01\x00'*20])
    second = bank_bytes([b'\x02\x00'*30])
    archive.write_bytes(first+second)
    lookup = mod / 'audio/CONFIG/BankLkup.dat'
    lookup.parent.mkdir(parents=True)
    lookup.write_bytes(struct.pack('<B3xII', 3, 0, 40)+struct.pack('<B3xII', 3, len(first), 60))
    info = {'bank_num': 2, 'sound_num': 1}
    slot = sfx_slot(info, ga, 'Target')
    assert slot['offset'] == len(first)+4804
    path = extract_sfx_slot(info, tmp_path, ga, 'Target')
    with wave.open(str(path)) as stream:
        assert stream.readframes(100) == b'\x02\x00'*30
    assert sfx_slot({'bank_num': 3, 'sound_num': 1}, ga, 'Target') is None


def test_malformed_bank_does_not_claim_a_real_slot(tmp_path):
    ga = game_audio(tmp_path)
    bank = ga.modloader_dir() / 'Target/audio/SFX/SCRIPT/Bank_002/std.bank'
    bank.parent.mkdir(parents=True)
    bank.write_bytes(bank_bytes([b'\x00\x00'*20])[:-8])
    # The final slot length is defined by the actual bank payload. An offset
    # outside that payload is invalid rather than an available audio file.
    data = bytearray(bank.read_bytes())
    struct.pack_into('<I', data, 4, 10000)
    bank.write_bytes(data)
    assert sfx_slot({'bank_num': 2, 'sound_num': 1}, ga, 'Target') is None


def test_stream_layer_presence_is_separate_from_audition_and_take_equality(tmp_path):
    ga = game_audio(tmp_path)
    archive = ga.modloader_dir() / 'Target/audio/streams/CUTSCENE'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'current stream')
    gid = 'INTRO1:00000001'
    info = dict(pak='CUTSCENE', stream={'track': 'Track_001'}, gxt_stable_id=gid)
    row = dict(gid=gid, container='CUTSCENE', track='Track_001',
               archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    proof = tmp_path / 'workspace/stream_installs' / (hashlib.sha256(b'Target').hexdigest()[:16]+'.json')
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({'layers': {'CUTSCENE/Track_001/'+gid: row}}))
    evidence = installed_line_evidence(info, tmp_path, ga)
    assert evidence['state'] == 'proveninstalled'
    assert evidence['path'] is None and evidence['matches_take'] is None
    assert not evidence['verified'], 'per-line live proof is distinct from a verified audition WAV'
    archive.write_bytes(b'externally changed stream')
    assert installed_line_evidence(info, tmp_path, ga)['state'] == 'stale'


def test_stream_audition_reads_current_track_exact_window_and_refuses_bank_race(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf
    from studio.ui_qt.audio_sync import extract_stream_line
    from studio.ui_qt import scene_sources
    ga = game_audio(tmp_path)
    archive = ga.modloader_dir() / 'Target/audio/streams/CUTSCENE'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'current stream')
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    actual = tmp_path/'actual_track.wav'
    pcm = np.arange(8000, dtype=np.int16)
    sf.write(actual, pcm, 8000, subtype='PCM_16')
    gid = 'INTRO1:00000001'
    info = dict(pak='CUTSCENE', stream={'track': 'Track_001'}, gxt_stable_id=gid)
    row = dict(gid=gid, container='CUTSCENE', track='Track_001', start=.1, end=.2,
               archive_sha256=archive_sha)
    proof = tmp_path / 'workspace/stream_installs' / (hashlib.sha256(b'Target').hexdigest()[:16]+'.json')
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({'layers': {'CUTSCENE/Track_001/'+gid: row}}))
    def full_track(context, container, track, installed=False, cancel=None):
        assert installed and context.game_audio.mod_ativo() == 'Target'
        return dict(source_path=str(actual), source_sha256=hashlib.sha256(actual.read_bytes()).hexdigest(),
                    archive_path=str(archive), archive_sha256=archive_sha)
    monkeypatch.setattr(scene_sources, 'full_track', full_track)
    audition = extract_stream_line(info, tmp_path, ga, 'Target')
    saved, rate = sf.read(audition, dtype='int16')
    assert rate == 8000 and np.array_equal(saved, pcm[800:1600])
    assert installed_line_evidence(info, tmp_path, ga)['path'] == audition
    # Different uncached window ensures the source is re-opened. Bank changes
    # during that read must prevent publication of an old-version audition.
    row['start'] = .2
    row['end'] = .3
    proof.write_text(json.dumps({'layers': {'CUTSCENE/Track_001/'+gid: row}}))
    def race(*args, **kwargs):
        source = full_track(*args, **kwargs)
        archive.write_bytes(b'changed during decode')
        return source
    monkeypatch.setattr(scene_sources, 'full_track', race)
    import pytest
    with pytest.raises(ValueError, match='banco mudou'):
        extract_stream_line(info, tmp_path, ga, 'Target')
