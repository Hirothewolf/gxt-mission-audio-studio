"""Versioned, non-destructive layered PCM timeline; no game writes or Qt imports."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
import wave
from pathlib import Path

SCHEMA_VERSION = 1
ROLES = {'original', 'installed', 'pt', 'ambience'}


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_pcm(path):
    """Read uncompressed PCM WAV as float64 frames x channels."""
    import numpy as np
    with wave.open(str(path), 'rb') as src:
        channels, rate, width, count = src.getnchannels(), src.getframerate(), src.getsampwidth(), src.getnframes()
        if channels not in (1, 2) or width not in (1, 2, 3, 4) or src.getcomptype() != 'NONE' or count < 1:
            raise ValueError('WAV precisa conter PCM mono/estéreo de 8, 16, 24 ou 32 bits.')
        raw = src.readframes(count)
    if len(raw) != count * channels * width:
        raise ValueError('WAV truncado.')
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
        data = ((values ^ 0x800000) - 0x800000).astype(np.float64) / 8388608
    else:
        data = np.frombuffer(raw, dtype='<i%d' % width).astype(np.float64) / (2 ** (width * 8 - 1))
    return data.reshape(-1, channels), rate


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('%s precisa ser inteiro >= %d.' % (label, minimum))


def _gain(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 16:
        raise ValueError('Ganho inválido (0–16).')


class TimelineProject:
    def __init__(self, sample_rate=48000, channels=2):
        self.data = {'schema_version': SCHEMA_VERSION, 'sample_rate': sample_rate, 'channels': channels,
                     'scene': {}, 'sources': {}, 'tracks': [], 'clips': []}
        self._undo, self._redo = [], []
        self._saved = copy.deepcopy(self.data)
        self.validate()

    @property
    def sample_rate(self):
        return self.data['sample_rate']

    @property
    def dirty(self):
        return self.data != self._saved

    @property
    def duration_frames(self):
        return max((c['position'] + self.clip_frames(c) for c in self.data['clips']), default=0)

    def clip_frames(self, clip):
        if 'sampling' in clip:
            return clip['sampling']['duration']
        source = self.data['sources'][clip['source_id']]
        return max(1, round((clip['source_out'] - clip['source_in']) * self.sample_rate / source['sample_rate']))

    def clip(self, clip_id):
        return next(c for c in self.data['clips'] if c['id'] == clip_id)

    def track(self, track_id):
        return next(t for t in self.data['tracks'] if t['id'] == track_id)

    def _change(self, operation):
        previous = copy.deepcopy(self.data)
        try:
            result = operation()
            self.validate()
        except BaseException:
            self.data = previous
            raise
        if previous != self.data:
            self._undo.append(previous)
            self._redo.clear()
        return result

    def undo(self):
        if self._undo:
            self._redo.append(copy.deepcopy(self.data))
            self.data = self._undo.pop()
            return True
        return False

    def redo(self):
        if self._redo:
            self._undo.append(copy.deepcopy(self.data))
            self.data = self._redo.pop()
            return True
        return False

    def add_track(self, name, role='pt'):
        track_id = uuid.uuid4().hex
        self._change(lambda: self.data['tracks'].append({'id': track_id, 'name': name, 'role': role,
                         'mute': role in ('original', 'installed'), 'solo': False, 'gain': 1.0,
                         'locked': role == 'original'}))
        return track_id

    def add_wav(self, path, track_id=None, position=0, role='pt', metadata=None, native_format=False):
        path = Path(path).resolve()
        pcm, rate = read_pcm(path)
        sha = digest(path)
        clip_id = uuid.uuid4().hex
        def apply():
            nonlocal track_id
            if native_format and not self.data['clips']:
                self.data['sample_rate'], self.data['channels'] = rate, pcm.shape[1]
            if track_id is None:
                track_id = uuid.uuid4().hex
                self.data['tracks'].append({'id': track_id, 'name': path.stem, 'role': role,
                    'mute': role in ('original', 'installed'), 'solo': False, 'gain': 1.0, 'locked': role == 'original'})
            elif self.track(track_id)['locked']:
                raise ValueError('Camada bloqueada.')
            self.data['sources'].setdefault(sha, {'path': str(path), 'sha256': sha,
                         'sample_rate': rate, 'channels': pcm.shape[1], 'frames': len(pcm)})
            self.data['clips'].append({'id': clip_id, 'track_id': track_id, 'source_id': sha,
                 'source_in': 0, 'source_out': len(pcm), 'position': position, 'gain': 1.0,
                 'fade_in': 0, 'fade_out': 0, 'metadata': copy.deepcopy(metadata or {})})
        self._change(apply)
        return clip_id

    def update_track(self, track_id, **values):
        if not set(values) <= {'name', 'role', 'mute', 'solo', 'gain', 'locked'}:
            raise ValueError('Campo de camada desconhecido.')
        self._change(lambda: self.track(track_id).update(values))

    def update_clip(self, clip_id, **values):
        if not set(values) <= {'source_in', 'source_out', 'position', 'gain', 'fade_in', 'fade_out', 'metadata', 'track_id'}:
            raise ValueError('Campo de clipe desconhecido.')
        clip = self.clip(clip_id)
        if self.track(clip['track_id'])['locked'] or ('track_id' in values and self.track(values['track_id'])['locked']):
            raise ValueError('Camada bloqueada.')
        def apply():
            target = self.clip(clip_id)
            # A split retains its fractional resampling phase and fade envelope.
            # An explicit new trim/fade establishes a new local envelope.
            if any(key in values and values[key] != target[key] for key in ('source_in', 'source_out', 'fade_in', 'fade_out')):
                target.pop('sampling', None)
            target.update(copy.deepcopy(values))
        self._change(apply)

    def delete(self, clip_id):
        clip = self.clip(clip_id)
        if self.track(clip['track_id'])['locked']:
            raise ValueError('Camada bloqueada.')
        self._change(lambda: self.data['clips'].remove(clip))

    def duplicate(self, clip_id, position, track_id=None):
        source = self.clip(clip_id)
        target = track_id or source['track_id']
        if self.track(target)['locked']:
            raise ValueError('Camada bloqueada.')
        new_clip = copy.deepcopy(source)
        new_clip.update(id=uuid.uuid4().hex, position=position, track_id=target)
        self._change(lambda: self.data['clips'].append(new_clip))
        return new_clip['id']

    def split(self, clip_id, timeline_frame):
        clip = self.clip(clip_id)
        if self.track(clip['track_id'])['locked']:
            raise ValueError('Camada bloqueada.')
        source = self.data['sources'][clip['source_id']]
        delta = timeline_frame - clip['position']
        duration = self.clip_frames(clip)
        if not 0 < delta < duration:
            raise ValueError('Cursor precisa estar dentro do clipe.')
        sampling = copy.deepcopy(clip.get('sampling') or {
            'start_num': clip['source_in'] * self.sample_rate, 'duration': duration,
            'limit_in': clip['source_in'], 'limit_out': clip['source_out'],
            'envelope_offset': 0, 'envelope_length': duration,
            'fade_in': clip['fade_in'], 'fade_out': clip['fade_out']})
        cut_num = sampling['start_num'] + delta * source['sample_rate']
        cut_floor, remainder = divmod(cut_num, self.sample_rate)
        cut_ceil = cut_floor + bool(remainder)
        right_id = uuid.uuid4().hex
        def apply():
            right = copy.deepcopy(clip)
            right.update(id=right_id, source_in=min(cut_floor, clip['source_out'] - 1), position=timeline_frame, fade_in=0)
            right['sampling'] = dict(sampling, start_num=cut_num, duration=duration - delta,
                                     envelope_offset=sampling['envelope_offset'] + delta)
            clip.update(source_out=max(clip['source_in'] + 1, cut_ceil), fade_out=0)
            clip['sampling'] = dict(sampling, duration=delta)
            clip['fade_in'] = min(clip['fade_in'], self.clip_frames(clip))
            right['fade_out'] = min(right['fade_out'], self.clip_frames(right))
            self.data['clips'].append(right)
        self._change(apply)
        return right_id

    def validate(self):
        d = self.data
        if not isinstance(d, dict) or type(d.get('schema_version')) is not int or d.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('Versão de projeto não suportada.')
        _integer(d['sample_rate'], 'Taxa', 1)
        if d['sample_rate'] > 384000 or d['channels'] not in (1, 2):
            raise ValueError('Formato do projeto inválido.')
        if not isinstance(d['scene'], dict) or not isinstance(d['sources'], dict) or not isinstance(d['tracks'], list) or not isinstance(d['clips'], list):
            raise ValueError('Estrutura do projeto inválida.')
        track_ids, clip_ids = set(), set()
        for sid, source in d['sources'].items():
            if sid != source['sha256'] or len(sid) != 64 or any(ch not in '0123456789abcdef' for ch in sid):
                raise ValueError('Hash de fonte inválido.')
            if not isinstance(source['path'], str) or not source['path']:
                raise ValueError('Caminho de fonte inválido.')
            _integer(source['sample_rate'], 'Taxa da fonte', 1)
            _integer(source['frames'], 'Frames da fonte', 1)
            if source['channels'] not in (1, 2):
                raise ValueError('Canais da fonte inválidos.')
        for track in d['tracks']:
            if not isinstance(track['id'], str) or not track['id'] or track['id'] in track_ids or track['role'] not in ROLES or not isinstance(track['name'], str):
                raise ValueError('Camada inválida ou duplicada.')
            track_ids.add(track['id'])
            for key in ('mute', 'solo', 'locked'):
                if type(track[key]) is not bool:
                    raise ValueError('Estado de camada inválido.')
            _gain(track['gain'])
        for clip in d['clips']:
            if not isinstance(clip['id'], str) or not clip['id'] or clip['id'] in clip_ids or clip['track_id'] not in track_ids or clip['source_id'] not in d['sources']:
                raise ValueError('Referência de clipe inválida ou duplicada.')
            clip_ids.add(clip['id'])
            for key in ('source_in', 'source_out', 'position', 'fade_in', 'fade_out'):
                _integer(clip[key], key)
            if not clip['source_in'] < clip['source_out'] <= d['sources'][clip['source_id']]['frames']:
                raise ValueError('Corte ultrapassa a fonte.')
            if max(clip['fade_in'], clip['fade_out']) > self.clip_frames(clip):
                raise ValueError('Fade ultrapassa o clipe.')
            if not isinstance(clip['metadata'], dict):
                raise ValueError('Metadados de clipe inválidos.')
            if 'sampling' in clip:
                sampling = clip['sampling']
                for key in ('start_num', 'duration', 'limit_in', 'limit_out', 'envelope_offset', 'envelope_length', 'fade_in', 'fade_out'):
                    _integer(sampling[key], 'sampling.' + key)
                if (sampling['duration'] < 1 or not 0 <= sampling['limit_in'] < sampling['limit_out'] <= d['sources'][clip['source_id']]['frames']
                        or not sampling['limit_in'] * self.sample_rate <= sampling['start_num'] < sampling['limit_out'] * self.sample_rate
                        or sampling['envelope_offset'] + sampling['duration'] > sampling['envelope_length']
                        or max(sampling['fade_in'], sampling['fade_out']) > sampling['envelope_length']):
                    raise ValueError('Relógio de clipe dividido inválido.')
            _gain(clip['gain'])
        json.dumps(d, allow_nan=False)

    def save(self, path):
        self.validate()
        path = Path(path).resolve()
        if path in {Path(s['path']).resolve() for s in self.data['sources'].values()}:
            raise ValueError('O projeto não pode sobrescrever uma fonte.')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(self.data, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._saved = copy.deepcopy(self.data)

    @classmethod
    def load(cls, path, verify_sources=True):
        project = cls()
        try:
            project.data = json.loads(Path(path).read_text(encoding='utf-8'))
            project.validate()
        except (KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
            raise ValueError('Projeto inválido: %s' % exc) from exc
        if verify_sources:
            for source in project.data['sources'].values():
                if not Path(source['path']).is_file() or digest(source['path']) != source['sha256']:
                    raise ValueError('Fonte ausente ou alterada: %s' % source['path'])
        project._saved = copy.deepcopy(project.data)
        return project

    def render(self, destination, include_comparison=False, cancel_event=None, on_progress=None):
        """Same float mixer for previews and exports. Originals require explicit opt-in."""
        import numpy as np
        self.validate()
        destination = Path(destination).resolve()
        if destination in {Path(s['path']).resolve() for s in self.data['sources'].values()}:
            raise ValueError('Render não pode sobrescrever uma fonte.')
        def check():
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Render cancelado.')
        tracks = [t for t in self.data['tracks'] if include_comparison or t['role'] not in ('original', 'installed')]
        solo = any(t['solo'] for t in tracks)
        active = {t['id']: t for t in tracks if not t['mute'] and (not solo or t['solo'])}
        clips = [c for c in self.data['clips'] if c['track_id'] in active]
        count = self.duration_frames
        if count < 1:
            raise ValueError('Timeline vazia.')
        check()
        mix = np.zeros((count, self.data['channels']), dtype=np.float64)
        cache = {}
        for index, clip in enumerate(clips):
            check()
            sid = clip['source_id']
            source = self.data['sources'][sid]
            if sid not in cache:
                if digest(source['path']) != source['sha256']:
                    raise ValueError('Fonte alterada: %s' % source['path'])
                cache[sid], rate = read_pcm(source['path'])
                if rate != source['sample_rate'] or len(cache[sid]) != source['frames'] or cache[sid].shape[1] != source['channels']:
                    raise ValueError('Metadados da fonte não correspondem ao WAV.')
            length = self.clip_frames(clip)
            sampling = clip.get('sampling')
            if sampling:
                audio = cache[sid][sampling['limit_in']:sampling['limit_out']]
                locations = (sampling['start_num'] + np.arange(length) * source['sample_rate']) / self.sample_rate - sampling['limit_in']
                audio = np.stack([np.interp(locations, np.arange(len(audio)), audio[:, c]) for c in range(audio.shape[1])], axis=1)
            else:
                audio = cache[sid][clip['source_in']:clip['source_out']]
            if not sampling and source['sample_rate'] != self.sample_rate:
                locations = np.arange(length) * source['sample_rate'] / self.sample_rate
                audio = np.stack([np.interp(locations, np.arange(len(audio)), audio[:, c]) for c in range(audio.shape[1])], axis=1)
            if audio.shape[1] != self.data['channels']:
                audio = np.repeat(audio, 2, axis=1) if self.data['channels'] == 2 else audio.mean(axis=1, keepdims=True)
            audio = audio.copy() * clip['gain'] * active[clip['track_id']]['gain']
            if sampling:
                indices = np.arange(length) + sampling['envelope_offset']
                for key, reverse in (('fade_in', False), ('fade_out', True)):
                    n = sampling[key]
                    if n:
                        at = sampling['envelope_length'] - 1 - indices if reverse else indices
                        ramp = np.clip(at / max(1, n - 1), 0, 1)
                        audio *= ramp[:, None]
            else:
                for key, reverse in (('fade_in', False), ('fade_out', True)):
                    n = clip[key]
                    if n:
                        ramp = np.linspace(0, 1, n)[:, None]
                        if reverse:
                            audio[-n:] *= ramp[::-1]
                        else:
                            audio[:n] *= ramp
            start = clip['position']
            mix[start:start + length] += audio
            if on_progress:
                on_progress(index + 1, len(clips), 'Misturando clipe %d/%d' % (index + 1, len(clips)))
        check()
        clipped = int(np.count_nonzero(np.abs(mix) > 1))
        pcm = np.rint(np.clip(mix, -1, 32767 / 32768) * 32768).astype('<i2').tobytes()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.timeline_', suffix='.wav', dir=destination.parent)
        os.close(fd)
        try:
            with wave.open(temporary, 'wb') as out:
                out.setnchannels(self.data['channels'])
                out.setsampwidth(2)
                out.setframerate(self.sample_rate)
                out.writeframes(pcm)
            check()
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {'path': str(destination), 'frames': count, 'sample_rate': self.sample_rate,
                'channels': self.data['channels'], 'clipped_samples': clipped, 'mixed_clips': len(clips)}
