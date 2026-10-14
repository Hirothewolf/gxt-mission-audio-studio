"""Read-only synchronization and per-slot installed-audio evidence."""
import hashlib
import json
import os
import wave
from pathlib import Path

from studio.core.utils import sha256_arquivo
from studio.saat.audio import GameAudio
from studio.ui_qt.audio_sync import (
    ExternalFileWatcher, detect_mod_from_gxt, installed_line_evidence,
)


def wav(path, value=400):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(value.to_bytes(2, 'little', signed=True) * 80)


def game_audio(root):
    game = root / 'game'
    for rel in ('audio/SFX/SCRIPT', 'audio/CONFIG/BankLkup.dat'):
        path = game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'base data')
    (game / 'modloader/Target/text').mkdir(parents=True)
    (game / 'modloader/Target/text/american.gxt').write_bytes(b'gxt')
    return GameAudio(dict(game_dir=str(game), modloader_mod='Target'), lambda: None,
        root / '.cache/saat', root / 'backups', root / 'workspace/modloader_manifest.json')


def test_mod_detection_uses_open_document_and_leaves_settings_untouched(tmp_path):
    ga = game_audio(tmp_path)
    other = ga.modloader_dir() / 'Other/text/american.gxt'
    other.parent.mkdir(parents=True)
    other.write_bytes(b'other')
    assert detect_mod_from_gxt(other, ga) == 'Other'
    assert ga.cfg['modloader_mod'] == 'Target'
    base = ga.game_dir / 'text/american.gxt'
    base.parent.mkdir()
    base.write_bytes(b'base')
    assert detect_mod_from_gxt(base, ga) is None
    outside = tmp_path / 'modloader/Other/text/american.gxt'
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b'outside')
    assert detect_mod_from_gxt(outside, ga) is None
    assert detect_mod_from_gxt(other.parent / 'missing.gxt', ga) is None


def test_watcher_detects_same_size_same_mtime_edits_until_explicit_accept(tmp_path):
    source = tmp_path / 'source.gxt'
    source.write_bytes(b'old!')
    watcher = ExternalFileWatcher()
    watcher.watch('gxt', source)
    assert not watcher.changed('gxt')
    stamp = source.stat()
    source.write_bytes(b'new!')
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert watcher.changed('gxt')
    assert watcher.changed('gxt'), 'pending edits must keep the external change pending'
    watcher.accept('gxt')
    assert not watcher.changed('gxt')
    source.unlink()
    assert watcher.changed('gxt')
    watcher.accept('gxt')
    assert not watcher.changed('gxt')


def test_watcher_separates_review_changes_and_document_changes(tmp_path):
    gxt, review = tmp_path / 'text.gxt', tmp_path / 'review.json'
    gxt.write_bytes(b'gxt')
    watcher = ExternalFileWatcher()
    watcher.watch('gxt', gxt)
    watcher.watch('review', review)
    review.write_text('{"version":1}', encoding='utf-8')
    assert watcher.changed('review')
    assert not watcher.changed('gxt')
    watcher.accept('review')
    assert not watcher.changed('review')


def test_bank_presence_alone_is_not_per_line_install_evidence(tmp_path):
    ga = game_audio(tmp_path)
    archive = ga.modloader_dir() / 'Target/audio/SFX/SCRIPT'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'packed audio')
    evidence = installed_line_evidence({'bank_num': 2, 'sound_num': 3}, tmp_path, ga)
    assert evidence['status'] == 'embedded_unindexed'
    assert not evidence['verified']
    assert evidence['path'] is None
    assert evidence['matches_take'] is None


def test_loose_slot_is_audible_but_only_equal_pcm_matches_selected_take(tmp_path):
    ga = game_audio(tmp_path)
    installed = ga.modloader_dir() / 'Target/audio/SFX/SCRIPT/Bank_002/sound_003.wav'
    wav(installed)
    take = tmp_path / 'take.wav'
    wav(take)
    info = {'bank_num': 2, 'sound_num': 3}
    evidence = installed_line_evidence(info, tmp_path, ga, take_path=take)
    assert evidence['path'] == installed
    assert evidence['verified']
    assert evidence['matches_take'] is True
    wav(take, value=900)
    assert installed_line_evidence(info, tmp_path, ga, take_path=take)['matches_take'] is False


