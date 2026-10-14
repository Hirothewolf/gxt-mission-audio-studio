"""Qt engine controls use local files and mock configuration, never synthesis."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from studio import config
from studio.ui_qt.model_settings import (
    GenerationSettingsWidget, ModelManagerDialog, discover_engines, local_models,
)

app = QApplication.instance() or QApplication([])


def configured(tmp_path):
    python = tmp_path / 'python.exe'
    python.touch()
    scripts = {}
    for engine in ('qwen', 'chatterbox'):
        path = tmp_path / (engine + '.py')
        path.touch()
        scripts[engine] = {'python': str(python), 'script': str(path)}
    scripts['omnivoice'] = {'python': str(python)}
    return {'engine_padrao': 'OmniVoice', 'tts_engines': scripts}


def test_registry_inventory_keeps_omnivoice_and_explains_unsupported_engines(tmp_path):
    with patch.object(config, 'CFG', configured(tmp_path)):
        engines = discover_engines(tmp_path)
    assert engines[0].id == 'omnivoice'
    assert engines[0].runtime_available
    assert engines[0].supported
    f5 = next(engine for engine in engines if engine.id == 'f5')
    assert not f5.supported
    assert 'pt_BR' in f5.detail
    assert not next(engine for engine in engines if engine.id == 'piper').supported


def test_omnivoice_controls_use_migrated_timing_and_validate_seed(tmp_path):
    cfg = configured(tmp_path)
    cfg.update(omnivoice_speed='1.15', omnivoice_duration='2')
    with patch.object(config, 'CFG', cfg):
        panel = GenerationSettingsWidget(SimpleNamespace(project_root=tmp_path))
        assert panel.request_options() == dict(engine='OmniVoice', speed=1.0,
                                               duration='livre', seed=None)
        panel.speed.setValue(.95)
        panel.duration.setText('livre')
        panel.seed.setText('7')
        assert panel.request_options() == dict(engine='OmniVoice', speed=.95,
                                               duration='livre', seed=7)
        panel.seed.setText('-1')
        with pytest.raises(ValueError, match='Seed'):
            panel.request_options()
        panel.close()


def test_engine_selection_exposes_only_effective_parameters_and_remembers_values(tmp_path):
    with patch.object(config, 'CFG', configured(tmp_path)):
        panel = GenerationSettingsWidget(SimpleNamespace(project_root=tmp_path))
        panel.engine.setCurrentIndex(panel.engine.findData('qwen'))
        panel.max_speedup.setValue(1.25)
        assert panel.request_options() == dict(engine='Qwen3-TTS', max_speedup=1.25)
        assert panel.speed.isHidden()
        assert panel.exaggeration.isHidden()
        panel.engine.setCurrentIndex(panel.engine.findData('chatterbox'))
        panel.max_speedup.setValue(1.5)
        panel.exaggeration.setValue(.6)
        assert panel.request_options() == dict(engine='Chatterbox', max_speedup=1.5,
                                               exaggeration=.6)
        assert not panel.exaggeration.isHidden()
        panel.engine.setCurrentIndex(panel.engine.findData('qwen'))
        assert panel.max_speedup.value() == 1.25
        panel.close()


def test_unsupported_selection_cannot_produce_generation_request(tmp_path):
    with patch.object(config, 'CFG', configured(tmp_path)):
        panel = GenerationSettingsWidget(SimpleNamespace(project_root=tmp_path))
        panel.engine.setCurrentIndex(panel.engine.findData('f5'))
        with pytest.raises(ValueError, match='F5'):
            panel.request_options()
        panel.close()


def test_model_manager_persists_real_adapter_paths_and_defaults_without_synthesis(tmp_path):
    cfg = configured(tmp_path)
    old = copy.deepcopy(cfg)
    context = SimpleNamespace(project_root=tmp_path)
    with patch.object(config, 'CFG', cfg), patch.object(config, 'cfg_save') as save:
        dialog = ModelManagerDialog(context)
        assert cfg == old  # opening the manager never writes configuration
        dialog.settings.engine.setCurrentIndex(dialog.settings.engine.findData('chatterbox'))
        dialog.settings.max_speedup.setValue(1.35)
        dialog.settings.exaggeration.setValue(.7)
        dialog.default_engine.setChecked(True)
        assert dialog.save_settings()
        assert cfg['engine_padrao'] == 'Chatterbox'
        assert cfg['qt_generation_settings']['chatterbox'] == dict(max_speedup=1.35, exaggeration=.7)
        save.assert_called_once()
        dialog.close()


def test_inventory_reads_omnivoice_pin_without_importing_runner(tmp_path):
    runner = tmp_path / 'deliverables/omnivoice_opening_pacing_v2/run_omnivoice_opening_pacing_v2.py'
    runner.parent.mkdir(parents=True)
    runner.write_text('from pathlib import Path\nraise RuntimeError("never import")\n'
                      'ROOT = Path(__file__).resolve().parents[2]\n'
                      'MODEL_CACHE = ROOT / "cache"\n'
                      'MODEL_SOURCE = ROOT / "source"\n'
                      'MODEL_ID = "k2-fsa/OmniVoice"\nMODEL_REVISION = "test-pin"\n', encoding='utf-8')
    snapshot = tmp_path / 'cache/models--k2-fsa--OmniVoice/snapshots/test-pin'
    snapshot.mkdir(parents=True)
    (snapshot / 'model.safetensors').touch()
    (snapshot / 'audio_tokenizer').mkdir()
    (snapshot / 'audio_tokenizer/model.safetensors').touch()
    with patch.object(config, 'CFG', {}):
        model = local_models(tmp_path)[0]
    assert model.name == 'OmniVoice · k2-fsa/OmniVoice'
    assert model.path == snapshot
    assert model.present
    assert 'test-pin' in model.detail


def test_saved_previous_defaults_migrate_but_new_explicit_choices_survive(tmp_path):
    cfg = configured(tmp_path)
    cfg.update(omnivoice_timing_policy_version=2, omnivoice_seed=20261009,
        qt_generation_settings={'omnivoice': {'duration': 'original', 'seed': 20261009, 'speed': 1.0}})
    with patch.object(config, 'CFG', cfg):
        panel = GenerationSettingsWidget(SimpleNamespace(project_root=tmp_path))
        assert panel.request_options() == dict(engine='OmniVoice', speed=1.0, duration='livre', seed=None)
        panel.duration.setText('original')
        panel.seed.setText('20261009')
        panel.engine.setCurrentIndex(panel.engine.findData('qwen'))
        panel.engine.setCurrentIndex(panel.engine.findData('omnivoice'))
        assert panel.request_options()['duration'] == 'original'
        assert panel.request_options()['seed'] == 20261009
        panel.close()
    cfg['omnivoice_timing_policy_version'] = 3
    with patch.object(config, 'CFG', cfg):
        panel = GenerationSettingsWidget(SimpleNamespace(project_root=tmp_path))
        assert panel.request_options()['duration'] == 'original'
        assert panel.request_options()['seed'] == 20261009
        panel.close()
