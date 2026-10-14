"""Explicit-reference, single-take Qwen adapter; no implicit model downloads."""
import json
import re
import hashlib
from pathlib import Path
from studio import config

MODELS = {'1.7B': 'qwen3_tts_1_7b_base', '0.6B': 'qwen3_tts_0_6b_base'}


def local_models(project):
    root = Path(project)
    result = []
    for label, folder in MODELS.items():
        path = root / 'models' / folder
        try:
            cfg = json.loads((path / 'config.json').read_text(encoding='utf-8'))
            expected = '1b7' if label == '1.7B' else '0b6'
            if cfg.get('tts_model_type') == 'base' and cfg.get('tts_model_size') == expected and any(path.glob('*.safetensors')):
                result.append({'label': label, 'path': str(path.resolve())})
        except (OSError, ValueError):
            continue
    return result


def python_path(project):
    root = Path(project)
    candidates = [root / '.venv-qwen-local/Scripts/python.exe', root / '.venv-qwen/Scripts/python.exe', root / '.venv_qwen/Scripts/python.exe']
    return next((p for p in candidates if p.is_file()), None)


def build_command(project, model, reference, reference_text, text, output, device='auto',
                  reference_sha256=None):
    root = Path(project).resolve()
    available = local_models(root)
    choice = next((m for m in available if m['label'] == model or m['path'] == str(model)), None)
    python = python_path(root)
    if not choice or not python:
        raise ValueError('Qwen local indisponível: prepare o modelo Base e o ambiente Python antes de gerar.')
    reference = Path(reference).resolve()
    output = Path(output).resolve()
    output.relative_to(root / 'workspace' / 'cutscene_studio')
    text = re.sub(r'~[^~]*~', '', text).strip()
    reference_text = re.sub(r'~[^~]*~', '', reference_text).strip()
    if not reference.is_file() or not text or not reference_text:
        raise ValueError('Escolha um WAV de referência, sua transcrição e o texto da nova fala.')
    with reference.open('rb') as stream:
        actual_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    if reference_sha256 and actual_hash != reference_sha256:
        raise ValueError('O áudio de referência mudou desde a seleção; escolha uma referência íntegra.')
    if device not in ('auto', 'cpu', 'cuda:0'):
        raise ValueError('Dispositivo inválido.')
    return [str(python), '-u', str(config.APP_DIR / 'tools/cutscene_tts.py'),
            '--model', choice['path'], '--reference', str(reference), '--reference-text', re.sub(r'~[^~]*~', '', reference_text).strip(),
            '--text', re.sub(r'~[^~]*~', '', text).strip(), '--output', str(output), '--device', device,
            '--reference-sha256', actual_hash, '--non-streaming-mode']
