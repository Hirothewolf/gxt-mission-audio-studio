"""Install approved utterances into native stream banks, never into SCRIPT slots."""
import configparser
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from studio.audio.source_routing import audio_route
from studio.core.utils import json_atomico, sha256_arquivo


def destination_window(info, root):
    """Destination clock is separate from the original voice-cloning reference clock."""
    route = audio_route(info)
    if not route or route['family'] != 'streams':
        raise ValueError('Esta fala não está vinculada a uma faixa contínua.')
    if route['container'] == 'BEATS':
        # Supplied hypertranslated mod windows, used by the installed opening assembly.
        manifest = Path(root) / 'reports/studio_rework_20261008/omnivoice_opening_v1/input_manifest.json'
        data = json.loads(manifest.read_text(encoding='utf-8'))
        rows = [r for r in data['records'] if r['id'] == info.get('stable_audio_id')
                and r['gxt_id'] == info.get('gxt_stable_id') and r['track'] == 'BEATS_010']
        if len(rows) != 1:
            raise ValueError('Falta um único intervalo de destino conferido para esta fala BEATS.')
        start, end = float(rows[0]['start']), float(rows[0]['end'])
        authority = {'path': str(manifest), 'sha256': sha256_arquivo(manifest),
                     'clock': 'SUPPLIED_HYPERTRANSLATED_MOD_WINDOW'}
    else:
        from studio.audio import window_overrides
        manual = window_overrides.get(root, info.get('gxt_stable_id'))
        if manual and manual.get('destination') and (manual['container'], manual['track']) == (route['container'], route['track']):
            # A listening-based destination replaces the caption clock for this line only.
            start, end = manual['destination']['start'], manual['destination']['end']
            authority = {'clock': 'MANUAL_DESTINATION_WINDOW', 'source_sha256': manual['source_sha256'],
                         'path': str(window_overrides.path_for(root)), 'updated_at': manual.get('updated_at')}
        else:
            if info.get('caption_start_ms') is None or info.get('caption_duration_ms') is None:
                raise ValueError('Esta fala não tem janela de legenda .cut. Abra a cena e grave a janela de destino manualmente.')
            # CUT captions anchor installed cutscene speech, not the ASR reference crop.
            start = float(info['caption_start_ms']) / 1000
            end = start + float(info['caption_duration_ms']) / 1000
            authority = {'clock': 'NATIVE_CUT_CAPTION', 'cut_metadata': info.get('cut_metadata')}
    if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start:
        raise ValueError('O intervalo de destino desta fala é inválido.')
    return dict(route, start=start, end=end, authority=authority)


