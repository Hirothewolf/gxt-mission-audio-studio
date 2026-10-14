"""Owned, reversible dialogue snapshots independent of audio container type.

SCRIPT, CUTSCENE and imported files describe provenance, not speaker count.
Each manually authored region owns its text, reference and replacement.
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from studio.audio import cutscene_studio as audio
from studio.core.utils import json_atomico
from studio.script.native_index import digest


def _root(project_root):
    return (Path(project_root) / 'workspace' / 'dialogue_studio').resolve()


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(parent.resolve())
    except (ValueError, TypeError):
        raise ValueError('Arquivo fora do armazenamento deste projeto.')


def _identity(value=None):
    value = value or uuid.uuid4().hex
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Identificador de rascunho/trecho inválido.')
    return value


def _copy_snapshot(path, folder, expected=None, cancel_event=None):
    source = Path(path).resolve()
    audio._check_cancel(cancel_event)
    signature = digest(source)
    if expected is not None and signature != expected:
        raise ValueError('Áudio mudou desde que foi selecionado: ' + source.name)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix not in ('.wav', '.ogg', '.mp3', '.flac', '.m4a'):
        raise ValueError('Formato de áudio não suportado; use WAV, OGG, MP3, FLAC ou M4A.')
    target = folder / (signature + suffix)
    if target.exists():
        if digest(target) != signature:
            raise ValueError('Snapshot do áudio foi alterado; preserve-o e importe uma fonte íntegra.')
        return target, signature
    part = folder / (uuid.uuid4().hex + '.part')
    try:
        with source.open('rb') as incoming, part.open('xb') as outgoing:
            while data := incoming.read(1024 * 1024):
                audio._check_cancel(cancel_event)
                outgoing.write(data)
        if digest(part) != signature or digest(source) != signature:
            raise ValueError('A origem mudou durante a importação.')
        audio._check_cancel(cancel_event)
        # Refuse overwriting an already published asset.
        if target.exists():
            if digest(target) != signature:
                raise ValueError('Snapshot concorrente inválido.')
        else:
            part.rename(target)
    finally:
        part.unlink(missing_ok=True)
    return target, signature


def open_source(project_root, path, source_kind='imported', provenance=None,
                cancel_event=None, on_progress=None):
    """Copy an explicitly selected source before decoding; never infer voice count."""
    kind = str(source_kind).lower()
    if kind not in ('script', 'cutscene', 'imported'):
        raise ValueError('Origem de áudio inválida.')
    audio._progress(on_progress, 'Preservando uma cópia do áudio selecionado…')
    source = Path(path).resolve()
    provenance = dict(provenance or {})
    if provenance.get('native_cache_verified'):
        archive = provenance.get('archive_path')
        if not archive or digest(archive) != provenance.get('archive_sha256'):
            raise ValueError('O arquivo CUTSCENE mudou; atualize o índice e a extração.')
    owned, signature = _copy_snapshot(source, _root(project_root) / 'sources',
                                      expected=provenance.get('source_sha256'), cancel_event=cancel_event)
    track = audio.load_track({'path': owned, 'sha256': signature},
                             cancel_event=cancel_event, on_progress=on_progress)
    track.update(source_kind=kind, source_origin_path=str(source),
                 provenance=provenance, speaker_layout='unreviewed')
    return track


def list_native_sources(project_root, cancel_event=None, on_progress=None):
    """Discover verified original cutscene tracks with scene labels, in a worker."""
    report_path = Path(project_root) / 'workspace' / 'native_audio' / 'index.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    stream = report.get('sources', {}).get('stream') or {}
    audio._check_cancel(cancel_event)
    if not stream.get('path') or digest(stream['path']) != stream.get('sha256'):
        raise ValueError('O arquivo CUTSCENE mudou; atualize o índice e a extração.')
    tracks = audio.list_tracks(project_root, report, cancel_event, on_progress)
    scenes = {}
    for record in report.get('records', []):
        if record.get('pak') != 'CUTSCENE':
            continue
        name = (record.get('stream') or {}).get('track')
        scene = record.get('scene') or record.get('table')
        if name and scene:
            scenes.setdefault(name, set()).add(str(scene))
    result = []
    for track in tracks:
        names = sorted(scenes.get(track['track'], ()))
        result.append({**track, 'source_kind': 'cutscene',
                       'label': '%s · %s' % (track['track'], ', '.join(names) or 'sem cena identificada'),
                       'provenance': {'native_cache_verified': True, 'scenes': names,
                                      'source_sha256': track['sha256'],
                                      'archive_path': track['archive_path'],
                                      'archive_sha256': track['archive_sha256']}})
    return result


def _validate_track(project_root, track):
    _inside(track.get('source_path'), _root(project_root) / 'sources')
    if digest(track['source_path']) != track.get('source_sha256'):
        raise ValueError('Snapshot do áudio foi alterado.')
    if Path(track['source_path']).stem != track['source_sha256']:
        raise ValueError('O nome do snapshot não corresponde ao hash da fonte.')
    if track.get('source_kind') not in ('script', 'cutscene', 'imported'):
        raise ValueError('Origem do rascunho inválida.')


def _validate_segments(track, segments):
    seen = set()
    for segment in segments:
        identity = _identity(segment.get('id'))
        if not segment.get('id') or identity in seen:
            raise ValueError('Trechos precisam ter identificadores únicos.')
        seen.add(identity)
        for field in ('speaker', 'text', 'reference_transcript'):
            if field in segment and not isinstance(segment[field], str):
                raise ValueError('Texto/personagem do trecho inválido.')
        if 'speaker_confirmed' in segment and type(segment['speaker_confirmed']) is not bool:
            raise ValueError('Confirmação de personagem inválida.')
        replacement = segment.get('replacement')
        if replacement:
            audio.validate_replacements(track, [replacement],
                                        [{'start': segment['start'], 'end': segment['end']}], fit=False)
    audio.validate_ranges(track, [{'start': s['start'], 'end': s['end']} for s in segments])


def speaker_layout(segments):
    """Two distinct confirmed manual labels show annotated voices, not diarization."""
    identities = {str(s.get('speaker', '')).strip().casefold() for s in segments
                  if s.get('speaker_confirmed') is True and str(s.get('speaker', '')).strip()}
    return 'multiple_annotated' if len(identities) > 1 else 'unreviewed'


def save_draft(project_root, track_data, segments, draft_id=None,
               cancel_event=None, on_progress=None):
    """Save owned audio and per-region annotations; never approve/install takes."""
    audio._check_cancel(cancel_event)
    _validate_track(project_root, track_data)
    _validate_segments(track_data, segments)
    identity = _identity(draft_id)
    root = _root(project_root)
    stored = []
    for index, segment in enumerate(segments):
        audio._check_cancel(cancel_event)
        audio._progress(on_progress, 'Preservando referências e takes…', index, len(segments))
        row = dict(segment)
        for field in ('reference', 'replacement'):
            asset = segment.get(field)
            if asset:
                copied, signature = _copy_snapshot(asset['path'], root / 'assets', asset['sha256'], cancel_event)
                verified = audio.import_replacement(copied)
                # Derive real format/duration; do not trust imported JSON metadata.
                row[field] = {**asset, **verified, 'path': str(copied), 'sha256': signature}
        gxt = row.get('gxt')
        if gxt:
            row['gxt'] = dict(gxt)
            for key in ('gxt_path', 'original_gxt_path'):
                path = gxt.get(key)
                if path and Path(path).is_file():
                    row['gxt'][key.replace('_path', '_sha256')] = digest(path)
        stored.append(row)
    _validate_segments(track_data, stored)
    source = {k: track_data.get(k) for k in
              ('source_path', 'source_sha256', 'source_origin_path', 'source_kind', 'provenance',
               'sample_rate', 'channels', 'frames', 'duration')}
    result = {'schema': 'dialogue-studio/1', 'draft_id': identity, 'status': 'DRAFT',
              'production_approved': False, 'source': source, 'segments': stored,
              'speaker_layout': speaker_layout(stored),
              'gxt_authority': 'per-segment hypertranslated snapshots; no automatic translation'}
    path = root / 'drafts' / (identity + '.json')
    audio._check_cancel(cancel_event)
    json_atomico(path, result)
    return path


def load_draft(project_root, path, cancel_event=None, on_progress=None):
    """Restore a draft only after verifying owned sources/assets and region bounds."""
    path = Path(path).resolve()
    parent = _root(project_root) / 'drafts'
    if path.parent != parent:
        raise ValueError('Abra um rascunho da pasta dialogue_studio/drafts deste projeto.')
    identity = _identity(path.stem)
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema') != 'dialogue-studio/1' or data.get('draft_id') != identity
            or data.get('status') != 'DRAFT' or data.get('production_approved') is not False):
        raise ValueError('Rascunho de diálogo inválido.')
    source = data.get('source') or {}
    _validate_track(project_root, source)
    track = audio.load_track({'path': source['source_path'], 'sha256': source['source_sha256']},
                             cancel_event=cancel_event, on_progress=on_progress)
    for field in ('sample_rate', 'channels', 'frames'):
        if track[field] != source.get(field):
            raise ValueError('Formato do áudio não corresponde ao rascunho.')
    if abs(track['duration'] - float(source['duration'])) > 1 / track['sample_rate']:
        raise ValueError('Duração do áudio não corresponde ao rascunho.')
    for field in ('source_kind', 'source_origin_path', 'provenance'):
        track[field] = source.get(field)
    segments = data.get('segments')
    if not isinstance(segments, list):
        raise ValueError('Lista de trechos inválida.')
    for segment in segments:
        audio._check_cancel(cancel_event)
        for field in ('reference', 'replacement'):
            asset = segment.get(field)
            if asset:
                _inside(asset.get('path'), _root(project_root) / 'assets')
                if digest(asset['path']) != asset.get('sha256'):
                    raise ValueError('Referência ou take do rascunho foi alterado.')
                segment[field] = {**asset, **audio.import_replacement(asset['path'])}
        gxt = segment.get('gxt') or {}
        for field in ('gxt_path', 'original_gxt_path'):
            associated = gxt.get(field)
            expected = gxt.get(field.replace('_path', '_sha256'))
            if expected and (not associated or not Path(associated).is_file()
                             or digest(associated) != expected):
                raise ValueError('GXT associado ao rascunho mudou; confira o texto antes de reutilizar este vínculo.')
    _validate_segments(track, segments)
    track['speaker_layout'] = speaker_layout(segments)
    return dict(data, track_data=track, segments=segments, speaker_layout=track['speaker_layout'])
