"""Automatic, cancellable full-track access. Game archives are read-only."""
import configparser
import json
import re
import uuid
from pathlib import Path

from studio.core.utils import json_atomico
from studio.audio.timeline_project import digest
from studio.saat.client import SaatClient


def full_track(context, container, track, installed=False, progress=lambda _: None, cancel=None):
    if container not in ('CUTSCENE', 'BEATS') or not re.fullmatch(r'Track_\d{3}', track):
        raise ValueError('Cena inválida. Escolha uma cena da lista.')
    count = 141 if container == 'CUTSCENE' else 10
    if not 1 <= int(track[6:]) <= count:
        raise ValueError('Esta faixa não existe no banco selecionado.')
    ga = context.game_audio
    if not ga.game_dir:
        raise ValueError('Escolha a pasta do GTA em Opções → Configurar jogo e mod.')
    base = ga._safe_mod_dir(ga.mod_ativo()) if installed else Path(ga.game_dir)
    archive = base / 'audio/streams' / container
    if not archive.is_file():
        if installed:
            return None
        raise ValueError('O banco %s não foi encontrado na pasta do jogo.' % container)
    progress('Conferindo o áudio %s de %s…' % ('do mod' if installed else 'original', container))
    archive_sha = digest(archive)
    cache = context.project_root / '.cache/qt_full_streams' / archive_sha
    # Reuse the established original CUTSCENE export instead of duplicating it.
    legacy = context.project_root / '.cache/cutscene_original' / archive_sha[:20]
    if not installed and container == 'CUTSCENE' and (legacy / 'source.json').is_file():
        metadata = json.loads((legacy / 'source.json').read_text(encoding='utf-8'))
        relative = container + '/' + track + '.ogg'
        signature = metadata.get('files', {}).get(relative, {})
        path = legacy / relative
        if metadata.get('archive_sha256') == archive_sha and path.is_file() and digest(path) == signature.get('sha256'):
            return dict(source_path=str(path), source_sha256=signature['sha256'], archive_path=str(archive), archive_sha256=archive_sha)
    manifest = cache / 'source.json'
    if manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        path = cache / container / (track + '.ogg')
        if metadata.get('archive_sha256') != archive_sha or not path.is_file() or digest(path) != metadata.get('files', {}).get(track):
            raise ValueError('O cache desta cena mudou. Ele foi preservado; atualize as fontes antes de continuar.')
        return dict(source_path=str(path), source_sha256=metadata['files'][track], archive_path=str(archive), archive_sha256=archive_sha)
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Abertura da cena cancelada.')
    exe = ga.saat().get('stream')
    if not exe:
        raise ValueError('Configure o SAAT de cutscenes em Opções → Gerenciador SAAT.')
    stage = cache.with_name('stage-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    progress('Extraindo %s automaticamente. Nas próximas aberturas o cache será reutilizado…' % container)
    client = SaatClient(ga.game_dir, exe_stream=exe)
    rc, lines = client._executar([str(exe), '-e', str(archive), str(stage)], Path(exe).parent, progress, cancel)
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Extração cancelada. O cache parcial foi preservado.')
    if rc:
        raise RuntimeError('Não foi possível abrir o banco de áudio. Detalhes: ' + '\n'.join(lines[-4:]))
    folder = stage / container
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(folder / 'stream_import.ini', encoding='utf-8-sig')
    tracks = [s for s in ini.sections() if re.fullmatch(r'Track_\d{3}', s)]
    if sorted(tracks) != ['Track_%03d' % n for n in range(1, count + 1)]:
        raise ValueError('A extração ficou incompleta. Os arquivos parciais foram preservados.')
    files = {}
    for name in tracks:
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Conferência da extração cancelada.')
        path = (folder / ini.get(name, 'filename').replace('\\', '/')).resolve()
        path.relative_to(folder.resolve())
        if path != (folder / (name + '.ogg')).resolve():
            raise ValueError('O SAAT retornou um nome de faixa inesperado.')
        files[name] = digest(path)
    if digest(archive) != archive_sha:
        raise ValueError('O banco mudou durante a leitura; reabra a cena para usar a versão atual.')
    json_atomico(stage / 'source.json', dict(archive_path=str(archive), archive_sha256=archive_sha, files=files))
    # Two windows may finish the same extraction together. Never overwrite a cache.
    if cache.exists():
        return full_track(context, container, track, installed, progress, cancel)
    stage.rename(cache)
    return dict(source_path=str(cache / container / (track + '.ogg')), source_sha256=files[track], archive_path=str(archive), archive_sha256=archive_sha)
