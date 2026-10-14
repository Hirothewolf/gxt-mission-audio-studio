"""Lightweight runtime discovery; importing the UI never loads Torch or downloads."""
from pathlib import Path
import json
import sys
from studio import config

MODEL_REPO = 'speechbrain/spkrec-ecapa-voxceleb'
MODEL_REVISION = '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286'
MODEL_FILES = ('hyperparams.yaml', 'embedding_model.ckpt', 'classifier.ckpt', 'mean_var_norm_emb.ckpt', 'label_encoder.txt')


def runtime_root(project_root):
    return Path(project_root).resolve() / 'tools' / 'voice_identity_runtime'


def find_python(project_root):
    root = Path(project_root)
    values = [config.CFG.get('speaker_python')]
    values += [root / name / 'Scripts' / 'python.exe' for name in ('.venv-speakers', '.venv-qwen-local', '.venv_chatterbox')]
    for value in values:
        if value and Path(value).is_file():
            return Path(value).resolve()
    return None


def runtime_status(project_root):
    root = runtime_root(project_root)
    python = find_python(project_root)
    result = {'ready': False, 'python': str(python) if python else None,
              'model_dir': str(root / 'model'), 'model_id': MODEL_REPO + '@' + MODEL_REVISION,
              'can_setup': bool(python), 'state': 'missing_runtime',
              'reason': 'Python com Torch não encontrado. Configure speaker_python ou o ambiente Qwen local.'}
    if not python:
        return result
    result.update(state='missing_model', reason='Prepare o reconhecimento local antes de analisar. Nenhum download ocorre ao abrir esta tela.')
    try:
        ready = json.loads((root / 'ready.json').read_text(encoding='utf-8'))
        if ready.get('python') != str(python) or ready.get('revision') != MODEL_REVISION:
            return result
        if ready.get('inference_checked') is not True or not isinstance(ready.get('files'), dict):
            return result
        recorded_paths = {str(key).replace('\\', '/') for key in ready['files']}
        if not all('model/' + name in recorded_paths for name in MODEL_FILES):
            return result
        for relative, signature in ready['files'].items():
            candidate = (root / relative).resolve()
            candidate.relative_to(root.resolve())
            stat = candidate.stat()
            if [stat.st_size, stat.st_mtime_ns] != signature:
                result['reason'] = 'Runtime/modelo alterado. Prepare novamente para verificar os arquivos.'
                return result
        if not all((root / 'model' / name).is_file() for name in MODEL_FILES):
            return result
        result.update(ready=True, state='ready', reason='Reconhecimento local preparado; sugestões ainda precisam de revisão.', model_id=ready['model_id'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def _command(project_root, task):
    python = find_python(project_root)
    if not python:
        raise RuntimeError(runtime_status(project_root)['reason'])
    cli = Path(__file__).resolve().parents[2] / 'tools' / 'voice_identity_cli.py'
    return [str(python), '-u', str(cli), '--project-root', str(Path(project_root).resolve()), task]


def build_setup_command(project_root):
    return _command(project_root, 'setup')


def build_analysis_command(project_root, tables=None, limit=None):
    state = runtime_status(project_root)
    if not state['ready']:
        raise RuntimeError(state['reason'])
    command = _command(project_root, 'analyze')
    for table in tables or ():
        command += ['--table', str(table)]
    if limit is not None:
        command += ['--limit', str(max(1, int(limit)))]
    return command
