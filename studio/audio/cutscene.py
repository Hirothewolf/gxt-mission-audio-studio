"""Read-only native CUTSCENE export and caption-window audition copies."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from studio.core.utils import CREATE_FLAGS, json_atomico
from studio.script.native_index import digest


def cache_root(root):
    return Path(root).resolve() / '.cache' / 'cutscene_original'


def extract(game, root, executable):
    game, root = Path(game).resolve(), Path(root).resolve()
    archive = game / 'audio/streams/CUTSCENE'
    before = digest(archive)
    dest = cache_root(root) / before[:20]
    if (dest / 'source.json').is_file():
        metadata = json.loads((dest / 'source.json').read_text(encoding='utf-8'))
        if metadata.get('archive_sha256') == before and len(metadata.get('files', {})) == 141 and all(
                (dest / name).is_file() and digest(dest / name) == item['sha256']
                for name, item in metadata['files'].items()):
            return dest
        raise ValueError('Cache CUTSCENE alterado; preserve-o e remova-o do caminho antes de extrair novamente.')
    stage = cache_root(root) / ('stage-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    # Exporter only receives original stream input and an isolated cache output.
    completed = subprocess.run([str(executable), '-e', str(archive), str(stage)],
                               cwd=str(Path(executable).parent), creationflags=CREATE_FLAGS, check=False)
    if completed.returncode:
        raise RuntimeError('SAAT não concluiu a extração de CUTSCENE. Cache parcial preservado em ' + str(stage))
    exported = stage / 'CUTSCENE'
    files = sorted(exported.glob('Track_*.ogg'))
    if [p.name for p in files] != ['Track_%03d.ogg' % i for i in range(1, 142)]:
        raise ValueError('Extração não contém as 141 faixas CUTSCENE esperadas.')
    signatures = {}
    for path in files:
        with path.open('rb') as stream:
            if stream.read(4) != b'OggS':
                raise ValueError('Faixa OGG inválida: ' + path.name)
        signatures['CUTSCENE/' + path.name] = {'sha256': digest(path), 'bytes': path.stat().st_size}
    if digest(archive) != before:
        raise ValueError('CUTSCENE original mudou durante a extração.')
    json_atomico(stage / 'source.json', {'archive': str(archive), 'archive_sha256': before, 'files': signatures})
    stage.rename(dest)
    return dest


def resolve_window(record, project, report, render=True):
    if not record or record.get('pak') != 'CUTSCENE' or record.get('cutscene_ambiguous'):
        return None
    stream = record.get('stream') or {}
    track = stream.get('track')
    if not track or not report:
        return None
    source = report['sources']['stream']
    root = cache_root(project) / source['sha256'][:20]
    metadata_path = root / 'source.json'
    if not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if metadata.get('archive_sha256') != source['sha256'] or metadata.get('archive') != source['path']:
        return None
    filename = 'CUTSCENE/' + track + '.ogg'
    track_path = root / filename
    expected = metadata.get('files', {}).get(filename)
    if not expected or not track_path.is_file() or digest(track_path) != expected['sha256']:
        return None
    # Subtitle timing can span pauses/effects. Explicitly audition a timing
    # window; this is not a forced-aligned transcription or TTS voice reference.
    start = max(0, record['caption_start_ms'] / 1000 - .15)
    duration = record['caption_duration_ms'] / 1000 + .3
    identity = hashlib.sha256(json.dumps([expected['sha256'], start, duration]).encode()).hexdigest()[:20]
    output = root / 'caption_windows' / (identity + '.wav')
    if output.is_file():
        return output
    if not render:
        return None
    executable = shutil.which('ffmpeg')
    if not executable:
        return None
    output.parent.mkdir(exist_ok=True)
    temporary = output.with_name(identity + '.part.wav')
    result = subprocess.run([executable, '-v', 'error', '-y', '-ss', str(start), '-i', str(track_path),
                             '-t', str(duration), '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(temporary)],
                            capture_output=True, creationflags=CREATE_FLAGS, timeout=45)
    if result.returncode or not temporary.is_file() or temporary.stat().st_size < 64:
        temporary.unlink(missing_ok=True)
        return None
    temporary.replace(output)
    return output
