"""Read-only synchronization using the open document and per-line audio evidence."""
import hashlib
import json
import os
import time
import struct
import wave
from collections import OrderedDict
from pathlib import Path

from studio.core.utils import sha256_arquivo
from studio.audio.source_routing import audio_route, is_stream_record


# UI evidence only; installation and the external-change watcher retain strict hashes.
_UI_HASHES = OrderedDict()
_JSON_CACHE = {}


def cached_json(path):
    """Read proof documents once per content signature, without rewriting them."""
    path = Path(path)
    stat = path.stat()
    stamp = (stat.st_size, stat.st_mtime_ns, _change_time(path, stat))
    cached = _JSON_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    data = json.loads(path.read_text(encoding='utf-8'))
    _JSON_CACHE[path] = (stamp, data)
    return data


def sfx_slot(info, game_audio, mod=None):
    """Locate one real SAAT PCM slot by reading only lookup and bank header.

    Coordinates are the same one-based bank/sound numbers used by SAAT.
    Both packed SFX archives and ModLoader Bank_NNN/std.bank are supported.
    """
    from studio.saat.detection import PAKS
    ref = game_audio.ref_para(info)
    if not ref:
        return None
    pak, bank, sound = ref
    if bank < 1 or sound < 1:
        return None
    base = game_audio.modloader_dir() / mod if mod else game_audio.game_dir
    if not base:
        return None
    archive = base / 'audio/SFX' / pak
    native_bank = archive / ('Bank_%03d' % bank) / 'std.bank'
    offset = 0
    if native_bank.is_file():
        archive = native_bank
        payload_size = archive.stat().st_size - 4804
    elif archive.is_file():
        lookup = base / 'audio/CONFIG/BankLkup.dat'
        if not lookup.is_file():
            return None
        data = lookup.read_bytes()
        if len(data) % 12:
            return None
        rows = [row for row in struct.iter_unpack('<B3xII', data) if row[0] == PAKS.index(pak)]
        if bank > len(rows):
            return None
        _, offset, payload_size = rows[bank-1]
    else:
        return None
    with archive.open('rb') as handle:
        handle.seek(offset)
        header = handle.read(4804)
    if len(header) != 4804:
        return None
    count = struct.unpack_from('<I', header)[0]
    if not 1 <= count <= 400 or sound > count:
        return None
    start, _, rate, _ = struct.unpack_from('<IIHH', header, 4 + (sound-1)*12)
    end = struct.unpack_from('<I', header, 4 + sound*12)[0] if sound < count else payload_size
    if rate < 1000 or start >= end or end > payload_size or (end-start) % 2:
        return None
    if offset + 4804 + end > archive.stat().st_size:
        return None
    return dict(path=archive, offset=offset+4804+start, size=end-start, rate=rate, ref=ref)


def _slot_cache_path(slot, project_root):
    source = slot['path']
    stat = source.stat()
    identity = repr((str(source.resolve()), stat.st_size, stat.st_mtime_ns,
                     _change_time(source, stat), slot['ref'], slot['offset'], slot['size']))
    return Path(project_root) / '.cache/qt_audio_slots' / (hashlib.sha256(identity.encode()).hexdigest()+'.wav')


def cached_sfx_slot(slot, project_root):
    target = _slot_cache_path(slot, project_root)
    try:
        proof = cached_json(target.with_suffix('.json'))
        if target.is_file() and _ui_sha256(target) == proof.get('sha256'):
            return target
    except (OSError, ValueError):
        pass
    return None


