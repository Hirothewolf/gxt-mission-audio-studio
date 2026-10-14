"""Explicit local voice-model preparation, original-SCRIPT analysis and held-out probe.

Heavy dependencies are confined to this child process. No synthesis/game writes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
from studio.audio.voice_runtime import MODEL_FILES, MODEL_REPO, MODEL_REVISION, runtime_root, runtime_status
from studio.core.utils import json_atomico

PINNED_WHEELS = {
    'hyperpyyaml': {'filename': 'hyperpyyaml-1.2.3-py3-none-any.whl',
                   'url': 'https://files.pythonhosted.org/packages/7d/a5/98e20cc4365e293dfc1382e2f8dbf44b1cb14d75658ab5147cb0d6d8718e/hyperpyyaml-1.2.3-py3-none-any.whl',
                   'digests': {'sha256': '0088c8ce97dc7c7d3460112b6ccc92dca46fade4d0320bc39f58b1fdda60f7f1'}},
    'ruamel.yaml': {'filename': 'ruamel.yaml-0.18.16-py3-none-any.whl',
                    'url': 'https://files.pythonhosted.org/packages/0f/73/bb1bc2529f852e7bf64a2dec885e89ff9f5cc7bbf6c9340eed30ff2c69c5/ruamel.yaml-0.18.16-py3-none-any.whl',
                    'digests': {'sha256': '048f26d64245bae57a4f9ef6feb5b552a386830ef7a826f235ffb804c59efbba'}},
}

PACKAGES = {'speechbrain': '1.1.1', 'hyperpyyaml': '1.2.3', 'ruamel.yaml': '0.18.16', 'sentencepiece': '0.2.1'}


def progress(done, total, message):
    print(json.dumps({'event': 'progress', 'done': done, 'total': total, 'message': message}, ensure_ascii=False), flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def get_json(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as stream:
                return json.load(stream)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            progress(0, None, 'Conexão interrompida; repetindo consulta de metadados.')


def download(url, target, expected_hash=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and (not expected_hash or sha256(target) == expected_hash):
        progress(1, 1, 'Arquivo local reutilizado: ' + target.name)
        return
    temporary = target.with_suffix(target.suffix + '.part')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=25) as source, temporary.open('wb') as dest:
                total = int(source.headers.get('Content-Length', 0)) or None
                done = 0
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    dest.write(block)
                    done += len(block)
                    progress(done, total, 'Baixando ' + target.name)
            if expected_hash and sha256(temporary) != expected_hash:
                raise ValueError('Checksum inválido: ' + target.name)
            temporary.replace(target)
            return
        except (OSError, ValueError):
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            progress(0, None, 'Transferência interrompida; repetindo ' + target.name)


def wheel_file(metadata):
    files = [item for item in metadata['urls'] if item['filename'].endswith('py3-none-any.whl')]
    if not files:
        tag = 'cp%d%d' % sys.version_info[:2]
        files = [item for item in metadata['urls'] if tag in item['filename'] and 'win_amd64.whl' in item['filename']]
    if len(files) != 1:
        raise RuntimeError('Wheel compatível não encontrado para ' + metadata['info']['name'])
    return files[0]


def load_encoder(model_dir):
    import torch
    from speechbrain.inference.classifiers import EncoderClassifier
    from speechbrain.utils.fetching import FetchConfig, LocalStrategy
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    return EncoderClassifier.from_hparams(
        source=str(model_dir), savedir=str(model_dir),
        overrides={'pretrained_path': str(model_dir)},
        run_opts={'device': 'cpu'},
        local_strategy=LocalStrategy.COPY,
        fetch_config=FetchConfig(allow_network=False),
    )


def _remove_runtime_tree(root, target):
    root, target = Path(root).resolve(), Path(target).resolve()
    target.relative_to(root)
    if target == root:
        raise ValueError('Recusa remover a raiz do runtime.')
    shutil.rmtree(target)


def setup(root):
    stage = root / ('deps-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    sources = []
    try:
        for name, version in PACKAGES.items():
            if name in PINNED_WHEELS:
                wheel = PINNED_WHEELS[name]
            else:
                metadata_path = root / 'downloads' / (name + '-' + version + '.json')
                if metadata_path.is_file():
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                else:
                    metadata = get_json('https://pypi.org/pypi/%s/%s/json' % (name, version))
                    json_atomico(metadata_path, metadata)
                wheel = wheel_file(metadata)
            target = root / 'downloads' / wheel['filename']
            download(wheel['url'], target, wheel['digests']['sha256'])
            with zipfile.ZipFile(target) as archive:
                for member in archive.namelist():
                    (stage / member).resolve().relative_to(stage.resolve())
                archive.extractall(stage)
            sources.append({'package': name, 'version': version, 'url': wheel['url'], 'sha256': wheel['digests']['sha256']})
        model_dir = root / 'model'
        model_dir.mkdir(exist_ok=True)
        tree = get_json('https://huggingface.co/api/models/%s/tree/%s?recursive=true' % (MODEL_REPO, MODEL_REVISION))
        hashes = {item['path']: (item.get('lfs') or {}).get('oid') for item in tree if isinstance(item, dict)}
        for name in MODEL_FILES:
            download('https://huggingface.co/%s/resolve/%s/%s' % (MODEL_REPO, MODEL_REVISION, name), model_dir / name, hashes.get(name))
        progress(0, None, 'Carregando o modelo local e verificando a inferência em CPU.')
        # Loaded Windows extension DLLs cannot be renamed/deleted. Validate in a
        # short-lived child before promoting its isolated dependency directory.
        checked = subprocess.run([sys.executable, '-u', str(Path(__file__).resolve()),
                                  '--project-root', str(root.parents[1]), '_verify',
                                  '--deps', str(stage)], check=False)
        if checked.returncode:
            raise RuntimeError('O teste de inferência local falhou; consulte o terminal da operação.')
        dest = root / 'deps'
        old = root / ('deps-backup-' + uuid.uuid4().hex)
        if dest.exists():
            dest.rename(old)
        try:
            stage.rename(dest)
            files = {str((model_dir / name).relative_to(root)): [(model_dir / name).stat().st_size, (model_dir / name).stat().st_mtime_ns] for name in MODEL_FILES}
            for dependency in dest.rglob('*'):
                if dependency.is_file() and '__pycache__' not in dependency.parts:
                    stat = dependency.stat()
                    files[dependency.relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            manifest = {'python': str(Path(sys.executable).resolve()), 'revision': MODEL_REVISION,
                        'model_id': MODEL_REPO + '@' + MODEL_REVISION + ':' + sha256(model_dir / 'embedding_model.ckpt'),
                        'files': files, 'sources': sources, 'device': 'cpu', 'inference_checked': True}
            json_atomico(root / 'ready.json', manifest)
        except Exception:
            if dest.exists():
                _remove_runtime_tree(root, dest)
            if old.exists():
                old.rename(dest)
            raise
        if old.exists():
            _remove_runtime_tree(root, old)
        progress(1, 1, 'Reconhecimento local preparado. Áudios não foram enviados para serviços externos.')
    finally:
        if stage.exists():
            _remove_runtime_tree(root, stage)


class EcapaEmbedder:
    model_version = 'speechbrain-1.1.1/preprocess-v1'
    def __init__(self, root):
        metadata = json.loads((root / 'ready.json').read_text(encoding='utf-8'))
        weights = root / 'model' / 'embedding_model.ckpt'
        if metadata['model_id'].split(':')[-1] != sha256(weights):
            raise ValueError('Modelo alterado; prepare novamente o reconhecimento.')
        self.model_id = metadata['model_id']
        sys.path.insert(0, str(root / 'deps'))
        self.encoder = load_encoder(root / 'model')

    def __call__(self, path):
        import soundfile
        import numpy as np
        import torch
        import torchaudio
        audio, rate = soundfile.read(str(path), dtype='float32', always_2d=True)
        tensor = torch.from_numpy(np.mean(audio, axis=1)).unsqueeze(0)
        if rate != 16000:
            tensor = torchaudio.functional.resample(tensor, rate, 16000)
        # Bound memory for abnormally long dialogue; uniformly spaced speech windows.
        if tensor.shape[-1] > 16000 * 20:
            starts = [0, (tensor.shape[-1] - 16000 * 5) // 2, tensor.shape[-1] - 16000 * 5]
            tensor = torch.cat([tensor[:, start:start + 16000 * 5] for start in starts], dim=1)
        with torch.inference_mode():
            return self.encoder.encode_batch(tensor).reshape(-1).cpu().tolist()


def collect(project, tables=None, limit=None):
    from studio.config import CFG
    from studio.saat.audio import GameAudio
    from studio.audio.library_context import read_manifest
    from studio.audio.voice_identity import VoiceIdentityStore, collect_original_items
    from studio.projects.review import ReviewStore
    game = GameAudio(CFG, lambda: None, project / '.cache' / 'saat', project / 'backups' / 'modloader', project / 'workspace' / 'modloader_manifest.json')
    records = read_manifest(project)
    store = ReviewStore(project)
    targets = collect_original_items(game, records, store, tables=tables, limit=limit)
    all_items = collect_original_items(game, records, store)
    identities = VoiceIdentityStore(project)
    for item in all_items:
        if item.get('known_identity') or item.get('evidence', {}).get('seed_conflict'):
            continue
        # Explicitly reviewed groups can teach a later run; suggestions cannot.
        fields = item['audio_id'].split(':')
        info = {'pak': 'SCRIPT', 'bank_num': int(fields[1][1:]), 'sound_num': int(fields[2][1:])}
        accepted = identities.lookup(info, item['path'])
        if accepted and accepted['status'] == 'VOICE_CONFIRMED':
            item['known_identity'] = accepted['identity']
            item['evidence'] = {'seed_source': 'confirmed_voice_group', 'report_id': accepted['report_id']}
    references = [item for item in all_items if item.get('known_identity')]
    return targets, references


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    sub = parser.add_subparsers(dest='task', required=True)
    sub.add_parser('setup')
    verify = sub.add_parser('_verify', help=argparse.SUPPRESS)
    verify.add_argument('--deps', required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--table', action='append')
    analyze.add_argument('--limit', type=int)
    pilot = sub.add_parser('pilot')
    pilot.add_argument('--per-character', type=int, default=3)
    args = parser.parse_args(argv)
    project = Path(args.project_root).resolve()
    root = runtime_root(project)
    root.mkdir(parents=True, exist_ok=True)
    if args.task == '_verify':
        sys.path.insert(0, args.deps)
        encoder = load_encoder(root / 'model')
        import torch
        with torch.inference_mode():
            vector = encoder.encode_batch(torch.zeros(1, 32000))
        if not torch.isfinite(vector).all():
            raise RuntimeError('O modelo retornou uma representação inválida.')
        progress(1, 1, 'Inferência local verificada em CPU: Torch ' + torch.__version__)
        return 0
    if args.task == 'setup':
        setup(root)
        return 0
    state = runtime_status(project)
    if not state['ready']:
        raise RuntimeError(state['reason'])
    from studio.audio.voice_identity import VoiceIdentityStore, analyze_items
    targets, references = collect(project, getattr(args, 'table', None), getattr(args, 'limit', None))
    if not targets:
        raise RuntimeError('Nenhum WAV original SCRIPT com coordenadas neste escopo. Confira a extração original e os vínculos da missão.')
    if args.task == 'pilot':
        from collections import defaultdict
        by_identity = defaultdict(list)
        for item in references:
            by_identity[item['known_identity']].append(item)
        targets = []
        for identity, items in sorted(by_identity.items()):
            if len(items) >= 5:
                targets += items[-min(args.per_character, len(items)-2):]
        held_out = {item['audio_id'] for item in targets}
        references = [item for item in references if item['audio_id'] not in held_out]
        if not targets:
            raise RuntimeError('Não há referências suficientes para um teste separado por personagem.')
        store = VoiceIdentityStore(project / 'workspace' / 'voice_identity_pilot')
    else:
        store = VoiceIdentityStore(project)
    progress(0, None, 'Carregando reconhecimento e referências originais; nenhum áudio do mod será usado como voz original.')
    report = analyze_items(targets, EcapaEmbedder(root), store, reference_items=references, on_progress=progress)
    if args.task == 'pilot':
        measured = []
        for item in report['items']:
            measured.append({'audio_id': item['audio_id'], 'expected': item.get('known_identity'),
                             'suggested': item.get('suggested_identity'), 'status': item.get('status'),
                             'score': item.get('score'), 'margin': item.get('margin'),
                             'correct': bool(item.get('suggested_identity') and item['suggested_identity'] == item['known_identity'])})
        suggested = [item for item in measured if item['status'] == 'SUGGESTED']
        result = {'items': measured, 'total': len(measured), 'correct': sum(item['correct'] for item in measured),
                  'suggested': len(suggested), 'wrong_suggestions': sum(not item['correct'] for item in suggested),
                  'abstained': len(measured) - len(suggested), 'thresholds': report['thresholds'],
                  'note': 'Hold-out from manifest labels, not independent human ground truth. No reference shares target audio_id; duplicate content is excluded by core.'}
        json_atomico(APP_ROOT / 'reports' / 'voice_identity_pilot.json', result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    progress(1, 1, 'Análise concluída; confira os grupos antes de confirmar personagens.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'event': 'error', 'message': str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
