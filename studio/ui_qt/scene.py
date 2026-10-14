"""Prepare an entire scene from saved native windows and current approved takes.

Layers (top to bottom):
  * Original do jogo            – whole track, muted and locked.
  * Referência EN por fala      – span cloned as voice reference (editable, muted).
  * Destino no jogo por fala    – span replaced on install (editable, muted).
  * Vozes PT-BR por personagem  – approved takes placed at the destination start,
                                  exactly where the installer will mix them.
"""
import uuid
from pathlib import Path
import soundfile as sf
from studio.audio.timeline_project import TimelineProject, digest
from studio.audio.source_routing import audio_route
from studio.audio.stream_catalog import lookup_window, catalog_rows, track_source
from studio.audio.stream_install import destination_window
from studio.core.utils import json_atomico

REFERENCE_LAYER = 'Referências originais por fala'
DESTINATION_LAYER = 'Janelas de substituição'


def selected_original_reference(project, selected_id, selection_frames, table, key):
    """Use an explicit original selection, or the linked utterance, never a whole scene by accident."""
    scene = project.data.get('scene', {})
    if not selected_id or not scene.get('original_path'):
        return None
    selected = project.clip(selected_id)
    original_ids = {clip['source_id'] for clip in project.data['clips']
                    if project.track(clip['track_id'])['role'] == 'original'}
    manual_range = selection_frames[1] > selection_frames[0]
    detail = selected.get('metadata', {})
    same_line = detail.get('table') == table and detail.get('gxt_hash') == key
    reference = selected if selected['source_id'] in original_ids and (manual_range or same_line) else next((clip
        for clip in project.data['clips'] if clip.get('metadata', {}).get('window_role') == 'reference'
        and clip['metadata'].get('table') == table and clip['metadata'].get('gxt_hash') == key), None)
    if not reference:
        return None
    source = project.data['sources'][reference['source_id']]
    first, last = reference['source_in'], reference['source_out']
    if manual_range:
        first += round(max(0, selection_frames[0]-reference['position']) * source['sample_rate'] / project.sample_rate)
        last = min(last, reference['source_in'] + round((selection_frames[1]-reference['position']) *
            source['sample_rate'] / project.sample_rate))
    if last <= first:
        raise ValueError('O intervalo selecionado não cruza a fala original.')
    return dict(source_path=scene['original_path'], source_sha256=scene['original_sha256'], start_frame=first, end_frame=last)


def _scene_source(context, selected, route):
    """Original track signature: the selected line's verified window, else the catalog."""
    if selected is not None:
        try:
            chosen = lookup_window(selected['info'], context.project_root)
            return dict(source_path=chosen['source_path'], source_sha256=chosen['source_sha256'])
        except (ValueError, KeyError, OSError):
            pass
    return track_source(context.project_root, route['container'], route['track'])


def _decoded(context, source, sha, cancel):
    cache = context.project_root / '.cache/qt_timeline_sources'
    cache.mkdir(parents=True, exist_ok=True)
    decoded = cache / (sha + '.wav')
    if not decoded.is_file():
        data, rate = sf.read(source, dtype='int16', always_2d=True)
        temp = cache / (uuid.uuid4().hex + '.wav')
        try:
            sf.write(temp, data, rate, subtype='PCM_16')
            if cancel.is_set():
                raise InterruptedError('Preparação cancelada.')
            temp.replace(decoded)
            json_atomico(decoded.with_suffix('.json'), dict(source=str(source), source_sha256=digest(source),
                decoded_sha256=digest(decoded), conversion='decoded_PCM16_no_stretch_no_trim'))
        finally:
            temp.unlink(missing_ok=True)
    else:
        import json
        proof = json.loads(decoded.with_suffix('.json').read_text(encoding='utf-8'))
        if proof['source_sha256'] != digest(source) or proof['decoded_sha256'] != digest(decoded):
            raise ValueError('O cache da timeline mudou; a cena original foi preservada.')
    return decoded


