import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from studio.ui_qt.context import QtContext
from test_qt_audio_sync import wav


def context(tmp_path):
    ctx = QtContext.__new__(QtContext)
    ctx.project_root = tmp_path
    ctx.review_store = SimpleNamespace(approved_path=lambda *args: None)
    return ctx


def test_approved_fallback_requires_exact_text_and_source_hash(tmp_path):
    ctx = context(tmp_path)
    source = tmp_path / 'integrated.wav'
    wav(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    proof = dict(source_take_path=str(source), source_take_sha256=digest,
                 target_text_sha256=hashlib.sha256(b'current text').hexdigest())
    ctx.audio_evidence = lambda *args: dict(state='proveninstalled', proof=proof)
    assert ctx.approved_path('INTRO1|00000001', 'current text') == source
    assert ctx.approved_path('INTRO1|00000001', 'changed text') is None
    wav(source, 900)
    assert ctx.approved_path('INTRO1|00000001', 'current text') is None


def test_bank_presence_does_not_grant_take_approval(tmp_path):
    ctx = context(tmp_path)
    source = tmp_path / 'existing.wav'
    wav(source)
    ctx.audio_evidence = lambda *args: dict(state='available', path=source, proof={})
    assert ctx.approved_path('INTRO1|00000001', 'text') is None


def test_integrated_receipt_requires_output_hash_and_exact_gxt_identity(tmp_path):
    ctx = context(tmp_path)
    source = tmp_path / 'audio.wav'
    wav(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ctx.audio_evidence = lambda *args: dict(state='proveninstalled', proof=dict(
        source_take_path=str(source), source_take_sha256=digest))
    receipt = dict(gxt_id='INTRO1:00000001', output_sha256=digest, pt_target_exact='exact text')
    (tmp_path/'receipt.json').write_text(json.dumps(receipt))
    assert ctx.approved_path('INTRO1|00000001', 'exact text') == source
    assert ctx.approved_path('INTRO1|00000001', '~z~exact text') == source
    assert ctx.approved_path('INTRO1|00000001', '~z~changed text') is None
    receipt['gxt_id'] = 'INTRO1:00000002'
    (tmp_path/'receipt.json').write_text(json.dumps(receipt))
    assert ctx.approved_path('INTRO1|00000001', 'exact text') is None


def test_presence_without_equal_take_does_not_mark_approval_installed(tmp_path, monkeypatch):
    from studio.ui_qt import audio_sync
    ctx = context(tmp_path)
    ctx.current_text = lambda record: 'text'
    ctx.approved_path = lambda *args: None
    ctx.selected_mod = lambda: 'Target'
    ctx.game_audio = None
    ctx.filter_installation_evidence = {'INTRO1|00000001': True}
    monkeypatch.setattr(audio_sync, 'installed_line_evidence', lambda *args, **kwargs:
        dict(verified=True, path=tmp_path/'existing.wav', matches_take=None))
    ctx.record_installed_evidence(dict(table='INTRO1', hash=1), {}, tmp_path/'existing.wav')
    assert ctx.filter_installation_evidence == {}


def test_generation_preflight_indexes_only_missing_native_cache_then_runs_strict_guard(tmp_path, monkeypatch):
    from studio.tts import omnivoice
    ctx = context(tmp_path)
    calls = []
    info = dict(bank_num=2, sound_num=3)
    ctx.game_audio = SimpleNamespace(original_para=lambda info: None,
        ref_para=lambda info: ('SCRIPT', 2, 3), indexar=lambda pak: calls.append(('index', pak)) or {'ok': True})
    monkeypatch.setattr(omnivoice, 'resolve_original_reference', lambda *args:
        calls.append(('guard', args[0])) or dict(path='strict-native.wav'))
    assert ctx._native_generation_reference(info)['path'] == 'strict-native.wav'
    assert calls == [('index', 'SCRIPT'), ('guard', info)]
    calls.clear()
    ctx.game_audio.original_para = lambda info: tmp_path/'existing-native.wav'
    ctx._native_generation_reference(info)
    assert calls == [('guard', info)]
    calls.clear()
    ctx.game_audio.original_para = lambda info: None
    ctx.game_audio.indexar = lambda pak: {'ok': False, 'motivo': 'broken source'}
    import pytest
    with pytest.raises(ValueError, match='broken source'):
        ctx._native_generation_reference(info)
    assert calls == [], 'a failed cache preparation must not bypass the strict guard'