def extract_sfx_slot(info, project_root, game_audio, mod=None):
    """Audition only: extract one slot into a source-pinned cache, never install."""
    slot = sfx_slot(info, game_audio, mod)
    if not slot:
        return None
    target = cached_sfx_slot(slot, project_root)
    if target:
        return target
    source = slot['path']
    stat = source.stat()
    target = _slot_cache_path(slot, project_root)
    with source.open('rb') as handle:
        handle.seek(slot['offset'])
        pcm = handle.read(slot['size'])
    if len(pcm) != slot['size'] or source.stat().st_mtime_ns != stat.st_mtime_ns:
        raise ValueError('O banco mudou durante a leitura do trecho.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), 'wb') as handle:
        handle.setparams((1, 2, slot['rate'], 0, 'NONE', 'not compressed'))
        handle.writeframes(pcm)
    target.with_suffix('.json').write_text(json.dumps({'sha256': sha256_arquivo(target)}), encoding='utf-8')
    return target


def _stream_line_cache(root, info, row):
    identity = (info.get('gxt_stable_id'), row.get('container'), row.get('track'),
                row.get('archive_sha256'), row.get('start'), row.get('end'))
    return Path(root) / '.cache/qt_stream_lines' / (hashlib.sha256(repr(identity).encode()).hexdigest()+'.wav')


def extract_stream_line(info, project_root, game_audio, mod=None, cancel=None):
    """Render the explicit proven window from the actual installed stream.

    Intended for an audition worker. No proof, bank, GXT or ReviewStore writes.
    full_track reuses its source-pinned export, shared by all lines of a scene.
    """
    import copy
    import uuid
    import soundfile as sf
    from types import SimpleNamespace
    from .scene_sources import full_track
    mod = mod or game_audio.mod_ativo()
    evidence = installed_line_evidence(info, project_root, game_audio, mod=mod)
    if evidence.get('path'):
        return evidence['path']
    row = evidence.get('proof', {})
    if evidence['state'] != 'proveninstalled' or not all(k in row for k in ('start', 'end')):
        return None
    start, end = float(row['start']), float(row['end'])
    if not 0 <= start < end:
        return None
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Audição cancelada.')
    selected = copy.copy(game_audio)
    selected.cfg = dict(game_audio.cfg, modloader_mod=mod)
    context = SimpleNamespace(project_root=Path(project_root), game_audio=selected)
    source = full_track(context, row['container'], row['track'], installed=True, cancel=cancel)
    if not source or source['archive_sha256'] != row['archive_sha256']:
        raise ValueError('O áudio atual do mod mudou; consulte a fala novamente.')
    with sf.SoundFile(source['source_path']) as audio:
        first, last = round(start*audio.samplerate), round(end*audio.samplerate)
        if not 0 <= first < last <= len(audio):
            raise ValueError('A janela comprovada está fora da faixa atual do mod.')
        rate, subtype = audio.samplerate, audio.subtype
        audio.seek(first)
        pcm = audio.read(last-first, dtype='int16' if subtype == 'PCM_16' else 'float32', always_2d=True)
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Audição cancelada.')
    if _ui_sha256(source['archive_path']) != row['archive_sha256']:
        raise ValueError('O banco mudou durante a leitura do trecho.')
    target = _stream_line_cache(project_root, info, row)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(uuid.uuid4().hex+'.wav')
    try:
        sf.write(temporary, pcm, rate, subtype='PCM_16' if subtype == 'PCM_16' else 'FLOAT')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    target.with_suffix('.json').write_text(json.dumps(dict(sha256=sha256_arquivo(target),
        archive_sha256=row['archive_sha256'], source_sha256=source['source_sha256'],
        start_frame=first, end_frame=last, sample_rate=rate)), encoding='utf-8')
    return target


def _change_time(path, stat):
    if os.name != 'nt':
        return stat.st_ctime_ns
    # Windows st_ctime is creation time, not the last metadata/content change.
    import ctypes
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ('creation', 'access', 'write', 'change')] + [('attributes', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        info = BasicInfo()
        return info.change if kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)) else None
    finally:
        kernel.CloseHandle(handle)


def _ui_sha256(path):
    path = Path(path).resolve()
    stat = path.stat()
    change = _change_time(path, stat)
    # NTFS can coalesce timestamps for rapid writes. A freshly touched file
    # is rehashed until its change time settles; stable large banks still
    # benefit from caching, and installation always hashes strictly.
    change_ns = ((change - 116444736000000000) * 100 if os.name == 'nt' else change) if change is not None else None
    settled = change_ns is not None and time.time_ns() - change_ns >= 1_000_000_000
    fingerprint = (stat.st_dev, stat.st_ino, stat.st_size,
                   stat.st_mtime_ns, change)
    cached = _UI_HASHES.get(path)
    if settled and cached and cached[0] == fingerprint:
        _UI_HASHES.move_to_end(path)
        return cached[1]
    digest = sha256_arquivo(path)
    after = path.stat()
    if fingerprint == (after.st_dev, after.st_ino, after.st_size,
                       after.st_mtime_ns, _change_time(path, after)) and settled:
        _UI_HASHES[path] = (fingerprint, digest)
        _UI_HASHES.move_to_end(path)
        while len(_UI_HASHES) > 128:
            _UI_HASHES.popitem(last=False)
    return digest


def file_signature(path):
    path = Path(path)
    if not path.is_file():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, sha256_arquivo(path)


class ExternalFileWatcher:
    def __init__(self):
        self.paths, self.signatures = {}, {}

    def watch(self, name, path):
        self.paths[name] = Path(path)
        self.accept(name)

    def changed(self, name):
        return name in self.paths and file_signature(self.paths[name]) != self.signatures[name]

    def accept(self, name, path=None):
        if path is not None:
            self.paths[name] = Path(path)
        self.signatures[name] = file_signature(self.paths[name])


def detect_mod_from_gxt(path, game_audio):
    path = Path(path).resolve()
    base = game_audio.modloader_dir()
    if not base or not path.is_file():
        return None
    try:
        relative = path.relative_to(base.resolve())
    except ValueError:
        return None
    return relative.parts[0] if len(relative.parts) >= 3 and relative.parts[-2].casefold() == 'text' else None