def _reference_window(record, project_root, sha):
    """(start, end, status, reason, authority). Unavailable lines keep their tentative span."""
    try:
        window = lookup_window(record['info'], project_root)
        if window['source_sha256'] != sha:
            raise ValueError('Fonte diferente da cena selecionada.')
        return float(window['start']), float(window['end']), window['status'], None, window['timing_authority']
    except (ValueError, KeyError, OSError) as exc:
        reason = str(exc)
    for row in catalog_rows(record['info'], project_root):
        if row.get('source_sha256') == sha and row.get('start') is not None and row.get('end') is not None:
            return float(row['start']), float(row['end']), 'UNAVAILABLE', reason, 'TENTATIVE_' + str(row.get('timing_authority'))
    return None, None, 'UNAVAILABLE', reason, None


def prepare_scene(context, selected, records, progress, cancel, track=None, scene_route=None):
    if scene_route:
        route = dict(family='streams', container=scene_route['container'], track=scene_route['track'])
    elif track:
        route = dict(family='streams', container='CUTSCENE', track=track)
    else:
        route = audio_route(selected['info'])
        if not route or route['family'] != 'streams':
            raise ValueError('Esta fala é um slot separado. Use "Trecho na timeline" ou selecione CUTSCENE/BEATS para abrir a cena completa.')
    from .scene_sources import full_track
    native = full_track(context, route['container'], route['track'], progress=progress, cancel=cancel)
    try:
        chosen = _scene_source(context, selected, route)
        if not Path(chosen['source_path']).is_file():
            chosen = native
    except (ValueError, OSError, KeyError):
        chosen = native
    alignment_source = Path(chosen['source_path'])
    if digest(alignment_source) != chosen['source_sha256']:
        raise ValueError('A faixa original mudou desde o alinhamento. Atualize os vínculos primeiro.')
    source = Path(native['source_path'])
    if abs(sf.info(alignment_source).duration - sf.info(source).duration) > .05:
        raise ValueError('A referência alinhada e o original têm durações diferentes; preserve o rascunho e atualize os vínculos.')
    progress('Preparando a cena completa e seus intervalos originais.')
    decoded = _decoded(context, source, native['source_sha256'], cancel)
    project = TimelineProject()
    base = project.add_wav(decoded, role='original', position=0, native_format=True,
        metadata=dict(name='Original completo · ' + route['track'], reference=str(source)))
    project.update_track(project.clip(base)['track_id'], name='Original do jogo · ' + route['track'])
    project.data['scene'] = dict(container=route['container'], track=route['track'],
        original_path=str(source), original_sha256=native['source_sha256'],
        timing_authority='saved_native_windows_not_new_alignment', gameplay_verified=False,
        pt_takes_positioned_at='install_destination_window')
    project.data['scene'].update(original_archive_path=native['archive_path'], original_archive_sha256=native['archive_sha256'])
    project.data['scene'].update(native_source_path=native['source_path'], native_source_sha256=native['source_sha256'])
    installed = full_track(context, route['container'], route['track'], installed=True, progress=progress, cancel=cancel)
    if installed:
        installed_wav = _decoded(context, Path(installed['source_path']), installed['source_sha256'], cancel)
        installed_info = sf.info(installed_wav)
        project.data.update(sample_rate=installed_info.samplerate, channels=installed_info.channels)
        current = project.add_wav(installed_wav, role='ambience', position=0, native_format=True,
            metadata=dict(name='Áudio atual do mod', scene_background=True))
        project.update_track(project.clip(current)['track_id'], name='Cena atual do mod', locked=False, mute=False)
        project.data['scene'].update(installed_archive_sha256=installed['archive_sha256'],
            installed_mod=context.game_audio.mod_ativo(), background_clip_id=current,
            installed_format=dict(frames=installed_info.frames, sample_rate=installed_info.samplerate, channels=installed_info.channels))
        # Full original is available for comparison and manual editing, never mixed by accident.
        project.clip(base)['metadata']['scene_background'] = True
        project.data['scene']['background_mode'] = 'installed'
    else:
        project.clip(base)['metadata']['scene_background'] = True
        project.data['scene']['background_mode'] = 'silence'
    total = project.data['sources'][project.clip(base)['source_id']]['frames']
    source_rate = project.data['sources'][project.clip(base)['source_id']]['sample_rate']
    rate = project.sample_rate
    refs = project.add_track(REFERENCE_LAYER, 'original')
    destinations = project.add_track(DESTINATION_LAYER, 'original')
    for layer in (refs, destinations):
        project.update_track(layer, mute=True, locked=False)
    unknown, unroutable, imported, linked, manual_lines = [], [], 0, 0, 0
    character_tracks = {}

    def clipped(start, end):
        first, last = round(start * source_rate), round(end * source_rate)
        if first < 0 or last <= first or last > total:
            raise ValueError('Intervalo fora dos limites da faixa original.')
        return first, last

    for record in records:
        if cancel.is_set():
            raise InterruptedError('Preparação cancelada.')
        try:
            candidate_route = audio_route(record.get('info', {}))
        except ValueError as exc:
            # The catalogue covers the whole game. An ambiguous unrelated row
            # cannot prevent opening a scene with a verified selected route.
            unroutable.append(dict(line_id=record['line_id'], reason=str(exc)))
            continue
        if not candidate_route or (candidate_route.get('container'), candidate_route.get('track')) != (route['container'], route['track']):
            continue
        info = record.get('info', {})
        common = dict(table=record['table'], gxt_hash=record['hash'], name=record['name'],
            text=record['text'], record_info=info, gid=info.get('gxt_stable_id'),
            character=info.get('speaker_identity', ''), reference=str(source))
        try:
            start, end, status, reason, authority = _reference_window(record, context.project_root, chosen['source_sha256'])
            destination = None
            try:
                window = destination_window(info, context.project_root)
                destination = (float(window['start']), float(window['end']), window['authority'].get('clock'))
            except (ValueError, KeyError, OSError, TypeError):
                destination = None
            if start is None and destination is None:
                # .cut is caption timing, not a verified voice alignment. Still
                # show the caption in the editor without inventing a speaker.
                if info.get('caption_start_ms') is not None and info.get('caption_duration_ms'):
                    start = float(info['caption_start_ms']) / 1000
                    end = start + float(info['caption_duration_ms']) / 1000
                    authority = 'NATIVE_CUT_CAPTION_ONLY'
                    status = 'UNAVAILABLE'
                else:
                    raise ValueError(reason or 'Sem janela de referência nem de destino.')
            if start is not None:
                first, last = clipped(start, end)
                clip = project.duplicate(base, round(start * rate), refs)
                project.update_clip(clip, source_in=first, source_out=last, metadata=dict(common,
                    window_role='reference', timing_status=status, timing_reason=reason, timing_authority=authority))
            if destination is not None:
                first, last = clipped(destination[0], destination[1])
                clip = project.duplicate(base, round(destination[0] * rate), destinations)
                project.update_clip(clip, source_in=first, source_out=last, metadata=dict(common,
                    window_role='destination', timing_status='MANUAL_WINDOW' if destination[2] == 'MANUAL_DESTINATION_WINDOW' else 'CAPTION_WINDOW',
                    timing_authority=destination[2]))
            manual_lines += status == 'MANUAL_WINDOW' or bool(destination and destination[2] == 'MANUAL_DESTINATION_WINDOW')
            if status == 'UNAVAILABLE':
                unknown.append(dict(line_id=record['line_id'], reason=reason))
            else:
                linked += 1
            approved = getattr(context, 'chosen_path', context.approved_path)(record['line_id'], record['text'])
            if approved:
                character = common['character'] or 'Vozes PT-BR · personagem não identificado'
                if character not in character_tracks:
                    character_tracks[character] = project.add_track(character, 'pt')
                    # Existing takes are offered as alternatives. Opening a scene
                    # must not replace the user's already installed edits.
                    project.update_track(character_tracks[character], mute=bool(installed))
                at = round(destination[0] * rate) if destination is not None else round(start * rate)
                project.add_wav(approved, track_id=character_tracks[character], position=at, role='pt',
                                metadata=dict(common, window_role='take', timing_status=status,
                                    available_take=True, replace_end=destination[1] if destination else end))
                imported += 1
        except (ValueError, KeyError, OSError) as exc:
            unknown.append(dict(line_id=record['line_id'], reason=str(exc)))
    project.data['scene']['unavailable_windows'] = unknown
    project.data['scene']['unroutable_catalog_records'] = unroutable
    project.validate()
    folder = context.project_root / 'workspace/qt_scenes'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (route['container'] + '_' + route['track'] + '_' + uuid.uuid4().hex + '.timeline.json')
    project.save(path)
    return dict(path=path, linked=linked, approved=imported, unavailable=len(unknown), manual=manual_lines,
                track=route['track'])
