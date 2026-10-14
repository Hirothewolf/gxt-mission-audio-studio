"""Verified whole-scene publication into an existing mod stream bank only."""
import configparser
import copy
import json
import re
import shutil
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from studio.audio.timeline_project import TimelineProject
from studio.audio.stream_install import _run, _signature, _proof_path, validate_lookup, refresh_scene_evidence
from studio.core.utils import json_atomico, sha256_arquivo


def _tracks(folder, count):
    ini = configparser.ConfigParser(interpolation=None)
    ini.optionxform = str
    ini.read(folder / 'stream_import.ini', encoding='utf-8-sig')
    names = ['Track_%03d' % i for i in range(1, count + 1)]
    if sorted(s for s in ini.sections() if s.startswith('Track_')) != names:
        raise ValueError('Banco exportado incompleto ou com quantidade de faixas inválida.')
    paths = {}
    for name in names:
        path = (folder / ini.get(name, 'filename').replace('\\', '/')).resolve()
        path.relative_to(folder.resolve())
        if not path.is_file():
            raise ValueError('Falta uma faixa exportada.')
        paths[name] = path
    return ini, paths


def publish_scene(context, project, mod=None, progress=None, cancel=None):
    """Render a frozen scene, verify a complete native repack, then publish with rollback."""
    def check():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Publicação da cena cancelada.')
    def report(n, text):
        if progress:
            progress(n, 5, text)
        check()
    frozen = TimelineProject()
    frozen.data = copy.deepcopy(project.data)
    frozen.validate()
    scene = frozen.data['scene']
    container, track = scene.get('container'), scene.get('track', '')
    count = {'CUTSCENE': 141, 'BEATS': 10}.get(container)
    if not count or not re.fullmatch(r'Track_\d{3}', track) or not 1 <= int(track[6:]) <= count:
        raise ValueError('Destino nativo de cena inválido.')
    ga = context.game_audio
    mod = mod or ga.mod_ativo()
    if scene.get('installed_mod', mod) != mod:
        raise ValueError('A cena pertence a outro mod; reabra as fontes.')
    mod_dir = ga._safe_mod_dir(mod).resolve()
    archive = mod_dir / 'audio/streams' / container
    lookup = mod_dir / 'audio/CONFIG/TrakLkup.dat'
    for path in (archive, lookup):
        if not path.is_file() or not ga._dentro(mod_dir, path.resolve()):
            raise ValueError('Falta um banco/lookup dentro do próprio mod.')
    original = Path(scene.get('original_path', ''))
    original_archive = Path(scene.get('original_archive_path', ''))
    if (not original.is_file() or not original_archive.is_file()
            or sha256_arquivo(original) != scene.get('original_sha256')
            or sha256_arquivo(original_archive) != scene.get('original_archive_sha256')):
        raise ValueError('A fonte original da cena está ausente ou mudou.')
    eligible = [t for t in frozen.data['tracks'] if t['role'] not in ('original', 'installed')]
    solo = any(t['solo'] for t in eligible)
    voices = {t['id'] for t in eligible if t['role'] == 'pt' and not t['mute']
              and t['gain'] > 0 and (not solo or t['solo'])}
    has_voices = any(c['track_id'] in voices and c['gain'] > 0 for c in frozen.data['clips'])
    active = {t['id'] for t in eligible if not t['mute'] and t['gain'] > 0
              and (not solo or t['solo'])}
    has_background = any(c['track_id'] in active and c['gain'] > 0
                         and c.get('metadata', {}).get('scene_background') is True
                         for c in frozen.data['clips'])
    if not has_voices and not has_background:
        raise ValueError('Não há vozes PT ou fundo de cena audíveis para publicar.')
    exe, ffmpeg = ga.saat().get('stream'), shutil.which('ffmpeg')
    if not exe or not ffmpeg:
        raise ValueError('Configure SAAT e ffmpeg antes de publicar.')
    root = Path(context.project_root).resolve()
    run = root / 'workspace/scene_publications' / uuid.uuid4().hex
    check()
    with ga._native_install_lock():
        if ga._game_is_running():
            raise ValueError('Feche o GTA antes de publicar.')
        pins = {p: sha256_arquivo(p) for p in (archive, lookup, original, original_archive)}
        if (pins[original] != scene.get('original_sha256')
                or pins[original_archive] != scene.get('original_archive_sha256')):
            raise ValueError('A fonte original mudou antes da montagem.')
        if scene.get('installed_lookup_sha256') and pins[lookup] != scene['installed_lookup_sha256']:
            raise ValueError('O lookup do mod mudou desde a abertura da cena.')
        if pins[archive] != scene.get('installed_archive_sha256'):
            raise ValueError('O banco do mod mudou desde a abertura da cena; reabra a cena.')
        run.mkdir(parents=True)
        staged, staged_lookup = run / container, run / 'TrakLkup.dat'
        shutil.copy2(archive, staged)
        shutil.copy2(lookup, staged_lookup)
        report(0, 'Lendo o banco atual do mod')
        export = run / 'source'; export.mkdir()
        _run(exe, ['-e', staged, export], run, run / 'export.log')
        ini, sources = _tracks(export / container, count)
        current = sf.info(sources[track])
        # The current destination is authoritative for physical duration. The
        # original remains the voice/timing reference, not a mandate to cut an
        # existing translated track that intentionally has an extra second.
        native = current
        native_shape = (current.frames, current.samplerate, current.channels)
        if (frozen.sample_rate, frozen.data['channels']) != (current.samplerate, current.channels):
            raise ValueError('Reabra a cena: taxa ou canais do rascunho diferem do áudio atual do mod.')
        audible_end = max((c['position'] + frozen.clip_frames(c) for c in frozen.data['clips']
            if c['track_id'] in active and c['gain'] > 0), default=0)
        if audible_end > current.frames:
            raise ValueError('A timeline ultrapassa a duração atual da cena no mod; nenhuma fala será cortada.')
        report(1, 'Renderizando a cena congelada')
        if has_voices:
            voice_project = TimelineProject()
            voice_project.data = copy.deepcopy(frozen.data)
            for layer in voice_project.data['tracks']:
                layer['mute'] = layer['id'] not in voices
                layer['solo'] = False
            voice_project.data['clips'] = [c for c in voice_project.data['clips'] if c['track_id'] in voices and c['gain'] > 0]
            voice_wav = run / 'voices.wav'
            voice_project.render(voice_wav, cancel_event=cancel)
            voice_pcm, _ = sf.read(voice_wav, dtype='int16', always_2d=True)
            if not np.any(voice_pcm):
                raise ValueError('As vozes estão silenciosas; publicação recusada.')
        wav = run / 'scene.wav'
        mix_project = copy.deepcopy(frozen)
        mix_project.data['clips'] = [c for c in mix_project.data['clips'] if c['track_id'] in active and c['gain'] > 0]
        rendered = mix_project.render(wav, cancel_event=cancel)
        if rendered['clipped_samples']:
            raise ValueError('A mixagem satura; reduza os ganhos antes de publicar.')
        pcm, rate = sf.read(wav, dtype='int16', always_2d=True)
        if not np.any(pcm):
            raise ValueError('A mixagem está silenciosa; publicação recusada.')
        if len(pcm) > native.frames:
            raise ValueError('Render ultrapassa a duração nativa.')
        pcm = np.pad(pcm, ((0, native.frames-len(pcm)), (0, 0)))
        sf.write(wav, pcm, rate, subtype='PCM_16')
        ogg = run / (track + '.ogg')
        _run(ffmpeg, ['-v','error','-y','-i',wav,'-c:a','libvorbis','-q:a','9',ogg], run, run / 'encode.log')
        encoded = sf.info(ogg)
        if (encoded.frames, encoded.samplerate, encoded.channels) != native_shape:
            raise ValueError('A codificação mudou o formato/duração nativa.')
        expected = {name: sha256_arquivo(path) for name, path in sources.items()}
        expected[track] = sha256_arquivo(ogg)
        signature = _signature(ogg)
        for name, path in sources.items():
            ini.set(name, 'filename', str(ogg if name == track else path))
        recipe = run / 'import.ini'
        with recipe.open('w', encoding='utf-8') as out:
            ini.write(out)
        report(2, 'Reconstruindo e conferindo todas as faixas')
        _run(exe, ['-i', staged, recipe, staged_lookup], export / container, run / 'import.log')
        validate_lookup(lookup.read_bytes(), staged_lookup.read_bytes(), 8 if container == 'CUTSCENE' else 4)
        roundtrip = run / 'roundtrip'; roundtrip.mkdir()
        _run(exe, ['-e', staged, roundtrip], run, run / 'roundtrip.log')
        _, checked = _tracks(roundtrip / container, count)
        for name, path in checked.items():
            check()
            if name != track and sha256_arquivo(path) != expected[name]:
                raise ValueError('O repack alterou bytes da faixa ' + name)
        if _signature(checked[track]) != signature:
            raise ValueError('A faixa publicada não conserva o PCM codificado.')
        proof_path = _proof_path(root, mod)
        old_proof_bytes = proof_path.read_bytes() if proof_path.is_file() else None
        proof = json.loads(old_proof_bytes) if old_proof_bytes is not None else {'lines': {}}
        new_hash = sha256_arquivo(staged)
        invalidated = []
        for section in ('lines', 'layers'):
            rows = proof.setdefault(section, {})
            for key, row in list(rows.items()):
                if row.get('container') != container:
                    continue
                if row.get('track') == track:
                    invalidated.append(key)
                    del rows[key]
                elif row.get('archive_sha256') == pins[archive]:
                    row['archive_sha256'] = new_hash
        scene_proof = dict(container=container, track=track, archive_sha256=new_hash,
            original_archive_sha256=pins[original_archive], original_sha256=pins[original],
            timeline=frozen.data, render=rendered, native_frames=native.frames,
            sample_rate=native.samplerate, channels=native.channels,
            encoded_sha256=sha256_arquivo(checked[track]), encoded_pcm_signature=signature,
            unchanged_tracks_byte_exact=count-1, invalidated_line_proofs=invalidated,
            gameplay_verified=False, lossy_encoding=True, post_time_stretch=False)
        # Fresh auditions describe the actual mixed result. Keep them separate
        # from independent voice layers so a later installer cannot mistake a
        # multi-speaker crop for an isolated neighbouring take.
        auditions, candidates = {}, {}
        for clip in frozen.data['clips']:
            detail = clip.get('metadata', {})
            if not detail.get('table') or detail.get('gxt_hash') is None:
                continue
            layer = frozen.track(clip['track_id'])
            role = detail.get('window_role')
            priority = 3 if layer['id'] in voices and clip['gain'] > 0 else 2 if role == 'destination' else 1 if role == 'reference' else 0
            if not priority:
                continue
            gid = '%s:%08X' % (detail['table'], detail['gxt_hash'])
            first = round(clip['position'] * native.samplerate / frozen.sample_rate)
            last = round((clip['position'] + frozen.clip_frames(clip)) * native.samplerate / frozen.sample_rate)
            if 0 <= first < last <= native.frames:
                candidates.setdefault(gid, []).append((priority, first, last))
        actual_pcm, actual_rate = sf.read(checked[track], dtype='int16', always_2d=True)
        for gid, windows in candidates.items():
            best = max(row[0] for row in windows)
            unique = {(a, b) for rank, a, b in windows if rank == best}
            if len(unique) != 1:
                continue
            first, last = unique.pop()
            crop = run / (gid.replace(':', '_') + '_audition.wav')
            sf.write(crop, actual_pcm[first:last], actual_rate, subtype='PCM_16')
            auditions[gid] = dict(container=container, track=track, archive_sha256=new_hash,
                audition_path=str(crop), audition_sha256=sha256_arquivo(crop),
                start=first/actual_rate, end=last/actual_rate, mixed_scene=True)
        scene_proof['auditions'] = auditions
        refresh_scene_evidence(proof, container, pins[archive], new_hash, {track})
        proof.setdefault('scenes', {})[container+'/'+track] = dict(archive_sha256=new_hash,
            proof_path=str(run/'scene_proof.json'), auditions=auditions)
        report(3, 'Criando backup e verificando fontes novamente')
        backup = run / 'backup'; backup.mkdir()
        for path in (archive, lookup):
            shutil.copy2(path, backup/path.name)
            if sha256_arquivo(backup/path.name) != pins[path]:
                raise ValueError('Backup não confere; nada foi publicado.')
        # Record the frozen inputs before any game/mod write.
        json_atomico(run/'scene_proof.json', scene_proof)
        report(4, 'Publicando a cena no mod')
        if ga._game_is_running() or any(sha256_arquivo(p) != h for p,h in pins.items()):
            raise ValueError('O jogo abriu ou uma fonte mudou durante a montagem.')
        for source in frozen.data['sources'].values():
            if sha256_arquivo(source['path']) != source['sha256']:
                raise ValueError('Uma fonte da timeline mudou durante a montagem.')
        if (proof_path.read_bytes() if proof_path.is_file() else None) != old_proof_bytes:
            raise ValueError('Os comprovantes mudaram em outra janela.')
        check()
        temp_archive = archive.with_name(archive.name+'.scene-'+uuid.uuid4().hex+'.tmp')
        temp_lookup = lookup.with_name(lookup.name+'.scene-'+uuid.uuid4().hex+'.tmp')
        try:
            shutil.copy2(staged, temp_archive); shutil.copy2(staged_lookup, temp_lookup)
            check()
            if ga._game_is_running() or any(sha256_arquivo(p) != h for p,h in pins.items()):
                raise ValueError('O jogo abriu ou uma fonte mudou antes da publicação.')
            if (proof_path.read_bytes() if proof_path.is_file() else None) != old_proof_bytes:
                raise ValueError('Os comprovantes mudaram antes da publicação.')
            ga._publish_native_pair(temp_archive, archive, temp_lookup, lookup, run/'.rollback')
            try:
                if sha256_arquivo(archive) != new_hash or sha256_arquivo(lookup) != sha256_arquivo(staged_lookup):
                    raise IOError('Os arquivos publicados não conferem.')
                json_atomico(proof_path, proof)
            except Exception:
                restore_archive, restore_lookup = run/'restore_archive', run/'restore_lookup'
                shutil.copy2(backup/archive.name, restore_archive); shutil.copy2(backup/lookup.name, restore_lookup)
                ga._publish_native_pair(restore_archive, archive, restore_lookup, lookup, run/'.restore')
                raise
        finally:
            temp_archive.unlink(missing_ok=True); temp_lookup.unlink(missing_ok=True)
        ga._stream_proof_cache = {}
        # Success remains success even if cancellation arrives after publication.
        if progress:
            progress(5, 5, 'Cena publicada e banco conferido')
        return dict(ok=True, archive_sha256=new_hash, lookup_sha256=sha256_arquivo(lookup),
                    backup=str(backup), proof_path=str(run/'scene_proof.json'),
                    invalidated_line_proofs=invalidated, gameplay_verified=False)
