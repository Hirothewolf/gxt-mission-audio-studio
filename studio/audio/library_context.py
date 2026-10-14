"""Read-only snapshots and audio roots for the per-project library."""
import json
from functools import lru_cache
from pathlib import Path

from studio.config import CFG
from studio.gxt.codec import decode_text_c
from studio.gxt.model import Gxt
from studio.script.mapping import PAKS


_NPC_SPCS = tuple(pak for pak in PAKS if pak.startswith('SPC_'))
SCOPE_PROFILES = {
    'script': ('SCRIPT',),
    'npc_spcs': _NPC_SPCS,
    'dialogue': ('SCRIPT',) + _NPC_SPCS,
    'all_sfx': tuple(PAKS),
}
SCOPE_PROFILE_LABELS = {
    'script': 'SCRIPT',
    'npc_spcs': 'NPC SPCs',
    'dialogue': 'Dialogue (SCRIPT + SPCs)',
    'all_sfx': 'Advanced: All SFX',
}


def gxt_entries(gxt):
    if not gxt:
        return []
    return [{'table': table, 'hash': key, 'text': decode_text_c(gxt.t[table]['cur'][key])}
            for table in gxt.order for key in gxt.t[table]['hashes']]


def base_gxt_path(app):
    game = getattr(app, 'jogo', None)
    path = game.info.get('gxt') if game else None
    if not path and CFG.get('game_dir'):
        path = Path(CFG['game_dir']) / 'text' / 'american.gxt'
    return Path(path).resolve() if path and Path(path).is_file() else None


@lru_cache(maxsize=3)
def _base_texts(path, size, mtime):
    gxt = Gxt(path)
    return {(e['table'], e['hash']): e['text'] for e in gxt_entries(gxt)}


def base_texts(app):
    path = base_gxt_path(app)
    if not path:
        return {}
    stat = path.stat()
    return _base_texts(str(path), stat.st_size, stat.st_mtime_ns)


def read_manifest(root):
    candidates = [Path(root) / directory / name for directory in ('manifests', '')
                  for name in ('script_mapping_v3.jsonl', 'script_mapping_v2.jsonl', 'script_mapping.jsonl')]
    source = next((p for p in candidates if p.is_file()), None)
    records = []
    if source:
        with source.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError('Registro inválido no catálogo: %s' % source)
                records.append(record)
    from studio.script.native_index import load_current, merge_records
    return merge_records(records, load_current(root, CFG.get('game_dir')))


def inventory_roots(app, project_root, paks=('SCRIPT',)):
    """Return only roots inside the requested archive scope.

    Custom roots are narrowed to exact selected-PAK directories. The all-SFX
    scope explicitly opts into scanning each complete custom root, including
    loose WAVs with no archive coordinates.
    """
    selected = tuple(dict.fromkeys(str(pak).upper() for pak in (paks or ())))
    unknown = set(selected) - set(PAKS)
    if unknown:
        raise ValueError('PAK desconhecido: %s' % ', '.join(sorted(unknown)))
    selected_set = set(selected)
    advanced_all = selected_set == set(PAKS)
    root, results = Path(project_root), []

    def add(path, source, label):
        path = Path(path).resolve()
        if path.is_dir():
            results.append({'path': path, 'source': source, 'label': label})

    def add_selected(base, source, label, *, allow_whole=False):
        base = Path(base)
        if not base.is_dir():
            return
        if allow_whole:
            add(base, source, label)
            return
        if base.name.upper() in selected_set:
            add(base, source, label)
            return
        try:
            children = sorted(base.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return
        for child in children:
            if child.is_dir() and child.name.upper() in selected_set:
                add(child, source, label)

    add_selected(root / 'extracted' / 'audio' / 'sfx', 'project_original', 'Áudio extraído no projeto')
    add_selected(root / 'extracted' / 'audio' / 'non_mission_spc', 'project_original', 'Vozes de ambiente do projeto')
    game = getattr(app, 'jogo', None)
    if game:
        for pak in selected:
            state = game.estado_cache(pak)
            if not state.get('desatualizado') and state.get('indexado'):
                add(game.cache_dir() / 'export' / pak, 'original_cache', 'Original extraído do jogo')
            if game._cache_mod_atual(game.mod_ativo(), pak):
                add(game.cache_dir_mod() / 'export' / pak, 'mod_cache', 'Extraído do mod ativo')
        if game.modloader_dir():
            mod = game.modloader_dir() / game.mod_ativo()
            # Case-insensitive lookup is needed for common audio/SFX layouts.
            from studio.core.utils import nome_insens
            audio = nome_insens(mod, 'audio') if mod.is_dir() else None
            sfx = nome_insens(audio, 'SFX') if audio else None
            if sfx:
                add_selected(sfx, 'mod', 'WAVs no ModLoader: %s' % game.mod_ativo())
    for path in CFG.get('audio_search_roots', []) or []:
        add_selected(path, 'custom', 'Pasta adicionada manualmente', allow_whole=advanced_all)
    # Repeated configuration entries are harmless. The inventory deduplicates
    # physical paths, keeping the most specific root's origin.
    unique = {(r['source'], str(r['path']).casefold()): r for r in results}
    return list(unique.values())