def installed_line_evidence(info, project_root, game_audio, take_path=None, mod=None):
    root = Path(project_root).resolve()
    base = game_audio.modloader_dir()
    result = dict(status='unavailable', state='not_found', path=None, verified=False, matches_take=None)
    if not base:
        return result
    mod = mod or game_audio.mod_ativo()
    if mod in ('', '.', '..') or any(ch in mod for ch in '/\\:'):
        return result
    directory = base / mod
    def expose(path, source_sha=None, expected_sha=None):
        path = Path(path)
        if not path.is_file() or (expected_sha and _ui_sha256(path) != expected_sha):
            return result
        match = None
        if take_path and Path(take_path).is_file():
            if source_sha:
                match = _ui_sha256(take_path) == source_sha
            else:
                match = game_audio._pcm_signature(path) == game_audio._pcm_signature(take_path)
        return dict(status='verified', state='available', path=path, verified=True, matches_take=match)
    try:
        if is_stream_record(info):
            route = audio_route(info)
            archive = directory / 'audio/streams' / route['container']
            if not archive.is_file():
                return result
            result.update(status='embedded_unindexed', state='needs_extract', archive_path=archive)
            proof = root / 'workspace/stream_installs' / (hashlib.sha256(mod.encode()).hexdigest()[:16] + '.json')
            data = cached_json(proof)
            row = data.get('lines', {}).get(info.get('gxt_stable_id'), {})
            if not row or (row.get('container'), row.get('track')) != (route['container'], route['track']):
                scene = data.get('scenes', {}).get(route['container']+'/'+route['track'], {})
                row = scene.get('auditions', {}).get(info.get('gxt_stable_id'), {})
            if not row:
                row = data.get('layers', {}).get('/'.join((route['container'], route['track'], info.get('gxt_stable_id', ''))), {})
            if not row:
                return result
            if row.get('archive_sha256') != _ui_sha256(archive):
                result['state'] = 'stale'
                return result
            result.update(state='proveninstalled', proof=row)
            if not row.get('audition_path'):
                if all(k in row for k in ('start', 'end')):
                    cached = _stream_line_cache(root, info, row)
                    if cached.is_file():
                        metadata = cached_json(cached.with_suffix('.json'))
                        evidence = expose(cached, row.get('source_take_sha256'), metadata.get('sha256'))
                        if evidence.get('path'):
                            evidence.update(state='proveninstalled', proof=row)
                            return evidence
                return result
            path = Path(row.get('audition_path', ''))
            path.resolve().relative_to(root)
            evidence = expose(path, row.get('source_take_sha256'), row.get('audition_sha256'))
            if evidence.get('path'):
                evidence.update(state='proveninstalled', proof=row)
            if row.get('mixed_scene'):
                evidence['matches_take'] = None
            return evidence
        ref = game_audio.ref_para(info)
        if not ref:
            return result
        pak, bank, sound = ref
        loose = directory / ('audio/SFX/%s/Bank_%03d/sound_%03d.wav' % (pak, bank, sound))
        if loose.is_file():
            return expose(loose)
        archive = directory / 'audio/SFX' / pak
        if archive.is_dir():
            slot = sfx_slot(info, game_audio, mod)
            if slot:
                cached = cached_sfx_slot(slot, root)
                if cached:
                    return expose(cached)
                result.update(status='embedded_unindexed', state='needs_extract', archive_path=slot['path'])
            return result
        if not archive.is_file():
            return result
        result['status'] = 'embedded_unindexed'
        physical_slot = sfx_slot(info, game_audio, mod)
        result['state'] = 'needs_extract' if physical_slot else 'not_found'
        manifest = game_audio._manifest()
        lookup = directory / 'audio/CONFIG/BankLkup.dat'
        for row in reversed(manifest.get('native_archives', {}).get(mod, [])):
            if row.get('pak') != pak or row.get('archive_sha256') != _ui_sha256(archive):
                continue
            if not lookup.is_file() or row.get('banklkup_sha256') != _ui_sha256(lookup):
                continue
            for slot in row.get('slots_verified', []):
                if tuple(slot.get('ref', ())) != tuple(ref):
                    continue
                path = Path(slot.get('audition_path', ''))
                path.resolve().relative_to(root)
                evidence = expose(path, slot.get('source_take_sha256'), slot.get('audition_sha256'))
                if evidence.get('path'):
                    evidence.update(state='proveninstalled', proof=slot)
                return evidence
        cached = game_audio.instalado_para(info, mod)
        if cached:
            return expose(cached)
        if physical_slot:
            cached = cached_sfx_slot(physical_slot, root)
            if cached:
                return expose(cached)
    except (OSError, ValueError, KeyError, TypeError):
        return result
    return result
