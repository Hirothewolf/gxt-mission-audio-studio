"""Conservative, provenance-preserving voice grouping for original SCRIPT audio.

The module deliberately has no dependency on a particular embedding model. A caller
supplies ``embedder(path)`` and may expose ``model_id``/``version`` attributes for
cache separation. Similarities are cosine scores, never probabilities.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import wave
import copy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from studio.script.mapping import ref_do_registro


_ALIASES = {
    'bigsmoke': 'Big Smoke', 'big smoke': 'Big Smoke',
    'ryder': 'Ryder', 'cj': 'CJ', 'carl johnson': 'CJ',
}

# These fields are declarations about the audio track itself. Caption count,
# GXT keys, WAV channels, and inferred embedding clusters are deliberately not
# treated as speaker-layout evidence.
_LAYOUT_FIELDS = ('speaker_layout', 'track_layout', 'multi_speaker',
                  'is_multi_speaker', 'multi_speaker_track', 'speaker_count',
                  'speakers')
_MULTI_SPEAKER_LABELS = {
    'multi speaker', 'multi speakers', 'multi-speaker', 'multi-speakers',
    'multiple speakers', 'multiple-speaker', 'multiple-speakers',
}


def _declares_multiple_speakers(field, value):
    """Recognize explicit track-layout declarations without inferring from captions."""
    if field in ('multi_speaker', 'is_multi_speaker', 'multi_speaker_track'):
        return value is True or (isinstance(value, str) and value.strip().casefold() in
                                 ('true', 'yes', '1', 'multi_speaker', 'multi-speaker'))
    if field == 'speaker_count':
        try:
            return int(value) >= 2
        except (TypeError, ValueError):
            return False
    if field == 'speakers':
        return isinstance(value, (list, tuple, set)) and len(value) >= 2
    if field in ('speaker_layout', 'track_layout'):
        if isinstance(value, dict):
            if _declares_multiple_speakers('speakers', value.get('speakers')):
                return True
            value = value.get('layout', value.get('mode', value.get('type', value.get('kind'))))
        label = str(value or '').strip().casefold().replace('_', ' ')
        return label in _MULTI_SPEAKER_LABELS
    return False


def _layout_evidence(item):
    """Return preserved explicit declarations that rule out a single-voice claim."""
    found = []
    for source in [item] + list(item.get('links') or ()):
        if not isinstance(source, dict):
            continue
        for field in _LAYOUT_FIELDS:
            if field in source and _declares_multiple_speakers(field, source[field]):
                row = {'field': field, 'value': copy.deepcopy(source[field])}
                if source is not item:
                    for key in ('line_id', 'table'):
                        if source.get(key):
                            row[key] = source[key]
                if row not in found:
                    found.append(row)
    return found


def _canonical_identity(value):
    name = str(value or '').strip()
    return _ALIASES.get(name.casefold(), name)


def coordinate_key(info):
    """Return a one-based ``SCRIPT:bank:sound`` coordinate key, or None."""
    if not isinstance(info, dict):
        return None
    explicit = next((str(info.get(k) or '').strip().upper()
                     for k in ('pak', 'archive', 'package', 'sfx_archive', 'container')
                     if str(info.get(k) or '').strip()), '')
    if explicit and explicit != 'SCRIPT':
        return None
    # Keep precedence and index handling aligned with the shared parser: explicit
    # bank_num/sound_num are one-based and outrank zero-based stable IDs.
    ref = ref_do_registro(info)
    if not ref or str(ref[0]).upper() != 'SCRIPT':
        return None
    try:
        bank, sound = int(ref[1]), int(ref[2])
    except (TypeError, ValueError):
        return None
    if bank < 1 or sound < 1:
        return None
    return 'SCRIPT:%d:%d' % (bank, sound)


def _audio_id(bank, sound):
    return 'SCRIPT:B%03d:S%03d' % (bank, sound)


def _key_to_audio_id(key):
    match = re.fullmatch(r'SCRIPT:(\d+):(\d+)', str(key or ''), re.I)
    return _audio_id(int(match.group(1)), int(match.group(2))) if match else None


def _audio_id_coordinate(audio_id):
    match = re.fullmatch(r'SCRIPT:B(\d+):S(\d+)', str(audio_id or ''), re.I)
    return 'SCRIPT:%d:%d' % (int(match.group(1)), int(match.group(2))) if match else None


def _record_hash(record):
    for key in ('hash', 'jamcrc', 'gxt_hash', 'text_hash', 'crc'):
        value = record.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def _review_line_id(record, table):
    existing = str(record.get('line_id') or '')
    if '|' in existing:
        return existing
    value = record.get('jamcrc', record.get('crc'))
    if table and value not in (None, ''):
        try:
            number = int(value, 16) if isinstance(value, str) else int(value)
            return '%s|%08x' % (table, number & 0xffffffff)
        except (TypeError, ValueError):
            pass
    return existing or str(record.get('gxt_stable_id') or record.get('id') or '')


def _seed_for_links(links, review_store):
    manual = set()
    verified = set()
    for link in links:
        line_id = link.get('line_id')
        if review_store is not None and line_id:
            try:
                value = str(review_store.character(line_id) or '').strip()
            except (AttributeError, KeyError, ValueError):
                value = ''
            if value:
                manual.add(value)
        # Manifest identities are usable as seeds only when explicitly VERIFIED.
        # WIKI exact-text results are intentionally excluded.
        if str(link.get('speaker_identity_status') or '').upper() == 'VERIFIED':
            value = _canonical_identity(link.get('speaker_identity'))
            if value:
                verified.add(value)
    if len(manual) == 1:
        return next(iter(manual)), {'seed_source': 'manual_review'}
    if len(manual) > 1:
        return None, {'seed_conflict': {'source': 'manual_review', 'identities': sorted(manual)}}
    if len(verified) == 1:
        return next(iter(verified)), {'seed_source': 'manifest_verified'}
    if len(verified) > 1:
        return None, {'seed_conflict': {'source': 'manifest_verified', 'identities': sorted(verified)}}
    return None, {}


def collect_original_items(game, manifest_records, review_store=None, tables=None, limit=None):
    """Collect slots from a current ORIGINAL GameAudio SCRIPT cache only.

    Every slot in the selected coordinate scope is returned, including slots with no
    manifest/GXT link. Neither project extraction trees nor ModLoader indices are read.
    """
    if game is None or not callable(getattr(game, '_cache_original_atual', None)):
        return []
    try:
        if not game._cache_original_atual('SCRIPT'):
            return []
    except (OSError, ValueError, TypeError):
        return []
    try:
        original_index = dict(getattr(game, 'indice', {}) or {})
    except (TypeError, ValueError):
        return []
    if tables is None:
        allowed_coordinates = None
        selected_tables = None
    else:
        selected_tables = {str(x).strip().casefold() for x in ([tables] if isinstance(tables, str) else tables)}
        allowed_coordinates = set()

    links_by_key = {}
    for record in manifest_records or ():
        if not isinstance(record, dict):
            continue
        key = coordinate_key(record)
        if not key:
            continue
        table = str(record.get('table') or record.get('table_name') or '')
        if not table:
            gxt_id = str(record.get('gxt_stable_id') or '')
            if ':' in gxt_id:
                table = gxt_id.split(':', 1)[0].strip()
        if selected_tables is not None and table.strip().casefold() in selected_tables:
            allowed_coordinates.add(key)
        # The general report includes all links; selected-table analysis includes
        # only those links while still only selecting its coordinates.
        if selected_tables is not None and table.strip().casefold() not in selected_tables:
            continue
        links_by_key.setdefault(key, []).append({
            'line_id': _review_line_id(record, table),
            'table': table,
            'hash': _record_hash(record),
            'source_text': str(record.get('source_text') or record.get('text_original_en') or
                               record.get('original_text') or record.get('text') or ''),
            'speaker_identity': record.get('speaker_identity'),
            'speaker_identity_status': record.get('speaker_identity_status'),
            **{field: copy.deepcopy(record[field]) for field in _LAYOUT_FIELDS if field in record},
        })

    rows = []
    for ref, path in original_index.items():
        try:
            pak, bank, sound = ref
            if str(pak).upper() != 'SCRIPT' or int(bank) < 1 or int(sound) < 1:
                continue
            bank, sound = int(bank), int(sound)
            key = 'SCRIPT:%d:%d' % (bank, sound)
            if allowed_coordinates is not None and key not in allowed_coordinates:
                continue
            path = Path(path).resolve()
            if not path.is_file():
                continue
            links = links_by_key.get(key, [])
            known, evidence = _seed_for_links(links, review_store)
            row = {
                'audio_id': _audio_id(bank, sound), 'path': str(path), 'links': links,
                'known_identity': known, 'evidence': evidence,
            }
            layout = _layout_evidence(row)
            if layout:
                row['layout_evidence'] = layout
                # Convenient summary for callers; detailed source fields remain
                # intact on their original links and in layout_evidence.
                row['speaker_layout'] = next((entry['value'] for entry in layout
                                              if entry['field'] in ('speaker_layout', 'track_layout')),
                                             layout[0]['value'])
            rows.append(row)
        except (OSError, TypeError, ValueError):
            continue
    rows.sort(key=lambda row: tuple(int(part) for part in re.findall(r'\d+', row['audio_id'])))
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _stat_signature(path):
    stat = Path(path).stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'ctime_ns': getattr(stat, 'st_ctime_ns', None)}


def _wav_quality(path):
    """Return duration and a conservative signal-quality result using stdlib WAV."""
    try:
        with wave.open(str(path), 'rb') as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            frames = source.getnframes()
            duration = frames / float(rate) if rate else 0.0
            if channels < 1 or width not in (1, 2, 3, 4) or rate <= 0:
                return duration, 'unsupported_wav'
            square_sum = 0.0
            crossings = 0
            count = 0
            previous = None
            while True:
                block = source.readframes(8192)
                if not block:
                    break
                stride = width * channels
                for offset in range(0, len(block) - stride + 1, stride):
                    raw = block[offset:offset + width]
                    if width == 1:
                        value = raw[0] - 128
                    else:
                        value = int.from_bytes(raw, 'little', signed=True)
                    norm = value / float(1 << (8 * width - 1))
                    square_sum += norm * norm
                    sign = value >= 0
                    if previous is not None and sign != previous:
                        crossings += 1
                    previous = sign
                    count += 1
            if duration < 1.0:
                return duration, 'too_short'
            if not count:
                return duration, 'empty_wav'
            rms = math.sqrt(square_sum / count)
            zcr = crossings / float(max(1, count - 1))
            if rms < 0.002:
                return duration, 'silent'
            if zcr > 0.38:
                return duration, 'noise_like'
            return duration, None
    except (OSError, EOFError, wave.Error, ValueError):
        return 0.0, 'invalid_wav'


def _unit_vector(values):
    vector = [float(value) for value in values]
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError('Embedder returned an invalid vector.')
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        raise ValueError('Embedder returned a zero vector.')
    return [value / norm for value in vector]


def _cosine(left, right):
    if len(left) != len(right):
        return -1.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))


class VoiceIdentityStore:
    """Separate report/cache/confirmed-identity storage under workspace/voice_identity."""

    def __init__(self, root):
        self.root = Path(root).resolve() / 'workspace' / 'voice_identity'
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.root / 'latest.json'
        self.overlay_path = self.root / 'confirmed.json'
        self.cache_path = self.root / 'embeddings.sqlite3'
        self._lock = threading.RLock()
        self._json_cache = {}
        self._hash_cache = {}
        self._report_item_index = None
        self._latest_raw = None
        self._latest_signature = None
        self._initialize_cache()

    def _initialize_cache(self):
        with closing(sqlite3.connect(self.cache_path)) as db:
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS embeddings (
                    audio_sha256 TEXT NOT NULL, model_id TEXT NOT NULL, model_version TEXT NOT NULL,
                    vector_json TEXT NOT NULL, PRIMARY KEY(audio_sha256, model_id, model_version))''')

    def _read_json(self, path, default=None, copy_value=True):
        path = Path(path)
        if not path.exists():
            return default
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, getattr(stat, 'st_ctime_ns', None))
            cache_key = str(path.resolve())
            cached = self._json_cache.get(cache_key)
            if cached and cached[0] == signature:
                return copy.deepcopy(cached[1]) if copy_value else cached[1]
        except OSError:
            signature = None
            cache_key = str(path)
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Invalid voice identity data preserved: %s' % path) from exc
        if not isinstance(value, dict):
            raise ValueError('Invalid voice identity data preserved: %s' % path)
        self._json_cache[cache_key] = (signature, copy.deepcopy(value))
        return value if copy_value else self._json_cache[cache_key][1]

    def _load_latest_raw(self):
        if not self.latest_path.exists():
            return None
        stat = self.latest_path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, getattr(stat, 'st_ctime_ns', None))
        if signature == self._latest_signature and self._latest_raw is not None:
            return self._latest_raw
        report = self._read_json(self.latest_path, copy_value=False)
        if not isinstance(report.get('items'), list) or not report.get('id'):
            raise ValueError('Invalid voice identity report preserved: %s' % self.latest_path)
        self._latest_signature = signature
        self._latest_raw = report
        self._report_item_index = {row.get('audio_id'): row for row in report['items']}
        return report

    def latest(self):
        report = self._load_latest_raw()
        if report is None:
            return None
        report = copy.deepcopy(report)
        overlay = self._read_json(self.overlay_path, {'version': 1, 'assignments': {}})
        if overlay.get('version') == 1 and isinstance(overlay.get('assignments'), dict):
            assignments = overlay['assignments']
            for group in report['groups']:
                assigned = [assignments.get(audio_id) for audio_id in group.get('item_ids', [])]
                if assigned and all(row and row.get('report_id') == report.get('id') and
                                    row.get('group_id') == group.get('id') for row in assigned):
                    identities = {row.get('identity') for row in assigned}
                    if len(identities) == 1:
                        group['status'] = 'CONFIRMED'
                        group['confirmed_identity'] = next(iter(identities))
                        group['review_status'] = 'CONFIRMED'
        return report

    @staticmethod
    def _atomic_json(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
        fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temp), str(path))
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    def publish(self, report):
        if not isinstance(report, dict) or not report.get('id') or not isinstance(report.get('items'), list):
            raise ValueError('A complete report is required for publication.')
        with self._lock:
            # Parse first: malformed prior data must never be silently replaced.
            self.latest()
            self._atomic_json(self.latest_path, report)
            self._json_cache.pop(str(self.latest_path.resolve()), None)
            self._report_item_index = None
            self._latest_raw = None
            self._latest_signature = None
        return report

    def embedding(self, audio_sha256, model_id, model_version):
        with closing(sqlite3.connect(self.cache_path)) as db:
            row = db.execute('SELECT vector_json FROM embeddings WHERE audio_sha256=? AND model_id=? AND model_version=?',
                             (audio_sha256, model_id, model_version)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_embedding(self, audio_sha256, model_id, model_version, vector):
        payload = json.dumps(vector, separators=(',', ':'))
        with closing(sqlite3.connect(self.cache_path)) as db:
            with db:
                db.execute('INSERT OR IGNORE INTO embeddings VALUES (?, ?, ?, ?)',
                           (audio_sha256, model_id, model_version, payload))

    def assign_group(self, report_id, group_id, character):
        character = str(character or '').strip()
        if not character:
            raise ValueError('A character name is required.')
        with self._lock:
            report = self.latest()
            if not report or report.get('id') != report_id:
                raise ValueError('Only the latest report can be confirmed.')
            group = next((row for row in report.get('groups', []) if row.get('id') == group_id), None)
            if not group or not group.get('item_ids'):
                raise ValueError('The selected group is not in the latest report.')
            by_id = {row.get('audio_id'): row for row in report['items']}
            overlay = self._read_json(self.overlay_path, {'version': 1, 'assignments': {}})
            if overlay.get('version') != 1 or not isinstance(overlay.get('assignments'), dict):
                raise ValueError('Invalid confirmed identity overlay preserved: %s' % self.overlay_path)
            entries = []
            for audio_id in group['item_ids']:
                item = by_id.get(audio_id)
                if item is None or not item.get('path'):
                    raise ValueError('The latest report has an incomplete source entry.')
                path = Path(item['path'])
                if not path.is_file() or _sha256(path) != item.get('content_sha256') or _stat_signature(path) != item.get('stat_signature'):
                    raise ValueError('Audio source changed after analysis: %s' % path)
                if audio_id in overlay['assignments']:
                    raise ValueError('This audio already has a confirmed voice identity.')
                entries.append((audio_id, {
                    'identity': character, 'report_id': report_id, 'group_id': group_id,
                    'path': str(path.resolve()), 'content_sha256': item['content_sha256'],
                    'stat_signature': item['stat_signature'], 'model_id': report.get('model_id'),
                    'score': item.get('score'), 'confirmed_at': datetime.now(timezone.utc).isoformat(),
                }))
            updated = {'version': 1, 'assignments': dict(overlay['assignments'])}
            updated['assignments'].update(entries)
            self._atomic_json(self.overlay_path, updated)
            self._json_cache.pop(str(self.overlay_path.resolve()), None)
        return {'report_id': report_id, 'group_id': group_id, 'identity': character,
                'item_ids': [audio_id for audio_id, _ in entries]}

    def lookup(self, info, original_path=None):
        key = coordinate_key(info)
        audio_id = _key_to_audio_id(key)
        if not audio_id or original_path is None:
            return None
        try:
            path = Path(original_path).resolve()
            if not path.is_file():
                return None
        except OSError:
            return None
        overlay = self._read_json(self.overlay_path, {'version': 1, 'assignments': {}}, copy_value=False)
        assignment = overlay.get('assignments', {}).get(audio_id) if overlay.get('version') == 1 else None
        report = self._load_latest_raw()
        item = self._report_item_index.get(audio_id) if self._report_item_index is not None else None
        if (not assignment or assignment.get('path') != str(path)) and (
                not item or Path(item.get('path', '')).resolve() != path):
            return None
        try:
            signature = _stat_signature(path)
            assignment_match = bool(assignment and assignment.get('path') == str(path) and
                                    assignment.get('stat_signature') == signature)
            item_match = bool(item and Path(item.get('path', '')).resolve() == path and
                              item.get('stat_signature') == signature)
            if not assignment_match and not item_match:
                return None
            digest = self._cached_hash(path, signature)
        except OSError:
            return None
        if (assignment_match and assignment.get('content_sha256') == digest):
            return {'status': 'VOICE_CONFIRMED', 'identity': assignment.get('identity'),
                    'score': assignment.get('score'), 'report_id': assignment.get('report_id'),
                    'group_id': assignment.get('group_id'), 'model_id': assignment.get('model_id')}
        if not report:
            return None
        if (not item or item.get('content_sha256') != digest or item.get('stat_signature') != signature or
                item.get('model_id') != report.get('model_id')):
            return None
        identity = item.get('suggested_identity')
        if item.get('status') == 'SUGGESTED' and identity:
            return {'status': 'VOICE_SUGGESTED', 'identity': identity, 'score': item.get('score'),
                    'margin': item.get('margin'), 'report_id': report.get('report_id', report.get('id')),
                    'group_id': item.get('group_id'), 'model_id': report.get('model_id')}
        return None

    def _cached_hash(self, path, signature):
        key = str(Path(path).resolve())
        source_signature = (signature['size'], signature['mtime_ns'], signature.get('ctime_ns'))
        cached = self._hash_cache.get(key)
        if cached and cached[0] == source_signature:
            return cached[1]
        digest = _sha256(path)
        self._hash_cache[key] = (source_signature, digest)
        return digest


def _cancelled(cancel):
    if cancel is None:
        return False
    checker = getattr(cancel, 'is_set', None)
    return bool(checker()) if callable(checker) else bool(cancel()) if callable(cancel) else bool(cancel)


class AnalysisCancelled(RuntimeError):
    pass


def _embedding_for(path, digest, embedder, store, model_id, model_version):
    cached = store.embedding(digest, model_id, model_version)
    if cached is not None:
        return _unit_vector(cached), True
    vector = _unit_vector(embedder(path))
    store.cache_embedding(digest, model_id, model_version, vector)
    return vector, False


def _choose_references(candidates, max_references):
    selected = []
    remaining = sorted(candidates, key=lambda row: (-row['duration'], row['audio_id']))
    while remaining and len(selected) < max_references:
        if not selected:
            choice = remaining[0]
        else:
            # Farthest-first cosine selection after preferring long clean clips.
            choice = max(remaining, key=lambda row: (
                min(1.0 - _cosine(row['vector'], prior['vector']) for prior in selected),
                row['duration'], row['audio_id']))
        selected.append(choice)
        remaining.remove(choice)
    return selected


def analyze_items(items, embedder, store, on_progress=None, cancel=None,
                  min_score=.60, min_margin=.08, max_references=8, reference_items=None):
    """Analyze target items against known original-audio seeds and publish on success."""
    if not callable(embedder):
        raise TypeError('embedder must be callable with an audio path.')
    default_model_id = '%s.%s' % (
        getattr(embedder, '__module__', type(embedder).__module__),
        getattr(embedder, '__qualname__', type(embedder).__qualname__),
    )
    model_id = str(getattr(embedder, 'model_id', None) or getattr(embedder, 'name', None) or default_model_id)
    model_version = str(getattr(embedder, 'version', getattr(embedder, 'model_version', '1')))
    targets = [dict(item) for item in items]
    references_input = [dict(item) for item in (reference_items or ())]
    records = []
    by_sha = {}
    embedding_hits = 0
    duplicate_count = 0
    all_inputs = []
    seen_ids = set()
    for role, rows in (('target', targets), ('reference', references_input)):
        for item in rows:
            if _cancelled(cancel):
                raise AnalysisCancelled('Voice analysis was canceled.')
            audio_id = str(item.get('audio_id') or '')
            if not audio_id:
                continue
            if role == 'target' and audio_id in seen_ids:
                raise ValueError('Target audio IDs must be unique: %s' % audio_id)
            if role == 'target':
                seen_ids.add(audio_id)
            path = Path(item.get('path') or '').resolve()
            entry = dict(item, audio_id=audio_id, path=str(path), role=role)
            try:
                entry['content_sha256'] = _sha256(path)
                entry['stat_signature'] = _stat_signature(path)
            except OSError as exc:
                entry.update(status='UNANALYSABLE', reason='missing_or_unreadable', vector=None, duration=0.0)
                records.append(entry)
                if role == 'target':
                    all_inputs.append(entry)
                if on_progress:
                    on_progress(len(records), len(targets) + len(references_input), audio_id)
                continue
            duration, reason = _wav_quality(path)
            entry['duration'] = duration
            entry['reason'] = reason
            if reason:
                entry.update(status='UNANALYSABLE', vector=None)
            elif _layout_evidence(entry):
                entry.update(status='MULTI_SPEAKER', reason='explicit_multi_speaker_layout', vector=None)
            else:
                digest = entry['content_sha256']
                if digest in by_sha:
                    entry['vector'] = by_sha[digest]['vector']
                    entry['status'] = 'READY'
                    duplicate_count += 1
                else:
                    try:
                        entry['vector'], hit = _embedding_for(path, digest, embedder, store, model_id, model_version)
                        entry['status'] = 'READY'
                        embedding_hits += int(hit)
                        by_sha[digest] = entry
                    except (ValueError, TypeError, RuntimeError, OSError) as exc:
                        entry.update(status='UNANALYSABLE', reason='embedding_error: %s' % exc, vector=None)
            records.append(entry)
            if role == 'target':
                all_inputs.append(entry)
            if on_progress:
                on_progress(len(records), len(targets) + len(references_input), audio_id)

    if _cancelled(cancel):
        raise AnalysisCancelled('Voice analysis was canceled.')

    seeds = {}
    reference_ids = set()
    reference_hashes = set()
    for record in records:
        if record['status'] != 'READY' or record.get('duration', 0) < 1.0:
            continue
        if _layout_evidence(record):
            continue
        if reference_items is not None:
            # Explicit reference pools are authoritative. This lets callers hold
            # verified pilot targets out by omitting their ids/content hashes.
            if record.get('role') != 'reference':
                continue
        identity = str(record.get('known_identity') or '').strip()
        if not identity or record.get('evidence', {}).get('seed_conflict'):
            continue
        identity = _canonical_identity(identity)
        seeds.setdefault(identity, []).append(record)
        if record.get('role') == 'reference':
            reference_ids.add(record['audio_id'])
            reference_hashes.add(record['content_sha256'])
    references = {identity: _choose_references(rows, max(1, int(max_references)))
                  for identity, rows in seeds.items()}

    # Cluster only unseeded target items, and only as unlabeled groups. Never use
    # these clusters as references for character identity suggestions.
    output_items = []
    suggested_groups = {}
    unlabeled = []
    for record in all_inputs:
        if _cancelled(cancel):
            raise AnalysisCancelled('Voice analysis was canceled.')
        result = {key: record.get(key) for key in (
            'audio_id', 'path', 'content_sha256', 'stat_signature', 'links',
            'speaker_layout', 'layout_evidence') if key in record}
        known_identity = record.get('known_identity')
        result.update(known_identity=_canonical_identity(known_identity) if known_identity else None,
                      evidence=record.get('evidence', {}),
                      model_id=model_id, score=None, margin=None, suggested_identity=None, group_id=None)
        if record['status'] == 'MULTI_SPEAKER':
            result.update(status='MULTI_SPEAKER', reason='explicit_multi_speaker_layout')
        elif record['status'] == 'UNANALYSABLE':
            result.update(status='UNANALYSABLE', reason=record.get('reason') or 'invalid_audio')
        elif record.get('evidence', {}).get('seed_conflict'):
            result.update(status='SEED_CONFLICT', reason='conflicting_known_identities')
        else:
            candidate_scores = {}
            for identity, refs in references.items():
                # Content duplicates of a seed are held out, preventing seed leakage.
                eligible = [ref for ref in refs if ref.get('content_sha256') != record['content_sha256']]
                if eligible:
                    candidate_scores[identity] = max(_cosine(record['vector'], ref['vector']) for ref in eligible)
            ordered = sorted(candidate_scores.items(), key=lambda pair: (-pair[1], pair[0]))
            if not ordered:
                result.update(status='UNKNOWN', reason='no_eligible_known_references')
                unlabeled.append(record)
            else:
                top_identity, top_score = ordered[0]
                second_score = ordered[1][1] if len(ordered) > 1 else -1.0
                margin = top_score - second_score
                result['score'], result['margin'] = top_score, margin
                if top_score < float(min_score):
                    result.update(status='UNKNOWN', reason='below_min_score')
                    unlabeled.append(record)
                elif margin < float(min_margin):
                    result.update(status='AMBIGUOUS', reason='insufficient_speaker_margin')
                    unlabeled.append(record)
                else:
                    result.update(status='SUGGESTED', suggested_identity=top_identity)
                    suggested_groups.setdefault(top_identity, []).append(record)
        output_items.append(result)

    groups = []
    output_by_id = {item['audio_id']: item for item in output_items}
    for identity, rows in sorted(suggested_groups.items()):
        group_id = 'voice-' + hashlib.sha1((identity + '\0' + '\0'.join(sorted(r['audio_id'] for r in rows))).encode()).hexdigest()[:12]
        group = {'id': group_id, 'identity': identity, 'status': 'SUGGESTED',
                 'item_ids': [row['audio_id'] for row in rows], 'count': len(rows),
                 'representative_paths': [row['path'] for row in sorted(rows, key=lambda r: (-r['duration'], r['audio_id']))[:3]]}
        groups.append(group)
        for row in rows:
            output_by_id[row['audio_id']]['group_id'] = group_id

    remaining = list(unlabeled)
    while remaining:
        first = remaining.pop(0)
        cluster = [first]
        if first.get('vector') is not None:
            matched = [row for row in remaining if row.get('vector') is not None and
                       row.get('content_sha256') != first.get('content_sha256') and
                       _cosine(first['vector'], row['vector']) >= .75]
            for row in matched:
                remaining.remove(row)
                cluster.append(row)
        group_id = 'unlabeled-' + hashlib.sha1('\0'.join(sorted(r['audio_id'] for r in cluster)).encode()).hexdigest()[:12]
        groups.append({'id': group_id, 'identity': None, 'status': 'UNLABELED',
                       'item_ids': [row['audio_id'] for row in cluster], 'count': len(cluster),
                       'representative_paths': [row['path'] for row in cluster[:3]]})
        for row in cluster:
            if row['audio_id'] in output_by_id:
                output_by_id[row['audio_id']]['group_id'] = group_id

    counts = {}
    for item in output_items:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    report_id = 'voice-report-' + hashlib.sha1(
        (datetime.now(timezone.utc).isoformat() + '\0' + '\0'.join(x['audio_id'] + ':' + x.get('content_sha256', '') for x in output_items)).encode()
    ).hexdigest()[:16]
    report = {
        'id': report_id, 'report_id': report_id, 'created_at': datetime.now(timezone.utc).isoformat(),
        'model_id': model_id, 'model_version': model_version,
        'thresholds': {'min_score': float(min_score), 'min_margin': float(min_margin),
                       'unlabeled_cluster_cosine': .75, 'max_references_per_identity': int(max_references)},
        'items': output_items, 'groups': groups,
        'stats': {'total': len(output_items), 'statuses': counts,
                  'unanalysable': counts.get('UNANALYSABLE', 0),
                  'unique_embeddings': len(by_sha), 'embedding_cache_hits': embedding_hits,
                  'duplicate_contents': duplicate_count, 'reference_identities': len(references),
                  'reference_items': sum(len(rows) for rows in references.values()),
                  'reference_audio_ids': sorted(reference_ids),
                  'reference_content_sha256': sorted(reference_hashes)},
    }
    if _cancelled(cancel):
        raise AnalysisCancelled('Voice analysis was canceled.')
    store.publish(report)
    return report
