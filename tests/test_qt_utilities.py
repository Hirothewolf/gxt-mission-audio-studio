import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from studio import config
from studio.audio.resolver import BaseAudioResolver
from studio.ui_qt.utilities import create_utility_dialog


APP = QApplication.instance() or QApplication([])


def _wait_worker(dialog, timeout=5000):
    worker = dialog._worker
    if not worker or not worker.isRunning():
        return
    loop = QEventLoop()
    worker.result.connect(lambda *_: loop.quit())
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()
    APP.processEvents()
    assert dialog._worker is None


class _Game:
    game_dir = None

    def available_archives(self, target):
        return ['SCRIPT'] if target == 'original' else []

    def mod_ativo(self):
        return 'PTBR_Dublagem'

    def saat(self):
        return {'sfx': None, 'stream': None}


def _context(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        game_audio=_Game(),
        base_audio=BaseAudioResolver(),
        _resolver_ready=False,
        document=SimpleNamespace(gxt=None),
        script_db={},
    )


def test_factory_creates_native_dialogs_and_rejects_unknown_key(tmp_path):
    context = _context(tmp_path)
    for key in ('audio_folders', 'diagnostics', 'archive_extract', 'saat'):
        dialog = create_utility_dialog(key, context)
        assert dialog.isModal() is False
        _wait_worker(dialog)
        dialog.close()
    try:
        create_utility_dialog('unknown', context)
    except ValueError as exc:
        assert 'desconhecida' in str(exc)
    else:
        raise AssertionError('unknown utility key should fail clearly')


def test_audio_folder_dialog_persists_roots_and_indexes_temp_wavs(tmp_path, monkeypatch):
    extra = tmp_path / 'wav bank'
    extra.mkdir()
    (extra / 'clip.wav').write_bytes(b'test')
    saved = {}
    monkeypatch.setattr(config, 'CFG', {'audio_search_roots': []})
    monkeypatch.setattr(config, 'cfg_save', lambda: saved.update(config.CFG))
    context = _context(tmp_path)
    monkeypatch.setattr(context.base_audio, '_save_cache_best_effort', lambda: None)
    dialog = create_utility_dialog('audio_folders', context)
    dialog.roots.append(str(extra.resolve()))
    dialog._render()
    assert dialog.list.count() == 1
    dialog.save_and_scan()
    assert dialog._worker is not None
    _wait_worker(dialog)
    assert saved['audio_search_roots'] == [str(extra.resolve())]
    assert context._resolver_ready
    assert context.base_audio.roots == [extra.resolve()]
    assert len(context.base_audio.records) == 1
    dialog.close()


def test_archive_dialog_only_enables_archives_available_from_real_backend(tmp_path):
    dialog = create_utility_dialog('archive_extract', _context(tmp_path))
    assert dialog.paks['SCRIPT'].isEnabled()
    assert dialog.paks['SCRIPT'].isChecked()
    assert not dialog.paks['SPC_GA'].isEnabled()
    dialog.apply_preset('npcs')
    assert not dialog.paks['SCRIPT'].isChecked()
    assert not dialog.paks['SPC_GA'].isChecked()
    dialog.close()


def test_native_sources_loads_index_and_shows_exact_audio_reference(tmp_path, monkeypatch):
    from studio.script import native_index

    report = {
        'generated_at': 'test snapshot',
        'classifications': [{
            'gxt_stable_id': 'MAIN:00000001', 'table': 'MAIN', 'names': ['WELCOME'],
            'category': 'unknown', 'source_text': 'Welcome back', 'uses': [],
        }],
        'records': [{
            'gxt_stable_id': 'MAIN:00000001', 'pak': 'SCRIPT',
            'stable_audio_id': 'SCRIPT:B000:S012', 'evidence_method': 'SCM_LITERAL_GAME_EVENT_GXT_HASH',
            'source_wav_path': 'audio/SFX/SCRIPT/Bank_001/sound_013.wav',
        }],
    }
    monkeypatch.setattr(native_index, 'load_current', lambda *_: report)
    dialog = create_utility_dialog('native_sources', _context(tmp_path))
    _wait_worker(dialog)
    assert dialog.table.rowCount() == 1
    dialog.table.setCurrentCell(0, 0)
    assert 'SCRIPT:B000:S012' in dialog.details.toPlainText()
    assert 'Bank_001/sound_013.wav' in dialog.details.toPlainText()
    dialog.close()


def test_saat_dialog_saves_paths_without_running_export(tmp_path, monkeypatch):
    cfg = {}
    monkeypatch.setattr(config, 'CFG', cfg)
    saved = []
    monkeypatch.setattr(config, 'cfg_save', lambda: saved.append(dict(cfg)))
    dialog = create_utility_dialog('saat', _context(tmp_path))
    dialog.sfx.setText(str(tmp_path / 'saat_sfx.exe'))
    dialog.stream.setText(str(tmp_path / 'saat_stream.exe'))
    dialog.save()
    assert cfg['saat_sfx'].endswith('saat_sfx.exe')
    assert cfg['saat_stream'].endswith('saat_stream.exe')
    assert saved
    dialog.close()


def test_diagnostics_is_read_only_and_contains_context_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CFG', {'audio_search_roots': []})
    context = _context(tmp_path)
    dialog = create_utility_dialog('diagnostics', context)
    _wait_worker(dialog)
    text = dialog.text.toPlainText()
    assert str(tmp_path) in text
    assert 'WAVs no índice carregado: 0' in text
    assert not list(tmp_path.iterdir())
    dialog.close()
