"""Non-destructive tools for editing time ranges in original CUTSCENE tracks.

All audio is decoded to PCM only in the user's project cache.  Source game
files are read and hash-checked; rendered tracks and project drafts are new
files outside the game directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

from studio.audio.cutscene import cache_root
from studio.audio.wav import wav_info
from studio.core.utils import CREATE_FLAGS, json_atomico
from studio.script.native_index import digest


class CancelledError(RuntimeError):
    pass


BACKGROUND_MODES = {
    'Somente vozes PT-BR (fundo silencioso)': 'voice_only',
    'Preservar áudio original fora dos trechos': 'preserve_original',
}


def validate_background_mode(mode):
    if mode not in BACKGROUND_MODES.values():
        raise ValueError('Modo de fundo inválido para a montagem da cutscene.')
    return mode


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError('Operação cancelada.')


def _progress(on_progress, message, done=0, total=0):
    if on_progress:
        on_progress(done, total, message)


def _run_ffmpeg(args, output_path, cancel_event=None, on_progress=None):
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('FFmpeg não foi encontrado no PATH.')
    _check_cancel(cancel_event)
    with tempfile.TemporaryFile() as error_log:
        proc = subprocess.Popen([executable, '-v', 'error', '-nostdin', '-y', *args],
                                stdout=subprocess.DEVNULL, stderr=error_log,
                                creationflags=CREATE_FLAGS)
        started = time.monotonic()
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.wait(.12):
                proc.kill()
                proc.communicate()
                raise CancelledError('Operação cancelada.')
            if cancel_event is None:
                time.sleep(.12)
            if on_progress and time.monotonic() - started > 0.3:
                _progress(on_progress, 'Decodificando áudio…')
        proc.communicate()
        error_log.seek(0)
        stderr = error_log.read()
    if proc.returncode:
        Path(output_path).unlink(missing_ok=True)
        raise RuntimeError(stderr.decode('utf-8', 'replace').strip() or 'FFmpeg não conseguiu decodificar o áudio.')


def _source_manifest(project_root, archive_sha):
    root = cache_root(project_root) / archive_sha[:20]
    manifest_path = root / 'source.json'
    if not manifest_path.is_file():
        raise ValueError('Cache original CUTSCENE ausente. Extraia as 141 faixas antes de abrir o estúdio.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('archive_sha256') != archive_sha:
        raise ValueError('O cache não corresponde ao índice atual do arquivo CUTSCENE.')
    return root, manifest


def list_tracks(project_root, report, cancel_event=None, on_progress=None):
    """Return all verified cached source tracks mapped by the native index."""
    source = (report or {}).get('sources', {}).get('stream')
    if not source:
        raise ValueError('Índice nativo atual sem assinatura do arquivo CUTSCENE.')
    root, manifest = _source_manifest(project_root, source['sha256'])
    tracks = []
    candidates = [(name, item) for name, item in manifest.get('files', {}).items()
                  if name.startswith('CUTSCENE/Track_') and name.endswith('.ogg')]
    for index, (rel_path, signature) in enumerate(candidates, 1):
        _check_cancel(cancel_event)
        if not rel_path.startswith('CUTSCENE/Track_') or not rel_path.endswith('.ogg'):
            continue
        path = root / rel_path
        if path.is_file() and path.stat().st_size == signature.get('bytes') and digest(path) == signature.get('sha256'):
            tracks.append({'path': path, 'track': path.stem, 'sha256': signature['sha256'],
                           'archive_sha256': source['sha256'], 'archive_path': source['path']})
        if on_progress and (index == len(candidates) or index % 10 == 0):
            _progress(on_progress, 'Verificando cache CUTSCENE: %d/%d faixas.' % (index, len(candidates)),
                      index, len(candidates))
    tracks.sort(key=lambda row: row['track'])
    if len(tracks) != 141:
        raise ValueError('Cache CUTSCENE incompleto ou alterado: esperadas 141 faixas verificadas, encontradas %d.' % len(tracks))
    return tracks


def _probe(path, cancel_event=None):
    executable = shutil.which('ffprobe')
    if not executable:
        # ffprobe is normally shipped with FFmpeg. Keep a clear diagnostic if
        # only ffmpeg itself is on the path.
        raise RuntimeError('FFprobe não foi encontrado no PATH.')
    _check_cancel(cancel_event)
    proc = subprocess.Popen([executable, '-v', 'error', '-select_streams', 'a:0',
                             '-show_entries', 'stream=sample_rate,channels,duration',
                             '-of', 'json', str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=CREATE_FLAGS)
    try:
        deadline = time.monotonic() + 60
        while True:
            _check_cancel(cancel_event)
            if time.monotonic() >= deadline:
                proc.kill()
                proc.communicate()
                raise RuntimeError('FFprobe excedeu o limite de 60 segundos.')
            try:
                stdout, stderr = proc.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
    except CancelledError:
        proc.kill()
        proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError(stderr.decode('utf-8', 'replace').strip() or 'Não foi possível ler os metadados do áudio.')
    rows = json.loads(stdout.decode('utf-8')).get('streams') or []
    if not rows:
        raise ValueError('A faixa não contém áudio.')
    row = rows[0]
    rate, channels = int(row['sample_rate']), int(row['channels'])
    duration = float(row.get('duration') or 0)
    if rate < 1 or channels not in (1, 2) or duration <= 0:
        raise ValueError('Metadados de áudio não suportados.')
    return {'sample_rate': rate, 'channels': channels, 'duration': duration}


def _decode_raw(path, rate, channels, cancel_event=None, on_progress=None):
    fd, raw_path = tempfile.mkstemp(prefix='cutscene_pcm_', suffix='.s16le')
    os.close(fd)
    Path(raw_path).unlink(missing_ok=True)
    try:
        _run_ffmpeg(['-i', str(path), '-map', '0:a:0', '-f', 's16le', '-acodec', 'pcm_s16le',
                     '-ar', str(rate), '-ac', str(channels), str(raw_path)],
                    raw_path, cancel_event, on_progress)
        _check_cancel(cancel_event)
        data = Path(raw_path).read_bytes()
        if not data or len(data) % (channels * 2):
            raise ValueError('PCM decodificado incompleto ou inválido.')
        return data
    finally:
        Path(raw_path).unlink(missing_ok=True)


def load_track(track, cancel_event=None, on_progress=None, waveform_bins=1800):
    """Decode a complete OGG track and return source metadata and peak samples."""
    path = Path(track['path'])
    if digest(path) != track['sha256']:
        raise ValueError('A faixa original do cache foi alterada; recarregue a extração antes de editar.')
    if track.get('archive_path') and digest(track['archive_path']) != track.get('archive_sha256'):
        raise ValueError('Arquivo CUTSCENE do jogo mudou desde a extração.')
    meta = _probe(path, cancel_event)
    _progress(on_progress, 'Decodificando a faixa original completa…', 0, 1)
    pcm = _decode_raw(path, meta['sample_rate'], meta['channels'], cancel_event, on_progress)
    frame_bytes = meta['channels'] * 2
    frames = len(pcm) // frame_bytes
    meta['frames'] = frames
    meta['duration'] = frames / meta['sample_rate']
    meta['pcm'] = pcm
    meta['waveform'] = waveform_peaks(pcm, meta['channels'], waveform_bins)
    meta['source_sha256'] = track['sha256']
    meta['archive_sha256'] = track.get('archive_sha256')
    meta['archive_path'] = track.get('archive_path')
    meta['source_path'] = str(path)
    _progress(on_progress, 'Faixa pronta: %.2f s, %d canais, %d Hz.' % (meta['duration'], meta['channels'], meta['sample_rate']), 1, 1)
    return meta


def waveform_peaks(pcm, channels, bins=1800):
    """Compute waveform peak values from interleaved signed 16-bit PCM."""
    sample_count = len(pcm) // 2
    frame_count = sample_count // channels
    bins = max(1, min(int(bins), frame_count))
    import array
    values = array.array('h')
    values.frombytes(pcm)
    if values.itemsize != 2:
        raise RuntimeError('A plataforma não oferece amostras PCM de 16 bits.')
    if __import__('sys').byteorder != 'little':
        values.byteswap()
    result = []
    for index in range(bins):
        first = index * frame_count // bins * channels
        last = max(first + channels, (index + 1) * frame_count // bins * channels)
        peak = max((abs(value) for value in values[first:min(last, sample_count)]), default=0)
        result.append(peak / 32768.0)
    return result


def _frame_bounds(start, end, sample_rate, frames):
    try:
        start, end = float(start), float(end)
    except (ValueError, TypeError):
        raise ValueError('Informe início e fim numéricos.')
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError('O intervalo precisa ter início >= 0 e fim maior que o início.')
    first = int(round(start * sample_rate))
    last = int(round(end * sample_rate))
    if first >= frames or last > frames or first >= last:
        raise ValueError('O intervalo ultrapassa a duração da faixa.')
    return first, last


def _write_wav(path, pcm, channels, rate):
    with wave.open(str(path), 'wb') as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(pcm)


def _guard_destination(destination, sources=()):
    path = Path(destination).resolve()
    for source in sources:
        if source and path == Path(source).resolve():
            raise ValueError('A saída não pode sobrescrever um arquivo de origem.')
    if path.exists():
        raise ValueError('O arquivo de saída já existe. Escolha um nome novo para preservar o resultado anterior.')
    return path


def export_reference(track_data, start, end, destination, cancel_event=None, on_progress=None):
    _check_cancel(cancel_event)
    if digest(track_data['source_path']) != track_data['source_sha256']:
        raise ValueError('Faixa fonte alterada desde a abertura; a referência não foi exportada.')
    if track_data.get('archive_path') and digest(track_data['archive_path']) != track_data.get('archive_sha256'):
        raise ValueError('Arquivo CUTSCENE do jogo mudou desde a extração; a referência não foi exportada.')
    first, last = _frame_bounds(start, end, track_data['sample_rate'], track_data['frames'])
    frame_bytes = track_data['channels'] * 2
    pcm = track_data['pcm'][first * frame_bytes:last * frame_bytes]
    destination = _guard_destination(destination, (track_data['source_path'], track_data.get('archive_path')))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + '.' + uuid.uuid4().hex + '.part.wav')
    try:
        _write_wav(temp, pcm, track_data['channels'], track_data['sample_rate'])
        _check_cancel(cancel_event)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    if track_data.get('archive_path') and digest(track_data['archive_path']) != track_data.get('archive_sha256'):
        destination.unlink(missing_ok=True)
        raise ValueError('Arquivo CUTSCENE do jogo mudou durante a exportação da referência.')
    _progress(on_progress, 'Referência exportada: %s' % destination.name, 1, 1)
    return {'path': str(destination), 'sha256': digest(destination), 'duration': (last - first) / track_data['sample_rate'],
            'sample_rate': track_data['sample_rate'], 'channels': track_data['channels'],
            'start': first / track_data['sample_rate'], 'end': last / track_data['sample_rate']}


def import_replacement(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('WAV de reposição não encontrado.')
    info = wav_info(path)
    if not info or info['fmt'] != 1 or info['bits'] not in (16, 24, 32):
        raise ValueError('Importe um WAV PCM válido de 16, 24 ou 32 bits.')
    return {'path': str(path), 'sha256': digest(path), 'duration': info['dur'],
            'sample_rate': info['taxa'], 'channels': info['canais'], 'bits': info['bits'],
            'bytes': path.stat().st_size}


def _replacement_pcm(replacement, track_data, fit, cancel_event=None, on_progress=None):
    path = Path(replacement['path'])
    if digest(path) != replacement['sha256']:
        raise ValueError('WAV de reposição foi alterado desde a importação.')
    pcm = _decode_raw(path, track_data['sample_rate'], track_data['channels'], cancel_event, on_progress)
    return pcm


def validate_replacements(track_data, replacements, ranges, fit=False, allow_overlap=False):
    if len(replacements) != len(ranges):
        raise ValueError('Cada intervalo precisa ter exatamente um WAV de reposição.')
    normalized = []
    for item, bounds in zip(replacements, ranges):
        first, last = _frame_bounds(bounds['start'], bounds['end'], track_data['sample_rate'], track_data['frames'])
        limit = (last - first) / track_data['sample_rate']
        if item['duration'] > limit + (1 / track_data['sample_rate']) and not fit:
            raise ValueError('%s dura %.3f s e excede o intervalo de %.3f s. Ative “Ajustar áudio maior ao intervalo” para permitir ajuste de velocidade.' %
                             (Path(item['path']).name, item['duration'], limit))
        normalized.append((first, last, item))
    normalized.sort(key=lambda row: row[0])
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] < previous[1] and not allow_overlap:
            raise ValueError('Os intervalos de reposição se sobrepõem. Ajuste os limites antes de exportar.')
    return normalized


def validate_ranges(track_data, ranges, allow_overlap=False):
    normalized = []
    for bounds in ranges:
        first, last = _frame_bounds(bounds['start'], bounds['end'], track_data['sample_rate'], track_data['frames'])
        normalized.append((first, last))
    normalized.sort()
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] < previous[1] and not allow_overlap:
            raise ValueError('Os intervalos de reposição se sobrepõem. Ajuste os limites antes de salvar.')
    return normalized


def render_track(track_data, replacements, ranges, destination, fit=False,
                 cancel_event=None, on_progress=None, allow_overlap=False, background_mode='voice_only', missing_ptbr=None):
    """Replace selected ranges at their original positions and preserve track length."""
    if digest(track_data['source_path']) != track_data['source_sha256']:
        raise ValueError('Faixa original alterada desde a abertura; exportação cancelada.')
    if track_data.get('archive_path') and digest(track_data['archive_path']) != track_data.get('archive_sha256'):
        raise ValueError('Arquivo CUTSCENE do jogo mudou desde a extração; exportação cancelada.')
    background_mode = validate_background_mode(background_mode)
    normalized = validate_replacements(track_data, replacements, ranges, fit, allow_overlap)
    frame_bytes = track_data['channels'] * 2
    output = (bytearray(len(track_data['pcm'])) if background_mode == 'voice_only'
              else bytearray(track_data['pcm']))
    # Clear the complete union first. Accumulate in wide signed integers so a
    # silent/short second take cannot erase the first or restore original EN.
    import array
    union = []
    for first, last, _ in normalized:
        if union and first <= union[-1][1]:
            union[-1][1] = max(union[-1][1], last)
        else:
            union.append([first, last])
    mixes = []
    for first, last in union:
        output[first * frame_bytes:last * frame_bytes] = b'\0' * ((last - first) * frame_bytes)
        mixes.append((first, last, array.array('q', [0]) * ((last - first) * track_data['channels'])))
    total = max(1, len(normalized))
    for index, (first, last, replacement) in enumerate(normalized, 1):
        _check_cancel(cancel_event)
        pcm = _replacement_pcm(replacement, track_data, fit, cancel_event, on_progress)
        target_frames = last - first
        source_frames = len(pcm) // frame_bytes
        if source_frames > target_frames:
            if not fit:
                raise ValueError('WAV de reposição excede o intervalo selecionado.')
            # Resample the clip's playback speed to exactly fit the selected
            # interval. Interleaved channels remain paired and no game audio is
            # written or changed.
            import array
            values = array.array('h')
            values.frombytes(pcm)
            if os.sys.byteorder != 'little':
                values.byteswap()
            new_values = []
            out_frames = target_frames
            for frame in range(out_frames):
                _check_cancel(cancel_event) if frame % 50000 == 0 else None
                position = frame * (source_frames - 1) / max(1, out_frames - 1)
                left = int(position)
                fraction = position - left
                right = min(left + 1, source_frames - 1)
                for channel in range(track_data['channels']):
                    a, b = values[left * track_data['channels'] + channel], values[right * track_data['channels'] + channel]
                    new_values.append(int(round(a * (1 - fraction) + b * fraction)))
            pcm = struct.pack('<%dh' % len(new_values), *new_values)
            source_frames = out_frames
        values = array.array('h')
        values.frombytes(pcm[:target_frames * frame_bytes])
        if os.sys.byteorder != 'little':
            values.byteswap()
        union_first, _, summed = next(mix for mix in mixes if mix[0] <= first and mix[1] >= last)
        offset = (first - union_first) * track_data['channels']
        for sample_index, sample in enumerate(values):
            if sample_index % 100000 == 0:
                _check_cancel(cancel_event)
            summed[offset + sample_index] += sample
        _progress(on_progress, 'Aplicando reposição %d de %d…' % (index, total), index, total)
    positive = max((max(samples, default=0) for _, _, samples in mixes), default=0)
    negative = min((min(samples, default=0) for _, _, samples in mixes), default=0)
    mix_gain = min(1.0, 32767 / positive if positive > 0 else 1.0,
                   -32768 / negative if negative < 0 else 1.0)
    for first, last, samples in mixes:
        mixed = array.array('h')
        for index, sample in enumerate(samples):
            if index % 100000 == 0:
                _check_cancel(cancel_event)
            mixed.append(round(sample * mix_gain))
        if os.sys.byteorder != 'little':
            mixed.byteswap()
        output[first * frame_bytes:last * frame_bytes] = mixed.tobytes()
    _check_cancel(cancel_event)
    destination = _guard_destination(destination, (track_data['source_path'], track_data.get('archive_path'),
        *(item['path'] for item in replacements)))
    metadata = _guard_destination(destination.with_suffix('.mix.json'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + '.' + uuid.uuid4().hex + '.part.wav')
    try:
        _write_wav(temp, bytes(output), track_data['channels'], track_data['sample_rate'])
        _check_cancel(cancel_event)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    if digest(track_data['source_path']) != track_data['source_sha256']:
        destination.unlink(missing_ok=True)
        raise ValueError('A faixa original mudou durante a exportação; o resultado foi removido.')
    if track_data.get('archive_path') and digest(track_data['archive_path']) != track_data.get('archive_sha256'):
        destination.unlink(missing_ok=True)
        raise ValueError('Arquivo CUTSCENE do jogo mudou durante a exportação; o resultado foi removido.')
    _progress(on_progress, 'Faixa completa exportada: %s' % destination.name, 1, 1)
    result = {'path': str(destination), 'sha256': digest(destination), 'duration': track_data['duration'],
            'sample_rate': track_data['sample_rate'], 'channels': track_data['channels'],
            'replacements': len(normalized), 'allow_overlap': bool(allow_overlap), 'mix_gain': mix_gain,
            'replaced_union_frames': union, 'background_mode': background_mode,
            'missing_ptbr': list(missing_ptbr or [])}
    json_atomico(metadata, result)
    return result


def save_draft(project_root, track_data, segments, gxt_rows, draft_id=None, fit=False,
               reference=None, reference_transcript='', reference_row_id=None, allow_overlap=False,
               background_mode='voice_only'):
    """Save a reviewable, hash-guarded manifest; never edits any GXT or game file."""
    background_mode = validate_background_mode(background_mode)
    if digest(track_data['source_path']) != track_data['source_sha256']:
        raise ValueError('Faixa original alterada; o rascunho não foi salvo.')
    if track_data.get('archive_sha256'):
        archive = None
        try:
            report_path = Path(project_root) / 'workspace' / 'native_audio' / 'index.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            archive = report.get('sources', {}).get('stream', {}).get('path')
        except (OSError, ValueError, TypeError):
            pass
        if not archive or digest(archive) != track_data['archive_sha256']:
            raise ValueError('Arquivo CUTSCENE do jogo mudou desde a extração; o rascunho não foi salvo.')
    for segment in segments:
        item = segment.get('replacement')
        if item and digest(item['path']) != item['sha256']:
            raise ValueError('WAV de reposição alterado: ' + Path(item['path']).name)
        segment_reference = segment.get('reference')
        if segment_reference and digest(segment_reference['path']) != segment_reference['sha256']:
            raise ValueError('WAV de referência alterado: ' + Path(segment_reference['path']).name)
    if reference and digest(reference['path']) != reference['sha256']:
        raise ValueError('WAV de referência alterado: ' + Path(reference['path']).name)
    validate_ranges(track_data, [{'start': item['start'], 'end': item['end']} for item in segments], allow_overlap)
    for item in segments:
        replacement = item.get('replacement')
        if replacement:
            validate_replacements(track_data, [replacement],
                [{'start': item['start'], 'end': item['end']}], fit)
    identity = draft_id or uuid.uuid4().hex
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('ID do rascunho inválido.')
    drafts_dir = (Path(project_root) / 'workspace' / 'cutscene_studio' / 'drafts').resolve()
    path = (drafts_dir / (identity + '.json')).resolve()
    if path.parent != drafts_dir:
        raise ValueError('Destino do rascunho fora da pasta permitida.')
    data = {'schema': 1, 'status': 'DRAFT', 'draft_id': identity,
            'source': {'track': Path(track_data['source_path']).stem,
                       'path': str(Path(track_data['source_path']).resolve()),
                       'sha256': track_data['source_sha256'],
                       'archive_sha256': track_data.get('archive_sha256'),
                       'duration_seconds': track_data['duration'],
                       'sample_rate': track_data['sample_rate'], 'channels': track_data['channels']},
            'segments': [], 'gxt_authority': 'hypertranslated text shown by the editor; no automatic translation',
            'fit_oversized': bool(fit), 'allow_overlap': bool(allow_overlap), 'background_mode': background_mode,
            'reference_authoring': {
                'audio': reference, 'transcript': reference_transcript,
                'gxt_row_id': reference_row_id},
            'production_approved': False}
    for segment in segments:
        replacement = segment.get('replacement')
        row = {'start_seconds': float(segment['start']), 'end_seconds': float(segment['end']),
               'gxt': segment.get('gxt'), 'reference': segment.get('reference'),
               'gxt_row_id': segment.get('gxt_row_id'), 'actor_id': segment.get('actor_id'),
               'reference_transcript': segment.get('reference_transcript')}
        if replacement:
            row['replacement'] = dict(replacement)
        data['segments'].append(row)
    data['gxt_rows'] = []
    for row in gxt_rows:
        row = dict(row)
        for key in ('gxt_path', 'original_gxt_path'):
            source = row.get(key)
            row[key.replace('_path', '_sha256')] = digest(source) if source and Path(source).is_file() else None
        data['gxt_rows'].append(row)
    json_atomico(path, data)
    return path


def load_draft(path, project_root, report, cancel_event=None, allow_overlap=False, background_mode=None):
    """Read a draft and verify every source/reference/replacement signature."""
    _check_cancel(cancel_event)
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Não foi possível ler o rascunho CUTSCENE: %s' % exc)
    if data.get('schema') != 1 or data.get('status') != 'DRAFT' or data.get('production_approved') is not False:
        raise ValueError('Manifesto CUTSCENE não é um rascunho reconhecido.')
    saved_overlap = data.get('allow_overlap', False)
    if not isinstance(saved_overlap, bool):
        raise ValueError('Opção de falas simultâneas inválida no rascunho.')
    allow_overlap = allow_overlap or saved_overlap
    data['allow_overlap'] = bool(allow_overlap)
    # Absence means legacy preservation, never silently change a saved mix.
    data['background_mode'] = validate_background_mode(
        background_mode if background_mode is not None else data.get('background_mode', 'preserve_original'))
    identity = data.get('draft_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
        raise ValueError('ID do rascunho inválido.')
    drafts_dir = (Path(project_root) / 'workspace' / 'cutscene_studio' / 'drafts').resolve()
    if path.parent != drafts_dir or path.stem != identity:
        raise ValueError('O rascunho deve estar na pasta permitida e corresponder ao ID do manifesto.')
    source = data.get('source') or {}
    if not source.get('path') or digest(source['path']) != source.get('sha256'):
        raise ValueError('A faixa original do rascunho mudou ou não está disponível.')
    stream = (report or {}).get('sources', {}).get('stream') or {}
    if not stream.get('path') or digest(stream['path']) != source.get('archive_sha256'):
        raise ValueError('O arquivo CUTSCENE do jogo mudou desde que o rascunho foi salvo.')
    if stream.get('sha256') != source.get('archive_sha256'):
        raise ValueError('O rascunho não corresponde ao índice CUTSCENE atual.')
    expected_root, cache_manifest = _source_manifest(project_root, stream['sha256'])
    expected_track_path = (expected_root / 'CUTSCENE' / (source.get('track', '') + '.ogg')).resolve()
    try:
        actual_source_path = Path(source['path']).resolve()
        actual_source_path.relative_to(expected_root.resolve())
    except ValueError:
        raise ValueError('A origem do rascunho não está no cache CUTSCENE protegido do projeto.')
    if actual_source_path != expected_track_path:
        raise ValueError('A faixa deste rascunho não corresponde à faixa do cache CUTSCENE atual.')
    relative = 'CUTSCENE/' + expected_track_path.name
    signature = cache_manifest.get('files', {}).get(relative) or {}
    if not signature.get('sha256') or signature['sha256'] != source.get('sha256'):
        raise ValueError('O hash da faixa do rascunho difere do cache CUTSCENE atual.')
    if digest(actual_source_path) != signature['sha256']:
        raise ValueError('A faixa original no cache CUTSCENE foi alterada.')
    probed = _probe(actual_source_path, cancel_event)
    if (probed['sample_rate'] != int(source.get('sample_rate', 0)) or
            probed['channels'] != int(source.get('channels', 0)) or
            abs(probed['duration'] - float(source.get('duration_seconds', 0))) > 1 / max(1, probed['sample_rate'])):
        raise ValueError('A duração ou o formato da faixa mudou desde que o rascunho foi salvo.')
    for segment in data.get('segments', []):
        _check_cancel(cancel_event)
        for field in ('reference', 'replacement'):
            item = segment.get(field)
            if item and (not Path(item.get('path', '')).is_file() or digest(item['path']) != item.get('sha256')):
                raise ValueError('O WAV %s do rascunho mudou ou não está disponível.' % field)
    authoring_reference = (data.get('reference_authoring') or {}).get('audio')
    if authoring_reference and (not Path(authoring_reference.get('path', '')).is_file() or
                                digest(authoring_reference['path']) != authoring_reference.get('sha256')):
        raise ValueError('WAV de referência atual do rascunho mudou ou não está disponível.')
    ranges = []
    for segment in data.get('segments', []):
        start, end = segment.get('start_seconds'), segment.get('end_seconds')
        first, last = _frame_bounds(start, end, int(source['sample_rate']),
                                    round(float(source['duration_seconds']) * int(source['sample_rate'])))
        ranges.append((first, last))
        replacement = segment.get('replacement')
        if replacement and replacement.get('duration', 0) > (last-first)/int(source['sample_rate']) + 1/int(source['sample_rate']) and not data.get('fit_oversized'):
            raise ValueError('Rascunho contém WAV maior que o intervalo sem opção de ajuste registrada.')
    ranges.sort()
    if not allow_overlap and any(current[0] < previous[1] for previous, current in zip(ranges, ranges[1:])):
        raise ValueError('Rascunho contém intervalos sobrepostos.')
    for row in data.get('gxt_rows', []):
        _check_cancel(cancel_event)
        for path_key, hash_key in (('gxt_path', 'gxt_sha256'), ('original_gxt_path', 'original_gxt_sha256')):
            gxt_path, expected = row.get(path_key), row.get(hash_key)
            if expected and (not gxt_path or not Path(gxt_path).is_file() or digest(gxt_path) != expected):
                raise ValueError('Arquivo GXT associado ao rascunho foi alterado: %s' % (gxt_path or 'sem caminho'))
    return data
