"""Game-file evidence for captions. Production manifests are never rewritten."""
from collections import defaultdict, Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import struct

from studio.gxt.model import Gxt
from studio.gxt.codec import decode_text_c
from studio.gxt.jamcrc import jamcrc
from studio.script.scm import decode, definitions

TRACK_SOURCE = 'https://raw.githubusercontent.com/gta-reversed/gta-reversed/master/source/game_sa/Audio/Managers/CutSceneStreamsPC.h'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def event_coordinates(event):
    if isinstance(event, bool) or not isinstance(event, int) or not 2000 <= event <= 45400:
        return None
    bank, sound = divmod(event - 2000, 200)
    return {'pak': 'SCRIPT', 'bank_index': bank, 'sound_index': sound,
            'bank_num': bank + 1, 'sound_num': sound + 1,
            'stable_audio_id': 'SCRIPT:B%03d:S%03d' % (bank, sound), 'global_bank': bank + 147}


def audio_events(path):
    result = {}
    for line in Path(path).read_text(encoding='ascii').splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[1].isdigit():
            continue
        name = fields[0].removeprefix('SOUND_').upper()
        number = int(fields[1])
        if name in result and result[name] != number:
            raise ValueError('Ambiguous audio event: ' + name)
        result[name] = number
    return result


def read_cuts(path):
    """IMG v2 bounds and .cut text sections, with archive byte provenance."""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        magic, count = struct.unpack('<4sI', stream.read(8))
        if magic != b'VER2' or count > (size - 8) // 32:
            raise ValueError('Invalid cuts.img header')
        selected = []
        names = set()
        for _ in range(count):
            offset, streaming, stored, raw = struct.unpack('<IHH24s', stream.read(32))
            name = raw.split(b'\0')[0].decode('ascii').lower()
            if not name.endswith('.cut'):
                continue
            first, length = offset * 2048, (streaming or stored) * 2048
            if first < 8 + count * 32 or length < 1 or first + length > size or name in names:
                raise ValueError('Invalid/duplicate cutscene IMG entry: ' + name)
            names.add(name)
            selected.append((name, first, length))
        result = {}
        for name, first, length in selected:
            stream.seek(first)
            raw = stream.read(length)
            text = raw.split(b'\0')[0].decode('ascii')
            in_text, captions = False, []
            for line_number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if line.lower() == 'text':
                    in_text = True
                elif line.lower() == 'end':
                    in_text = False
                elif in_text and line and not line.startswith('#'):
                    fields = line.replace(',', ' ').split()
                    if len(fields) != 3:
                        raise ValueError('Invalid caption in ' + name)
                    start, duration = int(fields[0]), int(fields[1])
                    if start < 0 or duration <= 0:
                        raise ValueError('Invalid caption timing in ' + name)
                    captions.append({'key': fields[2].upper(), 'start_ms': start,
                                     'duration_ms': duration, 'line': line_number})
            result[name[:-4].upper()] = {'file': name, 'offset': first, 'length': length,
                                         'sha256': hashlib.sha256(raw).hexdigest(), 'captions': captions}
    return result


def track_lookup(game):
    source = Path(__file__).parents[1] / 'data' / 'CutSceneStreamsPC.h'
    pairs = re.findall(r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*\}', source.read_text())
    data = (Path(game) / 'audio' / 'CONFIG' / 'TrakLkup.dat').read_bytes()
    if len(data) % 12:
        raise ValueError('Invalid TrakLkup.dat length')
    entries = [struct.unpack_from('<B3xII', data, pos) for pos in range(0, len(data), 12)]
    cutscene_ids = [i for i, row in enumerate(entries) if row[0] == 8]
    archive_size = (Path(game) / 'audio' / 'streams' / 'CUTSCENE').stat().st_size
    if len(cutscene_ids) != 141:
        raise ValueError('Unsupported PC cutscene stream layout')
    result = {}
    for name, identifier in pairs:
        identifier = int(identifier)
        if identifier not in cutscene_ids:
            raise ValueError('Cutscene track table disagrees with game lookup')
        pak, offset, length = entries[identifier]
        if length <= 0 or offset + length > archive_size:
            raise ValueError('Cutscene stream bounds mismatch')
        result[name.upper()] = {'global_track_id': identifier,
                               'track': 'Track_%03d' % (cutscene_ids.index(identifier) + 1),
                               'stream_offset': offset, 'stream_bytes': length,
                               'mapping_source': TRACK_SOURCE}
    return result


