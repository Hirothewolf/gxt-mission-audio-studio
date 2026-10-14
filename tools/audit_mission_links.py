#!/usr/bin/env python3
"""Read-only audit of GXT-to-SCRIPT preview links.

The only files this tool writes are the JSON and Markdown reports named by
``--output-dir``. It never extracts, installs, or edits game/mod assets.
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from studio.config import APP_DIR, CFG, PROJ_ROOT
from studio.gxt.codec import decode_text_c
from studio.gxt.model import Gxt
from studio.script.mapping import build_script_index, consultar_script_info, ref_do_registro


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path):
    rows = []
    if not path.is_file():
        return rows
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _resolve_project_path(project_root, value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_file():
        return path
    candidate = project_root / path
    return candidate if candidate.is_file() else None


def _pcm_sha256(path):
    """Hash only the WAV data chunk, matching the SCRIPT manifest contract."""
    raw = path.read_bytes()
    if len(raw) < 12 or raw[:4] not in (b'RIFF', b'RF64') or raw[8:12] != b'WAVE':
        return None
    pos = 12
    while pos + 8 <= len(raw):
        name = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], 'little')
        start = pos + 8
        end = start + size
        if end > len(raw):
            return None
        if name == b'data':
            return hashlib.sha256(raw[start:end]).hexdigest()
        pos = end + (size & 1)
    return None


def _cache_status(project_root, game_root, mod_name):
    if not game_root or not mod_name:
        return {'current': False, 'indexed_wavs': 0, 'reason': 'game root/mod is not configured'}
    game_root = Path(game_root)
    mod_dir = game_root / 'modloader' / mod_name
    archive = mod_dir / 'audio' / 'SFX' / 'SCRIPT'
    mod_bank = mod_dir / 'audio' / 'CONFIG' / 'BankLkup.dat'
    bank = mod_bank if mod_bank.is_file() else game_root / 'audio' / 'CONFIG' / 'BankLkup.dat'
    slug = hashlib.sha1(mod_name.lower().encode('utf-8')).hexdigest()[:10]
    cache_dir = project_root / '.cache' / 'saat_mod' / slug
    meta_path = cache_dir / 'cache_meta_SCRIPT.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        signature = meta.get('fonte', {})
        archive_stat, bank_stat = archive.stat(), bank.stat()
        current = (
            meta.get('mod') == mod_name
            and str(signature.get('mod')) == mod_name
            and str(signature.get('pak', '')).upper() == 'SCRIPT'
            and signature.get('SCRIPT') == [str(archive.resolve()), archive_stat.st_size, archive_stat.st_mtime_ns]
            and signature.get('BankLkup.dat') == [str(bank.resolve()), bank_stat.st_size, bank_stat.st_mtime_ns]
        )
    except (OSError, ValueError, TypeError):
        current = False
    export = cache_dir / 'export' / 'SCRIPT'
    wavs = sum(1 for _ in export.rglob('*.wav')) if current and export.is_dir() else 0
    return {
        'current': current,
        'indexed_wavs': wavs,
        'archive': str(archive),
        'cache': str(export),
        'reason': 'source signature matches' if current else 'cache missing or source signature differs',
    }


def build_report(args):
    project_root = Path(args.project_root).resolve()
    mod_gxt_path = Path(args.mod_gxt).resolve()
    base_gxt_path = Path(args.base_gxt).resolve() if args.base_gxt else None
    mapping_path = next((path for path in (
        project_root / 'manifests' / 'script_mapping_v3.jsonl',
        project_root / 'manifests' / 'script_mapping_v2.jsonl',
        project_root / 'manifests' / 'script_mapping.jsonl',
    ) if path.is_file()), None)
    if not mapping_path:
        raise FileNotFoundError(f'No SCRIPT mapping manifest found under {project_root}')

    mod_gxt = Gxt(str(mod_gxt_path))
    base_gxt = Gxt(str(base_gxt_path)) if base_gxt_path and base_gxt_path.is_file() else None
    if args.table not in mod_gxt.t:
        raise KeyError(f'Table {args.table!r} not found in {mod_gxt_path}')

    records = _read_jsonl(mapping_path)
    script_index = build_script_index(records)

    cutscene_path = project_root / 'manifests' / 'cutscene_gxt_literal_candidates.jsonl'
    cutscene = {}
    for candidate in _read_jsonl(cutscene_path):
        for entry in candidate.get('candidate_gxt_entries', []):
            gxt_id = entry.get('gxt_id')
            if gxt_id:
                cutscene.setdefault(gxt_id, []).append({
                    'track': candidate.get('track'),
                    'candidate_id': candidate.get('candidate_id'),
                    'status': candidate.get('status'),
                    'production_approved': bool(candidate.get('production_approved')),
                    'candidate_source_text': entry.get('source_text'),
                })

    cache = _cache_status(project_root, args.game_root, args.mod_name)
    mod_export = Path(cache['cache']) if cache.get('cache') else None
    entries = []
    for crc in mod_gxt.t[args.table]['hashes']:
        gxt_id = f'{args.table}:{crc:08X}'
        record = consultar_script_info(script_index, args.table, crc)
        mapping_kind = (
            'exact' if (args.table, crc) in script_index
            else 'unique_crc_fallback' if record else 'no confirmed link'
        )
        original_path = _resolve_project_path(project_root, record.get('source_wav_path')) if record else None
        expected_pcm = (record.get('pcm_sha256') or record.get('source_pcm_sha256')) if record else None
        original_ok = bool(original_path and (
            not expected_pcm or _pcm_sha256(original_path) == str(expected_pcm).lower()
        ))
        ref = ref_do_registro(record) if record else None
        mod_path = None
        if ref and cache['current'] and mod_export:
            pak, bank, sound = ref
            mod_path = mod_export / f'Bank_{bank:03d}' / f'sound_{sound:03d}.wav'
            if pak != 'SCRIPT':
                mod_path = mod_export.parent / pak / f'Bank_{bank:03d}' / f'sound_{sound:03d}.wav'
        mod_ok = bool(mod_path and mod_path.is_file())

        english = ''
        if base_gxt and args.table in base_gxt.t and crc in base_gxt.t[args.table]['cur']:
            english = decode_text_c(base_gxt.t[args.table]['cur'][crc])
        current = decode_text_c(mod_gxt.t[args.table]['cur'][crc])
        match = re.match(r'(~[^~]+~)', english)
        tag = match.group(1) if match else ''
        role_hint = {
            '~z~': 'dialogue-subtitle tag; does not prove SCRIPT placement',
            '~s~': 'mission-objective/instruction tag; does not prove audio absence',
            '~r~': 'mission-status/warning tag; does not prove audio absence',
        }.get(tag, 'no recognized subtitle/objective tag')
        candidates = cutscene.get(gxt_id, [])
        entries.append({
            'gxt_id': gxt_id,
            'crc': f'{crc:08X}',
            'text_tag': tag or None,
            'classification_hint': role_hint,
            'english_base_text': english,
            'mod_text': current,
            'script_mapping': mapping_kind,
            'stable_audio_id': record.get('stable_audio_id') if record else None,
            'event_name': record.get('event_name') if record else None,
            'audio_coordinates': list(ref) if ref else None,
            'original_preview_available': original_ok,
            'original_path': str(original_path) if original_path else None,
            'mod_preview_available': mod_ok,
            'mod_path': str(mod_path) if mod_path else None,
            'cutscene_candidates_not_promoted': candidates,
        })

    missing = [entry for entry in entries if entry['script_mapping'] == 'no confirmed link']
    tag_counts = Counter(entry['text_tag'] or 'untagged' for entry in missing)
    exact_entries = [entry for entry in entries if entry['script_mapping'] != 'no confirmed link']
    exact_manifest_entries = [entry for entry in entries if entry['script_mapping'] == 'exact']
    cache_source_gxt = sorted({r.get('source_gxt_sha256') for r in records if r.get('source_gxt_sha256')})
    provenance_candidates = [
        project_root / 'source' / 'original_mod' / 'text' / 'american.gxt',
        project_root / 'deliverables' / 'american_gxt_base_game_20261005' / 'american.gxt',
        base_gxt_path,
        mod_gxt_path,
    ]
    source_gxt_hash_matches = {}
    for source_hash in cache_source_gxt:
        source_gxt_hash_matches[source_hash] = [
            str(path) for path in provenance_candidates
            if path and path.is_file() and _sha256(path).lower() == source_hash.lower()
        ]
    report = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'project_root': str(project_root),
        'table': args.table,
        'mod_gxt': str(mod_gxt_path),
        'mod_gxt_sha256': _sha256(mod_gxt_path),
        'base_gxt': str(base_gxt_path) if base_gxt_path else None,
        'base_gxt_sha256': _sha256(base_gxt_path) if base_gxt_path and base_gxt_path.is_file() else None,
        'mapping_manifest': str(mapping_path),
        'mapping_source_gxt_sha256_values': cache_source_gxt,
        'mapping_source_gxt_hash_matches': source_gxt_hash_matches,
        'mod_script_cache': cache,
        'coverage': {
            'gxt_entries': len(entries),
            'exact_script_links': len(exact_manifest_entries),
            'confirmed_script_links': len(exact_entries),
            'unlinked_entries': len(missing),
            'original_preview_available': sum(e['original_preview_available'] for e in exact_entries),
            'mod_preview_available': sum(e['mod_preview_available'] for e in exact_entries),
            'candidate_cutscene_links_not_promoted': sum(bool(e['cutscene_candidates_not_promoted']) for e in missing),
            'unlinked_tag_counts': dict(tag_counts),
        },
        'before_change_observation': {
            'exact_script_links': args.before_mapped,
            'original_preview_available': args.before_original,
            'mod_preview_available': args.before_mod,
            'unlinked_entries': args.before_unlinked,
        } if args.before_mapped is not None else None,
        'entries': entries,
        'interpretation': [
            'A missing SCRIPT link is left unresolved; no audio identity is inferred from text.',
            'Cutscene catalog records are candidate-only and are not promoted to audio links.',
            'Tag-based classifications are hints about GXT presentation, not proof of audio routing.',
        ],
    }
    return report


def markdown_report(report):
    coverage = report['coverage']
    lines = [
        f"# Mission GXT link audit: {report['table']}",
        '',
        f"Generated: {report['generated_at']}",
        f"Mod GXT SHA-256: `{report['mod_gxt_sha256']}`",
        f"Base GXT SHA-256: `{report['base_gxt_sha256'] or 'not supplied'}`",
        f"Mapping-manifest source GXT SHA-256 value(s): {', '.join('`'+x+'`' for x in report['mapping_source_gxt_sha256_values']) or 'none'}",
        f"Matching local source GXT path(s): {', '.join('`'+path+'`' for paths in report['mapping_source_gxt_hash_matches'].values() for path in paths) or 'none'}",
        '',
        f"Coverage: **{coverage['exact_script_links']}/{coverage['gxt_entries']}** exact SCRIPT links; ",
        f"{coverage['original_preview_available']} original and {coverage['mod_preview_available']} active-mod previews available; ",
        f"{coverage['unlinked_entries']} entries have no confirmed SCRIPT link.",
        f"Active-mod SCRIPT cache: current={report['mod_script_cache']['current']}, ",
        f"indexed WAVs={report['mod_script_cache']['indexed_wavs']}.",
        '',
        'Unlinked-entry tag counts (hints only):',
        '',
        '| GXT display tag | Count |',
        '|---|---:|',
    ]
    for tag, count in sorted(coverage['unlinked_tag_counts'].items()):
        lines.append(f'| `{tag}` | {count} |')
    lines += ['', 'Entries without a confirmed SCRIPT link:', '', '| GXT key | Tag | English base text | Cutscene candidate |', '|---|---|---|---|']
    for entry in report['entries']:
        if entry['script_mapping'] != 'no confirmed link':
            continue
        candidate = ', '.join(x.get('track') or '' for x in entry['cutscene_candidates_not_promoted']) or 'none'
        text = entry['english_base_text'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| `{entry['gxt_id']}` | `{entry['text_tag'] or 'untagged'}` | {text} | {candidate} |")
    before = report.get('before_change_observation')
    if before:
        lines += [
            '',
            'Before/after preview coverage:',
            '',
            f"- Before mapping/UI changes: {before['exact_script_links']} exact links, {before['original_preview_available']} original previews, {before['mod_preview_available']} active-mod previews, {before['unlinked_entries']} unlinked.",
            f"- After changes: {coverage['exact_script_links']} exact links, {coverage['original_preview_available']} original previews, {coverage['mod_preview_available']} active-mod previews, {coverage['unlinked_entries']} unlinked.",
            '',
            'The code changes protect against ambiguous cross-table CRC fallback and make unresolved SCRIPT links visible. They do not add guessed audio links.',
        ]
    lines += ['', 'The cutscene catalog remains candidate-only. GXT tag counts and text similarity are not audio-link evidence.']
    return '\n'.join(lines) + '\n'


def main():
    session = CFG.get('sessao', {}) if isinstance(CFG.get('sessao'), dict) else {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', default=str(PROJ_ROOT))
    parser.add_argument('--mod-gxt', default=session.get('gxt'))
    parser.add_argument('--base-gxt', default=CFG.get('game_dir') and str(Path(CFG['game_dir']) / 'text' / 'american.gxt'))
    parser.add_argument('--game-root', default=CFG.get('game_dir'))
    parser.add_argument('--mod-name', default=CFG.get('modloader_mod'))
    parser.add_argument('--table', default='CASIN10')
    parser.add_argument('--output-dir', default=str(APP_DIR / 'reports' / 'mission_link_audit'))
    parser.add_argument('--before-mapped', type=int)
    parser.add_argument('--before-original', type=int)
    parser.add_argument('--before-mod', type=int)
    parser.add_argument('--before-unlinked', type=int)
    args = parser.parse_args()
    if not args.mod_gxt:
        parser.error('Provide --mod-gxt or configure the editor session GXT.')
    report = build_report(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f'mission_link_audit_{args.table.lower()}'
    json_path = output_dir / f'{stem}.json'
    md_path = output_dir / f'{stem}.md'
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    md_path.write_text(markdown_report(report), encoding='utf-8')
    print(json.dumps({'json': str(json_path), 'markdown': str(md_path), 'coverage': report['coverage']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
