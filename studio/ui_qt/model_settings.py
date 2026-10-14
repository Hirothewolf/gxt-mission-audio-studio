"""Local Qt model inventory and controls backed by the installed CLI adapters.

Inspecting this module never imports a model runtime or downloads weights. A
present checkpoint is reported separately from the availability of its adapter.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QFormLayout, QComboBox, QDoubleSpinBox, QLineEdit, QLabel, QPushButton,
    QCheckBox, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView)

from studio import config
from studio.tts import registry
from studio.tts.timing import initial_timing_settings, resolve_duration


@dataclass(frozen=True)
class EngineInfo:
    id: str
    name: str
    python: Path | None
    script: Path
    supported: bool
    runtime_available: bool
    detail: str


@dataclass(frozen=True)
class LocalModel:
    name: str
    path: Path
    present: bool
    detail: str


def discover_engines(project_root):
    """Use the command registry, including OmniVoice, rather than Tk's catalog."""
    root = Path(project_root).resolve()
    result = []
    entries = sorted(registry.ENGINES, key=lambda item: item[1] != 'omnivoice')
    for name, engine_id, _, _ in entries:
        _, python, script = registry._paths_for(engine_id, root)
        supported = engine_id in registry._SUPPORTED
        available = bool(python and python.is_file() and script.is_file())
        if not supported:
            detail = registry._UNSUPPORTED_REASONS.get(engine_id,
                'Sem adapter de geração validado neste editor; pesos locais não habilitam geração.')
        elif available:
            detail = 'Python e script presentes; geração e qualidade do modelo ainda exigem execução.'
        else:
            missing = []
            if not python or not python.is_file():
                missing.append('Python')
            if not script.is_file():
                missing.append('script')
            detail = 'Runtime incompleto: %s.' % ', '.join(missing)
        result.append(EngineInfo(engine_id, name, python, script, supported, available, detail))
    return result