def build(game, existing_records=(), progress=None):
    game = Path(game).resolve()
    paths = {'scm': game / 'data/script/main.scm', 'gxt': game / 'text/american.gxt',
             'events': game / 'data/AudioEvents.txt', 'cuts': game / 'anim/cuts.img',
             'lookup': game / 'audio/CONFIG/TrakLkup.dat', 'stream': game / 'audio/streams/CUTSCENE'}
    sources = {name: {'path': str(path), 'sha256': digest(path)} for name, path in paths.items()}
    if progress:
        progress(0, 4, 'Decodificando main.scm e as tabelas de missão…')
    segments = decode(paths['scm'].read_bytes())
    incomplete = [{'id': x['id'], 'error': x['error']} for x in segments if x['error']]
    commands = definitions()['commands']
    branch_count, invalid_branches = 0, []
    boundaries = {i['offset'] for segment in segments for i in segment['instructions']}
    for segment in segments:
        for instruction in segment['instructions']:
            schema = commands[instruction['opcode']].get('input', [])
            for param, field in zip(instruction['params'], schema):
                if field.get('type') != 'label' or not isinstance(param['value'], int):
                    continue
                branch_count += 1
                value = param['value']
                target = value if value >= 0 else segment['start'] - value
                if target not in boundaries:
                    invalid_branches.append({'offset': instruction['offset'], 'target': target})
    gxt = Gxt(paths['gxt'])
    locations = defaultdict(list)
    for table in gxt.order:
        for key in gxt.t[table]['hashes']:
            locations[key].append(table)
    events = audio_events(paths['events'])
    cuts, tracks = read_cuts(paths['cuts']), track_lookup(game)
    records, uses, named_keys, audio_calls, scene_calls = [], defaultdict(list), defaultdict(set), [], []
    for segment in segments:
        rows = segment['instructions']
        tables = {i['params'][0]['value'].upper() for i in rows if i['opcode'] == '054C' and i['params'][0]['value']}
        table = next(iter(tables)) if len(tables) == 1 else None
        for instruction in rows:
            for index, param in enumerate(instruction['params']):
                schema = commands[instruction['opcode']].get('input', [])
                if instruction['opcode'] not in ('05A9', '05AA') and (index >= len(schema) or schema[index].get('type') != 'gxt_key'):
                    continue
                if param['type'] not in (9, 14, 15) or not isinstance(param['value'], str):
                    continue
                name = param['value'].upper()
                crc = jamcrc(name)
                matches = [table] if table and table in locations[crc] else [t for t in locations[crc] if t in (tables | {'MAIN'})]
                if len(matches) == 1:
                    gid = '%s:%08X' % (matches[0], crc)
                    named_keys[gid].add(name)
                    uses[gid].append({'offset': instruction['offset'], 'opcode': instruction['opcode'],
                                      'command': instruction['name'], 'segment': segment['id'], 'key': name})
            if instruction['opcode'] == '03CF':
                audio_calls.append({'offset': instruction['offset'], 'segment': segment['id'],
                                    'table': table, 'event_id': instruction['params'][1]['value'],
                                    'indirect': instruction['params'][1]['value'] is None})
            if instruction['opcode'] == '02E4' and instruction['params'][0]['value']:
                scene_calls.append({'scene': instruction['params'][0]['value'].upper(),
                                    'table': table, 'offset': instruction['offset'], 'segment': segment['id']})
    # These are exact literal-name + game event ID + GXT hash associations. A
    # literal text name alone is not a proof of actual audio playback.
    script_keys = set()
    for gid, names in named_keys.items():
        candidates = [(name, event_coordinates(events[name])) for name in names if name in events]
        candidates = [(name, coord) for name, coord in candidates if coord]
        if len(candidates) != 1:
            continue
        name, coord = candidates[0]
        table, crc = gid.split(':')
        records.append(dict(coord, table=table, gxt_stable_id=gid, jamcrc=crc, event_name=name,
                            event_id=events[name], state='MAPPED_NATIVE_SCRIPT',
                            evidence_method='SCM_LITERAL_GAME_EVENT_GXT_HASH', scm_uses=uses[gid],
                            caption_cooccurrence_verified=False, production_approved=False))
        script_keys.add(gid)
    scene_context = defaultdict(list)
    for call in scene_calls:
        scene_context[call['scene']].append(call)
    cut_keys = set()
    for scene, metadata in cuts.items():
        contexts = scene_context.get(scene, [])
        context_tables = {row['table'] for row in contexts if row['table']}
        for caption in metadata['captions']:
            key, crc = caption['key'], jamcrc(caption['key'])
            matches = [t for t in locations[crc] if t in context_tables] or [t for t in locations[crc] if t == 'MAIN']
            if len(matches) != 1:
                continue
            table = matches[0]
            gid = '%s:%08X' % (table, crc)
            records.append({'pak': 'CUTSCENE', 'gxt_stable_id': gid, 'jamcrc': '%08X' % crc,
                            'table': table, 'event_name': key, 'scene': scene,
                            'stable_audio_id': 'CUTSCENE:%s:%s' % (scene, key),
                            'state': 'MAPPED_NATIVE_CUTSCENE', 'evidence_method': 'IMG_CUT_TEXT_GXT_HASH',
                            'cut_metadata': {k: v for k, v in metadata.items() if k != 'captions'},
                            'caption_start_ms': caption['start_ms'], 'caption_duration_ms': caption['duration_ms'],
                            'stream': tracks.get(scene), 'scm_scene_calls': contexts,
                            'caption_cooccurrence_verified': False, 'production_approved': False})
            cut_keys.add(gid)
            named_keys[gid].add(key)
    classifications = []
    legacy_script_keys = {row.get('gxt_stable_id') for row in existing_records
                          if row.get('gxt_stable_id') and str(row.get('stable_audio_id', '')).startswith('SCRIPT:')
                          and row.get('bank_num') is not None and row.get('sound_num') is not None}
    # User-interface evidence is a separate category, never 'no audio by nature'.
    interface_commands = {'PRINT_HELP', 'PRINT_BIG', 'PRINT_WITH_NUMBER_BIG', 'PRINT_BIG_Q',
                          'SET_TEXT_DRAW_BEFORE_FADE', 'DISPLAY_TEXT', 'ADD_TEXT_LABEL',
                          'TEXT_DRAW', 'PRINT_HELP_FOREVER'}
    for table in gxt.order:
        for crc in gxt.t[table]['hashes']:
            gid = '%s:%08X' % (table, crc)
            evidence = uses.get(gid, [])
            category = ('script' if gid in script_keys or gid in legacy_script_keys else 'cutscene' if gid in cut_keys else
                        'interface_usage' if evidence and all(x['command'] in interface_commands for x in evidence)
                        else 'unknown')
            classifications.append({'gxt_stable_id': gid, 'table': table, 'hash': crc,
                                    'names': sorted(named_keys.get(gid, ())), 'category': category,
                                    'source_text': decode_text_c(gxt.t[table]['orig'][crc]), 'uses': evidence})
    old = {row.get('gxt_stable_id'): row for row in existing_records if row.get('gxt_stable_id')}
    conflicts = []
    for row in records:
        prior = old.get(row['gxt_stable_id'])
        if prior and row['pak'] == 'SCRIPT' and prior.get('stable_audio_id') != row['stable_audio_id']:
            conflicts.append({'gxt_stable_id': row['gxt_stable_id'], 'old': prior.get('stable_audio_id'), 'new': row['stable_audio_id']})
    for name, source in sources.items():
        if digest(source['path']) != source['sha256']:
            raise ValueError('Game source changed during indexing: ' + name)
    if progress:
        progress(4, 4, 'Índice dos arquivos do jogo concluído; fontes preservadas.')
    return {'version': 1, 'sources': sources, 'opcode_source': {k: v for k, v in definitions().items() if k != 'commands'},
            'records': records, 'classifications': classifications,
            'scm': {'segments': len(segments), 'complete_segments': len(segments) - len(incomplete),
                    'instructions': sum(len(s['instructions']) for s in segments), 'incomplete': incomplete,
                    'audio_calls': audio_calls, 'scene_calls': scene_calls,
                    'branch_targets_checked': branch_count, 'invalid_branch_targets': invalid_branches},
            'summary': {'classifications': dict(Counter(x['category'] for x in classifications)),
                        'native_script_links': len(script_keys), 'script_links': len(script_keys | legacy_script_keys), 'cutscene_links': len(cut_keys),
                        'cut_files': len(cuts), 'coordinate_conflicts': conflicts},
            'limits': ['Static file evidence; gameplay/audio content not verified.',
                       'Indirect audio IDs unresolved; no alphabetical position guessing.',
                       'Interface usage is not proof of absence of voice.']}