def replace_regions(audio, rate, entries, restore_layers=()):
    """Clear selected regions first, then mix whole takes (including overlaps)."""
    original = np.asarray(audio, dtype=np.float32)
    if original.ndim != 2 or not np.isfinite(original).all():
        raise ValueError('A faixa de destino contém amostras inválidas.')
    rendered = original.copy()
    prepared = []
    mask = np.zeros(len(original), dtype=bool)
    for entry in entries:
        start, end = round(entry['start'] * rate), round(entry['end'] * rate)
        if start < 0 or end <= start or end > len(original):
            raise ValueError('O intervalo de destino ultrapassa a faixa atual do mod.')
        take, sr = sf.read(entry['path'], dtype='float32', always_2d=True)
        if not len(take) or not np.isfinite(take).all():
            raise ValueError('O take aprovado está vazio ou inválido.')
        if sr != rate:
            divisor = math.gcd(sr, rate)
            take = resample_poly(take, rate // divisor, sr // divisor, axis=0)
        if take.shape[1] != original.shape[1]:
            take = np.repeat(take.mean(axis=1, keepdims=True), original.shape[1], axis=1)
        last = start + len(take)
        if last > len(original):
            raise ValueError('O take completo ultrapassa o fim da faixa; ajuste a duração nativa antes de aplicar.')
        # Never clear a neighbour just because a newly approved take runs longer.
        mask[start:end] = True
        for first, stop in entry.get('previous_regions', []):
            first, stop = round(first * rate), round(stop * rate)
            if first < 0 or stop > len(original) or stop <= first:
                raise ValueError('O comprovante da instalação anterior tem intervalo inválido.')
            mask[first:stop] = True
        prepared.append((entry, start, last, take))
    rendered[mask] = 0
    # Reconstruct accepted neighbouring voices removed by an overlapping edit,
    # only inside the cleared region (outside already contains their old mix).
    for layer in restore_layers:
        take, sr = sf.read(layer['path'], dtype='float32', always_2d=True)
        if not len(take) or not np.isfinite(take).all():
            raise ValueError('O áudio de uma fala vizinha instalada está inválido.')
        if sr != rate:
            divisor = math.gcd(sr,rate); take=resample_poly(take,rate//divisor,sr//divisor,axis=0)
        if take.shape[1] != original.shape[1]:
            take=np.repeat(take.mean(axis=1,keepdims=True),original.shape[1],axis=1)
        first=round(layer['start']*rate);last=first+len(take)
        if first<0 or last>len(original):raise ValueError('Fala vizinha fora da faixa.')
        selected=mask[first:last]
        rendered[first:last] += take * selected[:,None]
    touched = mask.copy()
    for entry, start, last, take in prepared:
        rendered[start:last] += take
        touched[start:last] = True
    # Limit only modified samples; never normalize the entire track.
    peak = np.max(np.abs(rendered), axis=1)
    gain = np.minimum(1., .98 / np.maximum(peak, 1e-12))
    rendered[touched] *= gain[touched, None]
    if not np.array_equal(original[~touched], rendered[~touched]):
        raise AssertionError('A montagem alterou amostras fora dos trechos selecionados.')
    return rendered, [{'line_id': e['line_id'], 'start': first / rate, 'end': last / rate,
                       'clear_start': e['start'], 'clear_end': e['end'],
                       'placed_frames': len(take), 'speech_frames_cut': 0,
                       'post_time_stretch': False} for e, first, last, take in prepared]


def validate_lookup(before, after, index):
    if len(before) != len(after) or len(before) % 12:
        raise ValueError('TrakLkup.dat mudou de tamanho ou possui estrutura inválida.')
    for offset in range(0, len(before), 12):
        a, b = before[offset:offset + 12], after[offset:offset + 12]
        if a[0] != b[0] or (a[0] != index and a != b):
            raise ValueError('O repack alterou outro banco em TrakLkup.dat.')


def _run(executable, args, cwd, log):
    result = subprocess.run([str(executable), *map(str, args)], cwd=str(cwd),
                            capture_output=True, timeout=3600)
    Path(log).write_bytes(result.stdout + b'\n' + result.stderr)
    if result.returncode:
        raise RuntimeError('A ferramenta de áudio não conseguiu reconstruir a faixa. Consulte %s.' % log)


def _signature(path):
    pcm, rate = sf.read(path, dtype='int16', always_2d=True)
    return rate, pcm.shape, hashlib.sha256(pcm.tobytes()).hexdigest()


def _proof_path(root, mod):
    key = hashlib.sha256(str(mod).encode()).hexdigest()[:16]
    return Path(root) / 'workspace/stream_installs' / (key + '.json')


def _prior_layers(root, container, digest, proof):
    """Recover the real installed positions, including older verified production."""
    layers={k:dict(v) for k,v in proof.get('layers',{}).items()
            if v.get('container')==container and v.get('archive_sha256')==digest}
    for gid,row in proof.get('lines',{}).items():
        if row.get('container')==container and row.get('archive_sha256')==digest:
            layers[container+'/'+row['track']+'/'+gid]=dict(row,gid=gid)
    if layers or container!='CUTSCENE':return layers
    report=Path(root)/'deliverables/cutscenes_all_recovered_safe_20261011_v1/staging_report.json'
    if not report.is_file():return layers
    data=json.loads(report.read_text(encoding='utf-8'))
    if data.get('staged_assets',{}).get('audio/streams/CUTSCENE')!=digest:return layers
    report_hash=sha256_arquivo(report)
    for row in data.get('inserted',[]):
        gid=row['gid'];track=row['track']
        layers[container+'/'+track+'/'+gid]=dict(gid=gid,container=container,track=track,
            start=float(row['start']),end=float(row['start'])+float(row['duration']),
            source_take_path=row['path'],source_take_sha256=row['sha256'],archive_sha256=digest,
            authority={'path':str(report),'sha256':report_hash,'clock':'VERIFIED_INSTALLED_PRODUCTION_LAYER'})
    return layers


def refresh_scene_evidence(proof, container, old_hash, new_hash, changed_tracks):
    """Keep whole-scene auditions only for tracks proven unchanged by a repack."""
    for key, scene in list(proof.get('scenes', {}).items()):
        if not key.startswith(container + '/') or scene.get('archive_sha256') != old_hash:
            continue
        if key.split('/', 1)[1] in changed_tracks:
            del proof['scenes'][key]
            continue
        scene['archive_sha256'] = new_hash
        for row in scene.get('auditions', {}).values():
            row['archive_sha256'] = new_hash


def installed_stream_path(info, project_root, game_audio, mod=None):
    """Expose an audition of the actual re-exported installed track, with live bank pin."""
    mod = mod or game_audio.mod_ativo()
    route = audio_route(info)
    if not route or route['family'] != 'streams':
        return None
    proof = _proof_path(project_root, mod)
    if not proof.is_file():
        return None
    data = json.loads(proof.read_text(encoding='utf-8'))
    row = data.get('lines', {}).get(info.get('gxt_stable_id'))
    if not row or (row.get('container'),row.get('track')) != (route['container'],route['track']):
        scene = data.get('scenes', {}).get(route['container']+'/'+route['track'], {})
        row = scene.get('auditions', {}).get(info.get('gxt_stable_id'))
        if not row or (row.get('container'),row.get('track')) != (route['container'],route['track']):
            return None
    archive = game_audio._safe_mod_dir(mod) / 'audio/streams' / route['container']
    st = archive.stat()
    # Cache expensive hash checks only while the file signature remains identical.
    cache = getattr(game_audio, '_stream_proof_cache', {})
    stamp = (str(archive), st.st_size, st.st_mtime_ns, row['archive_sha256'])
    if stamp not in cache:
        cache[stamp] = sha256_arquivo(archive) == row['archive_sha256']
        game_audio._stream_proof_cache = cache
    path = Path(row['audition_path']).resolve()
    path.relative_to(Path(project_root).resolve())
    if cache[stamp] and path.is_file() and sha256_arquivo(path) == row['audition_sha256']:
        return path
    return None


def install_stream_rows(store, rows, game_audio, mod, progress=None):
    result = {'installed': [], 'skipped': [], 'errors': [], 'banks': [], 'installedpaths': {}}
    groups = defaultdict(list)
    root = Path(store.root)
    for row in rows:
        try:
            path = store.approved_path(row['line_id'], row['text'])
            if not path or str(path) != row.get('path') or sha256_arquivo(path) != row.get('sha256'):
                raise ValueError('O take aprovado mudou ou não corresponde ao texto atual; atualize a lista.')
            window = destination_window(row['info'], root)
            if row['line_id'].replace('|', ':').upper() != row['info'].get('gxt_stable_id', '').upper():
                raise ValueError('O destino pertence a outra chave GXT.')
            entry = dict(row, **window)
            groups[window['container']].append(entry)
        except (ValueError, OSError, KeyError) as exc:
            result['skipped'].append({'line_id': row['line_id'], 'reason': str(exc)})
    for container, entries in groups.items():
        try:
            response = _install_bank(root, store, entries, game_audio, mod, container, progress)
            result['banks'].append({'pak': container, 'result': response})
            result['installed'].extend(response['installed'])
            result['installedpaths'].update(response['installedpaths'])
            result['backup'] = response['backup']
        except (ValueError, OSError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
            result['errors'].append({'bank': container, 'reason': str(exc)})
    return result


def _install_bank(root, store, entries, ga, mod, container, progress):
    mod_dir = ga._safe_mod_dir(mod)
    archive = mod_dir / 'audio/streams' / container
    lookup = mod_dir / 'audio/CONFIG/TrakLkup.dat'
    for target in (archive, lookup):
        if not target.is_file() or not ga._dentro(mod_dir, target.resolve()):
            raise ValueError('Falta o banco nativo ou TrakLkup.dat dentro do próprio mod.')
    if ga._game_is_running():
        raise ValueError('Feche o GTA antes de aplicar os áudios ao mod.')
    exe = ga.saat().get('stream')
    if not exe:
        raise ValueError('A ferramenta SAAT de faixas contínuas não foi encontrada.')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('O conversor de áudio ffmpeg não foi encontrado.')
    run = root / 'reports/editor_stream_installs' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    with ga._native_install_lock():
        pins = {str(p): sha256_arquivo(p) for p in (archive, lookup)}
        proof_path = _proof_path(root, mod)
        old_proof = json.loads(proof_path.read_text(encoding='utf-8')) if proof_path.is_file() else {'lines': {}}
        layers=_prior_layers(root,container,pins[str(archive)],old_proof)
        by_track = defaultdict(list)
        seen = set()
        for entry in entries:
            identity = entry['info']['gxt_stable_id']
            if identity in seen:
                raise ValueError('Há duas substituições para a mesma fala; atualize a lista.')
            seen.add(identity)
            previous = layers.get(container+'/'+entry['track']+'/'+identity)
            if previous and previous['archive_sha256'] == pins[str(archive)]:
                entry['previous_regions'] = [[previous['start'], previous['end']]]
                if container=='CUTSCENE':
                    entry.update(start=previous['start'],end=previous['end'],authority=previous['authority'])
            elif container=='CUTSCENE':
                raise ValueError('Esta fala de cutscene ainda não tem posição anterior comprovada no banco atual. '
                                 'Atualize o mapeamento no estúdio antes de substituir; o take foi preservado.')
            by_track[entry['track']].append(entry)
        with tempfile.TemporaryDirectory(prefix='stream_', dir=str(run)) as temporary:
            work = Path(temporary); staged = work / container; staged_lookup = work / 'TrakLkup.dat'
            shutil.copy2(archive, staged); shutil.copy2(lookup, staged_lookup)
            export = work / 'source'; export.mkdir()
            if progress: progress(0, 3, 'Lendo o banco ' + container + ' atualmente instalado')
            _run(exe, ['-e', staged, export], work, run / 'export.log')
            base = export / container
            ini = configparser.ConfigParser(interpolation=None); ini.optionxform = str
            ini.read(base / 'stream_import.ini', encoding='utf-8-sig')
            sections = [s for s in ini.sections() if s.startswith('Track_')]
            expected_count = 10 if container == 'BEATS' else 141
            if len(sections) != expected_count or any(s not in sections for s in by_track):
                raise ValueError('A quantidade de faixas nativas ou o destino não corresponde ao banco.')
            source = {s: (base / ini.get(s, 'filename').replace('\\', '/')).resolve() for s in sections}
            for p in source.values():
                p.relative_to(base.resolve())
                if not p.is_file(): raise ValueError('Exportação nativa incompleta.')
            changed, placements = {}, {}
            for track, members in by_track.items():
                audio, rate = sf.read(source[track], dtype='float32', always_2d=True)
                selected_ids={m['info']['gxt_stable_id'] for m in members}
                restorers=[]
                for previous in layers.values():
                    gid=previous['gid']
                    if (gid in selected_ids or previous.get('container')!=container or previous.get('track')!=track
                            or previous['archive_sha256']!=pins[str(archive)]):continue
                    overlaps=any(max(previous['start'],min([m['start']]+[r[0] for r in m.get('previous_regions',[])])) <
                                 min(previous['end'],max([m['end']]+[r[1] for r in m.get('previous_regions',[])])) for m in members)
                    if not overlaps:continue
                    saved=Path(previous['source_take_path'])
                    saved.resolve().relative_to(root.resolve())
                    if sha256_arquivo(saved)!=previous['source_take_sha256']:
                        raise ValueError('Falta a fonte de uma fala vizinha sobreposta; nada foi aplicado.')
                    restorers.append({'path':str(saved),'start':previous['start']})
                rendered, placement = replace_regions(audio, rate, members, restorers)
                wav = work / (track + '.wav'); ogg = work / (track + '.ogg')
                sf.write(wav, rendered, rate, subtype='PCM_16')
                _run(ffmpeg, ['-v','error','-y','-i',wav,'-c:a','libvorbis','-q:a','9',ogg], work, run / (track + '_encode.log'))
                a, b = sf.info(wav), sf.info(ogg)
                if (a.frames,a.samplerate,a.channels) != (b.frames,b.samplerate,b.channels):
                    raise ValueError('A codificação mudou a duração ou o formato da faixa.')
                changed[track] = ogg; placements[track] = placement
            expected = {}
            for s in sections:
                p = changed.get(s, source[s]); expected[s] = (sha256_arquivo(p), _signature(p))
                ini.set(s, 'filename', str(p))
            recipe = work / 'import.ini'
            with recipe.open('w', encoding='utf-8') as handle: ini.write(handle)
            if progress: progress(1, 3, 'Reconstruindo ' + container)
            _run(exe, ['-i', staged, recipe, staged_lookup], base, run / 'import.log')
            validate_lookup(lookup.read_bytes(), staged_lookup.read_bytes(), 4 if container == 'BEATS' else 8)
            rt = work / 'roundtrip'; rt.mkdir()
            _run(exe, ['-e', staged, rt], work, run / 'roundtrip.log')
            verified = []
            for s in sections:
                p = rt / container / (s + '.ogg')
                exact = sha256_arquivo(p) == expected[s][0]
                if (s not in changed and not exact) or (not exact and _signature(p) != expected[s][1]):
                    raise ValueError('A conferência do banco reconstruído falhou em ' + s)
                verified.append({'track': s, 'changed': s in changed, 'ogg_byte_exact': exact, 'pcm_verified': True})
            new_rows = {}
            for track, members in by_track.items():
                pcm, rate = sf.read(rt / container / (track + '.ogg'), dtype='float32', always_2d=True)
                for member, placement in zip(members, placements[track]):
                    crop = run / (member['line_id'].replace('|','_') + '_installed.wav')
                    sf.write(crop, pcm[round(placement['start'] * rate):round(placement['end'] * rate)], rate, subtype='PCM_16')
                    new_rows[member['info']['gxt_stable_id']] = dict(placement, container=container, track=track,
                        source_take_path=member['path'],source_take_sha256=member['sha256'], audition_path=str(crop), audition_sha256=sha256_arquivo(crop),
                        archive_sha256=sha256_arquivo(staged), authority=member['authority'])
                # A neighbouring edit can alter what is heard inside an earlier
                # audition. Refresh those crops from the actual roundtrip too.
                for gid,previous in old_proof['lines'].items():
                    if (gid in new_rows or previous.get('container')!=container or previous.get('track')!=track
                            or previous['archive_sha256']!=pins[str(archive)]):continue
                    crop=run/(previous['line_id'].replace('|','_')+'_installed.wav')
                    sf.write(crop,pcm[round(previous['start']*rate):round(previous['end']*rate)],rate,subtype='PCM_16')
                    previous.update(audition_path=str(crop),audition_sha256=sha256_arquivo(crop))
            if ga._game_is_running(): raise ValueError('O GTA abriu durante a montagem; nada foi publicado.')
            if any(sha256_arquivo(p) != digest for p,digest in pins.items()):
                raise ValueError('O banco do mod mudou durante a montagem; aplicação cancelada para preservar suas alterações.')
            for member in entries:
                if store.approved_path(member['line_id'],member['text']) != Path(member['path']):
                    raise ValueError('A aprovação mudou durante a montagem; aplicação cancelada.')
            if store.path.is_file():
                from studio.projects.review import ReviewStore
                fresh=ReviewStore(root)
                if any(fresh.approved_path(m['line_id'],m['text'])!=Path(m['path']) for m in entries):
                    raise ValueError('Uma aprovação foi alterada em outra janela; aplicação cancelada.')
            backup = run / 'backup'; (backup/'audio/streams').mkdir(parents=True); (backup/'audio/CONFIG').mkdir(parents=True)
            shutil.copy2(archive, backup/'audio/streams'/container); shutil.copy2(lookup, backup/'audio/CONFIG/TrakLkup.dat')
            if sha256_arquivo(backup/'audio/streams'/container) != pins[str(archive)] or sha256_arquivo(backup/'audio/CONFIG/TrakLkup.dat') != pins[str(lookup)]:
                raise ValueError('O backup não passou na conferência; nada foi publicado.')
            # Unchanged-track proofs remain current after this bank's repack.
            for previous in old_proof['lines'].values():
                if previous.get('container') == container and previous['archive_sha256'] == pins[str(archive)]:
                    previous['archive_sha256'] = sha256_arquivo(staged)
            all_layers=old_proof.setdefault('layers',{})
            for key,previous in layers.items():
                previous['archive_sha256']=sha256_arquivo(staged)
                all_layers[key]=previous
            for gid,row in new_rows.items():
                all_layers[container+'/'+row['track']+'/'+gid]=dict(row,gid=gid)
            old_proof['lines'].update(new_rows)
            refresh_scene_evidence(old_proof, container, pins[str(archive)], sha256_arquivo(staged), set(by_track))
            staged_archive = archive.with_name(archive.name + '.studio-' + uuid.uuid4().hex + '.tmp')
            staged_table = lookup.with_name(lookup.name + '.studio-' + uuid.uuid4().hex + '.tmp')
            try:
                shutil.copy2(staged, staged_archive); shutil.copy2(staged_lookup, staged_table)
                ga._publish_native_pair(staged_archive, archive, staged_table, lookup, run / '.rollback')
                try:
                    if sha256_arquivo(archive) != sha256_arquivo(staged) or sha256_arquivo(lookup) != sha256_arquivo(staged_lookup):
                        raise IOError('Os arquivos publicados não passaram na conferência de hash.')
                    json_atomico(proof_path, old_proof)
                except Exception:
                    shutil.copy2(backup/'audio/streams'/container, archive); shutil.copy2(backup/'audio/CONFIG/TrakLkup.dat', lookup)
                    raise
            finally:
                staged_archive.unlink(missing_ok=True); staged_table.unlink(missing_ok=True)
            response = {'ok': True, 'installed': [m['line_id'] for m in entries], 'backup': str(backup),
                        'installedpaths': {v['line_id']:v['audition_path'] for v in new_rows.values()},
                        'archive_sha256':sha256_arquivo(archive), 'lookup_sha256':sha256_arquivo(lookup),
                        'track_checks':verified, 'placements':placements,
                        'outside_regions_preserved_before_vorbis_encoding':True,
                        'lossy_encoding_of_changed_tracks':True, 'gameplay_verified':False}
            json_atomico(run/'installation_report.json',response)
            if progress: progress(3, 3, 'Banco aplicado e conferido: ' + container)
            return response
