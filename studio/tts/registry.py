# -*- coding: utf-8 -*-
"""Engine discovery and backward-compatible selection helpers."""
import sys
from dataclasses import dataclass
from pathlib import Path

from studio import config
from studio.tts.legacy import LegacyEngine

# (display name, id, virtual environments, default script in project/tools)
ENGINES = [
    ('Qwen3-TTS', 'qwen', ['.venv-qwen-local', '.venv-qwen', '.venv_qwen'], 'redub_qwen.py'),
    ('Chatterbox', 'chatterbox', ['.venv_chatterbox', '.venv-chatterbox'], 'redub_chatterbox.py'),
    ('Piper-TTS (pt_BR)', 'piper', ['.venv_piper', '.venv-piper'], 'redub_piper.py'),
    ('F5-TTS', 'f5', ['.venv_f5', '.venv-f5', '.venv_f5tts'], 'redub_f5tts.py'),
    ('OmniVoice', 'omnivoice', [], 'omnivoice_tts.py'),
]
_BY_ID = {engine_id: (name, venvs, script) for name, engine_id, venvs, script in ENGINES}
_SUPPORTED = {'qwen', 'chatterbox', 'omnivoice'}
_UNSUPPORTED_REASONS = {
    'f5': (
        'F5-TTS não está conectado a um runtime validado neste editor. O checkpoint '
        'F5-TTS Base do catálogo é para inglês/chinês; não há um modelo pt_BR validado. '
        'Baixar pesos não instala o runtime nem habilita geração em português.'
    ),
}


def _path(value, root):
    if value in (None, ''):
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _nested_engine(engine_id):
    engines = config.CFG.get('tts_engines', {})
    if not isinstance(engines, dict):
        return {}
    aliases = {'qwen': ('qwen', 'qwen3-tts'), 'chatterbox': ('chatterbox',)}
    for key in aliases.get(engine_id, (engine_id,)):
        value = engines.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _python_de_venv(venv, root=None):
    """Find a Python executable under a project-local virtual environment."""
    root = Path(root or config.PROJ_ROOT)
    for rel in ('Scripts/python.exe', 'bin/python', 'bin/python3'):
        candidate = root / venv / rel
        if candidate.is_file():
            return candidate
    return None


def _paths_for(engine_id, root):
    if engine_id == 'omnivoice':
        from studio.tts.omnivoice import _runtime_python
        return 'OmniVoice', _runtime_python(root), config.APP_DIR / 'tools' / 'omnivoice_tts.py'
    name, venvs, default_script = _BY_ID[engine_id]
    nested = _nested_engine(engine_id)
    python_value = (nested.get('python') or nested.get('python_path') or
                    nested.get('python_executable') or config.CFG.get('python_' + engine_id))
    script_value = nested.get('script') or nested.get('script_path') or config.CFG.get('script_' + engine_id)
    python_path = _path(python_value, root) if python_value else None
    if python_path is None or not python_path.is_file():
        python_path = next((found for item in venvs if (found := _python_de_venv(item, root))), None)
    script_path = _path(script_value, root) if script_value else root / 'tools' / default_script
    return name, python_path, script_path


def detectar_engines():
    """Return legacy tuples; callers determine availability from both paths."""
    root = Path(config.PROJ_ROOT)
    return [_paths_for(engine_id, root) for _, engine_id, _, _ in ENGINES]


def select_engine(preference=None, root=None):
    """Select an available verified CLI adapter, never silently substituting."""
    root_path = Path(root or config.PROJ_ROOT).expanduser().resolve()
    configured = config.CFG.get('engine_padrao', 'auto')
    requested = preference if preference not in (None, '') else configured
    requested_norm = str(requested).strip().casefold()
    if requested_norm in ('', 'auto'):
        order = ['qwen', 'chatterbox']
    else:
        aliases = {
            'qwen': 'qwen', 'qwen3-tts': 'qwen',
            'chatterbox': 'chatterbox',
        }
        for name, engine_id, _, _ in ENGINES:
            aliases[name.casefold()] = engine_id
        selected = aliases.get(requested_norm)
        if selected is None:
            raise RuntimeError(f'Engine selecionada indisponível ou sem adapter: {requested}.')
        if selected in _UNSUPPORTED_REASONS:
            raise RuntimeError(_UNSUPPORTED_REASONS[selected])
        order = [selected]

    unavailable = []
    for engine_id in order:
        if engine_id not in _SUPPORTED:
            unavailable.append(engine_id)
            continue
        name, python_path, script_path = _paths_for(engine_id, root_path)
        if python_path and python_path.is_file() and script_path.is_file():
            if engine_id == 'omnivoice':
                return OmniVoiceEngine(engine_id, name, python_path.resolve(), script_path.resolve(), root_path)
            return LegacyEngine(engine_id, name, python_path.resolve(), script_path.resolve(), root_path)
        unavailable.append(name)

    label = requested if requested_norm not in ('', 'auto') else 'auto'
    detail = ', '.join(unavailable) or 'nenhum adapter TTS disponível'
    raise RuntimeError(f'Engine {label} indisponível (Python e script válidos necessários; verifique: {detail}).')


def achar_python_ai():
    """Compatibility tuple for legacy callers, with strict availability checks."""
    engine = select_engine()
    return engine.python, engine.script, engine.name


def achar_python_chatterbox():
    """Compatibility alias retained for older imports."""
    python_path, _, _ = achar_python_ai()
    return python_path


@dataclass(frozen=True)
class OmniVoiceEngine:
    """Single-utterance engine requiring explicit original-reference provenance."""
    id: str
    name: str
    python: Path
    script: Path
    root: Path

    @property
    def capabilities(self):
        from studio.tts.base import TTSCapabilities
        return TTSCapabilities(voice_cloning=True, reference_audio=True,
                               reference_text=True, speed_control=True,
                               batch_generation=False, custom_checkpoint=False,
                               offline_cpu=False)

    def build_command(self, *_args, **_kwargs):
        raise RuntimeError('OmniVoice exige uma fala individual e uma referência original verificada; lote indisponível.')