@lru_cache(maxsize=4)
def _load(path, size, mtime):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_current(root, game):
    path = Path(root) / 'workspace/native_audio/index.json'
    if not path.is_file() or not game:
        return None
    stat = path.stat()
    report = _load(str(path), stat.st_size, stat.st_mtime_ns)
    if report.get('version') != 1 or not report.get('sources') or not isinstance(report.get('records'), list):
        return None
    for source in report['sources'].values():
        candidate = Path(source['path'])
        # Report must refer to the selected game, not a previous installation.
        try:
            candidate.resolve().relative_to(Path(game).resolve())
            now = candidate.stat()
        except (ValueError, OSError):
            return None
        signature = source.get('signature')
        if signature != [now.st_size, now.st_mtime_ns]:
            return None
    return report


def merge_records(existing, report):
    """Supplement legacy coordinates; keep their character annotations and conflicts."""
    if not report:
        return list(existing)
    output = list(existing)
    positions = {row.get('gxt_stable_id'): index for index, row in enumerate(output) if row.get('gxt_stable_id')}
    groups = defaultdict(list)
    for row in report.get('records', []):
        groups[row['gxt_stable_id']].append(row)
    for gid, rows in groups.items():
        prior_index = positions.get(gid)
        prior = output[prior_index] if prior_index is not None else None
        scripts = [row for row in rows if row['pak'] == 'SCRIPT']
        if prior and prior.get('bank_num') is not None and prior.get('sound_num') is not None:
            # The old exact link remains authoritative; discrepancies stay in the
            # report instead of replacing production coordinates.
            row = dict(prior, native_evidence=rows)
        elif scripts:
            row = dict(scripts[0])
        else:
            options = {json.dumps([row['scene'], row['caption_start_ms'], row['caption_duration_ms']], sort_keys=True): row for row in rows}
            row = dict(next(iter(options.values())))
            row['cutscene_options'] = list(options.values())
            row['cutscene_ambiguous'] = len(options) != 1
            if prior:
                for name in ('speaker_identity', 'speaker_identity_status', 'speaker_kind'):
                    if name in prior:
                        row[name] = prior[name]
        if prior_index is None:
            positions[gid] = len(output)
            output.append(row)
        else:
            output[prior_index] = row
    return output