def native_proof(root, ga):
    mod = ga.modloader_dir() / 'Target'
    archive, lookup = mod / 'audio/SFX/SCRIPT', mod / 'audio/CONFIG/BankLkup.dat'
    for path in (archive, lookup):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())
    take, audition = root / 'take.wav', root / '.cache/audition.wav'
    wav(take)
    wav(audition)
    slot = dict(ref=['SCRIPT', 2, 3], source_take_path=str(take),
        source_take_sha256=sha256_arquivo(take), audition_path=str(audition),
        audition_sha256=sha256_arquivo(audition), pcm_signature=list(ga._pcm_signature(audition)))
    bank = dict(pak='SCRIPT', archive=str(archive), archive_sha256=sha256_arquivo(archive),
        banklkup_sha256=sha256_arquivo(lookup), backup=str(root / 'backup'), slots=1, slots_verified=[slot])
    ga.manifest_path.parent.mkdir(parents=True)
    ga.manifest_path.write_text(json.dumps(dict(mods={}, native_archives={'Target': [bank]})))
    return take, audition, archive, lookup, bank


def test_native_per_slot_proof_requires_current_archive_lookup_and_audition(tmp_path):
    ga = game_audio(tmp_path)
    take, audition, archive, lookup, bank = native_proof(tmp_path, ga)
    info = {'bank_num': 2, 'sound_num': 3}
    evidence = installed_line_evidence(info, tmp_path, ga, take_path=take)
    assert evidence['verified']
    assert evidence['matches_take'] is True
    assert evidence['path'] == audition
    other = installed_line_evidence({'bank_num': 2, 'sound_num': 4}, tmp_path, ga, take_path=take)
    assert not other['verified']
    lookup.write_bytes(b'changed lookup')
    assert not installed_line_evidence(info, tmp_path, ga, take_path=take)['verified']
    lookup.write_bytes(b'BankLkup.dat')
    wav(audition, value=1000)
    assert not installed_line_evidence(info, tmp_path, ga, take_path=take)['verified']


def test_native_proof_does_not_present_a_new_take_as_installed(tmp_path):
    ga = game_audio(tmp_path)
    take, audition, *_ = native_proof(tmp_path, ga)
    wav(take, value=1000)
    evidence = installed_line_evidence({'bank_num': 2, 'sound_num': 3}, tmp_path, ga, take_path=take)
    assert evidence['verified']
    assert evidence['path'] == audition
    assert evidence['matches_take'] is False


def test_stream_proof_matches_exact_line_and_selected_source_take(tmp_path):
    ga = game_audio(tmp_path)
    archive = ga.modloader_dir() / 'Target/audio/streams/CUTSCENE'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'packed stream')
    take, audition = tmp_path / 'take.wav', tmp_path / '.cache/installed.wav'
    wav(take)
    wav(audition)
    gid = 'INTRO1:00000001'
    row = dict(container='CUTSCENE', track='Track_001', source_take_path=str(take),
        source_take_sha256=sha256_arquivo(take), audition_path=str(audition),
        audition_sha256=sha256_arquivo(audition), archive_sha256=sha256_arquivo(archive))
    proof = tmp_path / 'workspace/stream_installs' / (hashlib.sha256(b'Target').hexdigest()[:16] + '.json')
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({'lines': {gid: row}}))
    info = dict(pak='CUTSCENE', stream={'track': 'Track_001'}, gxt_stable_id=gid,
        stable_audio_id='CUTSCENE:INTRO1:TEST')
    evidence = installed_line_evidence(info, tmp_path, ga, take_path=take)
    assert evidence['verified']
    assert evidence['path'] == audition
    assert evidence['matches_take'] is True
    wav(take, value=1000)
    assert installed_line_evidence(info, tmp_path, ga, take_path=take)['matches_take'] is False
    archive.write_bytes(b'changed stream')
    assert not installed_line_evidence(info, tmp_path, ga)['verified']


def test_untrusted_native_audition_outside_project_is_not_exposed(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    ga = game_audio(project)
    _, _, _, _, bank = native_proof(project, ga)
    outside = tmp_path / 'outside.wav'
    wav(outside)
    bank['slots_verified'][0].update(audition_path=str(outside), audition_sha256=sha256_arquivo(outside))
    ga.manifest_path.write_text(json.dumps(dict(mods={}, native_archives={'Target': [bank]})))
    evidence = installed_line_evidence({'bank_num': 2, 'sound_num': 3}, project, ga)
    assert not evidence['verified']
    assert evidence['path'] is None