def _omnivoice_pin(root):
    """Read simple pinned constants without executing the GPU runtime's source."""
    runner = root / 'deliverables/omnivoice_opening_pacing_v2/run_omnivoice_opening_pacing_v2.py'
    values = {'ROOT': root}
    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = literal(node.left), literal(node.right)
            if isinstance(left, (str, Path)) and isinstance(right, str):
                return Path(left) / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Path':
            if len(node.args) == 1 and not node.keywords:
                value = literal(node.args[0])
                if isinstance(value, str):
                    return Path(value)
        return None
    try:
        tree = ast.parse(runner.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                key = node.targets[0].id
                if key in {'MODEL_CACHE', 'MODEL_SOURCE', 'MODEL_ID', 'MODEL_REVISION'}:
                    value = literal(node.value)
                    if value is not None:
                        values[key] = value
    except (OSError, SyntaxError, UnicodeError):
        pass
    return runner, values


def local_models(project_root):
    """List exact pinned OmniVoice files and declared local weight files."""
    from studio.tts.downloader import CATALOGO_MODELOS, _QWEN_LEGACY_FOLDERS
    root = Path(project_root).resolve()
    runner, pin = _omnivoice_pin(root)
    cache = Path(pin.get('MODEL_CACHE', root / 'deliverables/omnivoice_gta_four_actor_preview_v1/hf_home/hub'))
    revision = pin.get('MODEL_REVISION')
    snapshots = cache / 'models--k2-fsa--OmniVoice/snapshots'
    model_dir = snapshots / revision if isinstance(revision, str) else snapshots
    files = (model_dir / 'model.safetensors', model_dir / 'audio_tokenizer/model.safetensors')
    present = bool(revision and all(path.is_file() for path in files))
    detail = ('Pin: %s. Presença de arquivos; hashes são conferidos pelo worker.' % revision
              if revision else 'Runner local ou revisão fixada ausente; snapshot ainda não identificado.')
    source = pin.get('MODEL_SOURCE')
    if source and not Path(source).is_dir():
        detail += ' Source local ausente.'
    result = [LocalModel('OmniVoice · %s' % pin.get('MODEL_ID', 'k2-fsa/OmniVoice'),
                         model_dir, present, detail)]
    for model_id, model in CATALOGO_MODELOS.items():
        folder = root / 'tools/models' / model.get('subpasta', model_id)
        names = [item['nome'] for item in model.get('arquivos', [])]
        def complete(path):
            return bool(names) and all((path / name).is_file() and
                                      (path / name).stat().st_size > 0 for name in names)
        installed = complete(folder)
        legacy = _QWEN_LEGACY_FOLDERS.get(model_id)
        if not installed and legacy and complete(root / 'models' / legacy):
            folder, installed = root / 'models' / legacy, True
        detail = 'Arquivos declarados presentes; não prova de runtime.' if installed else 'Arquivos declarados incompletos/ausentes.'
        if model_id.startswith('qwen'):
            detail += ' O CLI legado fixa o checkpoint; este catálogo não troca pesos de geração.'
        elif model_id.startswith('piper') or model_id == 'f5tts_base':
            detail += ' Sem adapter de geração validado neste editor.'
        result.append(LocalModel(model['nome'], folder, installed, detail))
    return result


class GenerationSettingsWidget(QWidget):
    """Compact per-engine parameters, with no inert checkpoint/seed controls."""
    engineChanged = Signal(str)

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self._current = None
        self._values = copy.deepcopy(config.CFG.get('qt_generation_settings', {}))
        if not isinstance(self._values, dict):
            self._values = {}
        # Migrate persisted implicit defaults once, before any edits this session.
        saved = self._values.get('omnivoice')
        version = config.CFG.get('omnivoice_timing_policy_version')
        if isinstance(saved, dict) and version != 3:
            if saved.get('seed') in (20261009, '20261009'):
                saved['seed'] = None
            if version == 2 and saved.get('duration') == 'original':
                saved['duration'] = 'livre'
            elif version not in (2, 3):
                saved['speed'], saved['duration'] = initial_timing_settings(config.CFG)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.form = QFormLayout()
        self.engine = QComboBox()
        self.form.addRow('Modelo / engine', self.engine)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 2.)
        self.speed.setSingleStep(.05)
        self.duration = QLineEdit()
        self.duration.setPlaceholderText('original / livre / segundos')
        self.seed = QLineEdit()
        self.seed.setPlaceholderText('Vazio = aleatória a cada geração')
        self.max_speedup = QDoubleSpinBox()
        self.max_speedup.setRange(.01, 10.)
        self.max_speedup.setSingleStep(.05)
        self.max_speedup.setToolTip('Limite --max-speedup do CLI legado; não é a velocidade nativa do OmniVoice.')
        self.exaggeration = QDoubleSpinBox()
        self.exaggeration.setRange(0., 1.)
        self.exaggeration.setSingleStep(.05)
        self.form.addRow('Velocidade nativa', self.speed)
        self.form.addRow('Duração (original / livre / s)', self.duration)
        self.form.addRow('Seed (0 a 2147483647)', self.seed)
        self.form.addRow('Limite de aceleração', self.max_speedup)
        self.form.addRow('Expressividade', self.exaggeration)
        layout.addLayout(self.form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.engine.currentIndexChanged.connect(self._engine_changed)
        self.refresh_engines()

    def refresh_engines(self):
        selected = self.engine.currentData()
        if selected is None:
            configured = str(config.CFG.get('engine_padrao', 'OmniVoice')).casefold()
            selected = next((engine_id for name, engine_id, _, _ in registry.ENGINES
                             if configured in (engine_id, name.casefold())), 'omnivoice')
        self.engines = discover_engines(self.context.project_root)
        self.engine.blockSignals(True)
        self.engine.clear()
        for info in self.engines:
            suffix = 'sem adapter' if not info.supported else ('runtime local' if info.runtime_available else 'runtime ausente')
            self.engine.addItem('%s · %s' % (info.name, suffix), info.id)
            self.engine.setItemData(self.engine.count() - 1, info.detail, 3)  # ToolTipRole
        self.engine.setCurrentIndex(max(0, self.engine.findData(selected)))
        self.engine.blockSignals(False)
        self._engine_changed()

    def selected_engine(self):
        return next(info for info in self.engines if info.id == self.engine.currentData())

    def _remember(self):
        if self._current == 'omnivoice':
            self._values[self._current] = dict(speed=self.speed.value(), duration=self.duration.text(), seed=self.seed.text())
        elif self._current in ('qwen', 'chatterbox'):
            values = dict(max_speedup=self.max_speedup.value())
            if self._current == 'chatterbox':
                values['exaggeration'] = self.exaggeration.value()
            self._values[self._current] = values

    def _engine_changed(self, *_args):
        self._remember()
        info = self.selected_engine()
        self._current = info.id
        values = self._values.get(info.id, {})
        if not isinstance(values, dict):
            values = {}
        omni = info.id == 'omnivoice'
        for row in (1, 2, 3):
            self.form.setRowVisible(row, omni)
        self.form.setRowVisible(4, info.id in ('qwen', 'chatterbox'))
        self.form.setRowVisible(5, info.id == 'chatterbox')
        if omni:
            speed, duration = initial_timing_settings(config.CFG)
            self.speed.setValue(float(values.get('speed', speed)))
            self.duration.setText(str(values.get('duration', duration)))
            default_seed = config.CFG.get('omnivoice_seed')
            if config.CFG.get('omnivoice_timing_policy_version') != 3 and default_seed == 20261009:
                default_seed = None
            seed = values.get('seed', default_seed)
            self.seed.setText('' if seed is None else str(seed))
            policy = 'Referência original + transcrição. Passos: 32; idioma: pt; políticas de pré/pós-processamento fixas no adapter.'
        elif info.id in ('qwen', 'chatterbox'):
            self.max_speedup.setValue(float(values.get('max_speedup', 1.0 if info.id == 'qwen' else 1.5)))
            self.exaggeration.setValue(float(values.get('exaggeration', .5)))
            policy = 'CLI legado: checkpoint e referências definidos pelo script. Sem seed ou duração nativa configuráveis.'
        else:
            policy = ''
        self.status.setText('%s\n%s' % (info.detail, policy))
        self.engineChanged.emit(info.id)

    def request_options(self):
        info = self.selected_engine()
        if not info.supported:
            raise ValueError('%s: %s' % (info.name, info.detail))
        if info.id == 'omnivoice':
            duration = self.duration.text().strip() or 'livre'
            resolve_duration(duration, 1.0)  # Syntax only; actual original window comes from context.
            value = self.seed.text().strip()
            try:
                seed = int(value) if value else None
            except (TypeError, ValueError):
                raise ValueError('Seed deve ser um inteiro entre 0 e 2147483647.')
            if seed is not None and not 0 <= seed <= 2**31 - 1:
                raise ValueError('Seed deve ser um inteiro entre 0 e 2147483647.')
            return dict(engine=info.name, speed=self.speed.value(), duration=duration, seed=seed)
        options = dict(engine=info.name, max_speedup=self.max_speedup.value())
        if info.id == 'chatterbox':
            options['exaggeration'] = self.exaggeration.value()
        return options


class ModelManagerDialog(QDialog):
    """Configure actual adapters and display local weight presence in Qt."""
    settingsChanged = Signal()

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.setWindowTitle('Modelos locais e geração')
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        self.settings = GenerationSettingsWidget(context, self)
        layout.addWidget(self.settings)
        self.paths = QFormLayout()
        self.python_path = QLineEdit()
        self.script_path = QLineEdit()
        for label, control in (('Python da engine', self.python_path), ('Script da engine', self.script_path)):
            row = QHBoxLayout()
            row.addWidget(control)
            button = QPushButton('Escolher…')
            button.clicked.connect(lambda _checked=False, field=control: self.choose_path(field))
            row.addWidget(button)
            self.paths.addRow(label, row)
            if control is self.script_path:
                self.script_button = button
        layout.addLayout(self.paths)
        self.default_engine = QCheckBox('Usar esta engine como padrão nas próximas revisões')
        layout.addWidget(self.default_engine)
        self.models = QTableWidget(0, 3)
        self.models.setHorizontalHeaderLabels(['Modelo local', 'Arquivos', 'Pasta / detalhes'])
        self.models.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.models.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.models, 1)
        self.message = QLabel('A presença de arquivos não confirma geração. O OmniVoice usa o snapshot fixado no runner local.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        refresh = QPushButton('Atualizar arquivos locais')
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        buttons.addStretch()
        self.save_button = QPushButton('Salvar configurações')
        self.save_button.clicked.connect(self.save_settings)
        buttons.addWidget(self.save_button)
        close = QPushButton('Fechar')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.settings.engineChanged.connect(self.selected_changed)
        self.selected_changed()
        self.refresh_models()

    def selected_changed(self, *_args):
        info = self.settings.selected_engine()
        self.python_path.setText(str(info.python or ''))
        self.script_path.setText(str(info.script))
        self.python_path.setEnabled(info.supported)
        self.script_path.setReadOnly(info.id == 'omnivoice' or not info.supported)
        self.script_button.setEnabled(info.supported and info.id != 'omnivoice')
        self.save_button.setEnabled(info.supported)
        self.default_engine.setChecked(str(config.CFG.get('engine_padrao', '')).casefold() in (info.id, info.name.casefold()))

    def choose_path(self, control):
        path, _ = QFileDialog.getOpenFileName(self, 'Selecionar arquivo local', control.text())
        if path:
            control.setText(path)

    def refresh_models(self):
        models = local_models(self.context.project_root)
        self.models.setRowCount(len(models))
        for row, model in enumerate(models):
            for column, text in enumerate((model.name, 'Presentes' if model.present else 'Ausentes/incompletos', str(model.path))):
                item = QTableWidgetItem(text)
                item.setToolTip(model.detail)
                self.models.setItem(row, column, item)
        self.models.resizeColumnsToContents()

    def refresh(self):
        self.settings.refresh_engines()
        self.refresh_models()

    def save_settings(self):
        try:
            options = self.settings.request_options()
            info = self.settings.selected_engine()
            root = Path(self.context.project_root)
            paths = {}
            for name, field in (('python', self.python_path), ('script', self.script_path)):
                raw = field.text().strip()
                path = Path(raw).expanduser() if raw else None
                path = (path if path.is_absolute() else root / path) if path else None
                if not path or not path.is_file():
                    raise ValueError('Arquivo %s da engine não encontrado.' % name)
                paths[name] = str(path.resolve())
            engines = config.CFG.get('tts_engines')
            if not isinstance(engines, dict):
                engines = {}
                config.CFG['tts_engines'] = engines
            nested = engines.get(info.id)
            nested = dict(nested) if isinstance(nested, dict) else {}
            nested['python'] = paths['python']
            if info.id != 'omnivoice':
                nested['script'] = paths['script']
            engines[info.id] = nested
            values = config.CFG.get('qt_generation_settings')
            if not isinstance(values, dict):
                values = {}
                config.CFG['qt_generation_settings'] = values
            values[info.id] = {key: value for key, value in options.items() if key != 'engine'}
            if info.id == 'omnivoice':
                config.CFG.update(omnivoice_speed=options['speed'], omnivoice_duration=options['duration'],
                    omnivoice_seed=options['seed'], omnivoice_timing_policy_version=3)
            if self.default_engine.isChecked():
                config.CFG['engine_padrao'] = info.name
            config.cfg_save()
            self.message.setText('Configurações salvas. Cada geração usa somente os parâmetros da engine selecionada.')
            self.settingsChanged.emit()
            self.refresh()
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.message.setText(str(exc))
            return False
