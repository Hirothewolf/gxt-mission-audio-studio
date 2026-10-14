# -*- coding: utf-8 -*-
"""
Integração com o mapeamento SCRIPT (manifests/script_mapping_v2.jsonl).
"""
import re
import json
from pathlib import Path
import studio.config as config

PAKS = ('FEET', 'GENRL', 'PAIN_A', 'SCRIPT', 'SPC_EA', 'SPC_FA', 'SPC_GA', 'SPC_NA', 'SPC_PA')
_RE_BS = re.compile(r'(?i)bank[_\-\s]?(\d+)\D{1,6}?sound[_\-\s]?(\d+)')
_RE_BS2 = re.compile(r'(?i)(?<![a-z0-9])b0*(\d+)\D+s0*(\d+)(?![a-z0-9])')
_RE_PAK = re.compile(r'(?i)(?<![a-z0-9_])(%s)(?![a-z0-9])' % '|'.join(PAKS))
_CAMPOS_TEXTO = ('source_wav_path', 'source_wav', 'wav', 'stable_audio_id', 'audio_id', 'sound_path', 'path', 'source')

_SCRIPT_CACHE = None


def build_script_index(records):
    """Build a SCRIPT lookup from manifest records without reading files.

    Exact (table, GXT CRC) links are retained. An ``ANY`` CRC fallback is
    exposed only when that CRC identifies one manifest row; collisions are
    suppressed instead of depending on file order.
    """
    db = {}
    ambiguous_keys = set()

    def index_unique(key, record):
        if key in ambiguous_keys:
            return
        previous = db.get(key)
        if previous is None:
            db[key] = record
        elif previous != record:
            db.pop(key, None)
            ambiguous_keys.add(key)
            db[('_AMBIGUOUS', key)] = True

    valid_records = [record for record in records if isinstance(record, dict)]
    exact_keys = set()
    explicit_any = set()

    # Pass 1: establish authoritative GXT identities before considering any
    # weaker JAMCRC aliases. An alias must never poison an explicit key.
    for record in valid_records:
        gid = record.get('gxt_stable_id')
        if not gid or ':' not in gid:
            continue
        tab, hex_crc = gid.split(':', 1)
        try:
            crc = int(hex_crc, 16)
        except ValueError:
            continue
        key = (tab, crc)
        exact_keys.add(key)
        explicit_any.add(crc)
        index_unique(key, record)
        index_unique(('ANY', crc), record)

    # Pass 2: add JAMCRC aliases only when no explicit key occupies the same
    # table/hash. Keep alias collisions conservative and independent by key.
    alias_keys = {}
    alias_any = {}
    for record in valid_records:
        gid = record.get('gxt_stable_id')
        tab = gid.split(':', 1)[0] if gid and ':' in gid else None
        jamcrc = record.get('jamcrc')
        if not jamcrc:
            continue
        try:
            crc = int(str(jamcrc).strip(), 16)
        except ValueError:
            continue
        if tab:
            key = (tab, crc)
            if key not in exact_keys:
                alias_keys.setdefault(key, []).append(record)
        alias_any.setdefault(crc, []).append(record)

    def unique_records(items):
        unique = []
        for item in items:
            if not any(item == previous for previous in unique):
                unique.append(item)
        return unique

    for key, candidates in alias_keys.items():
        unique = unique_records(candidates)
        if len(unique) == 1:
            index_unique(key, unique[0])
        elif unique:
            ambiguous_keys.add(key)
            db[('_AMBIGUOUS', key)] = True

    for crc, candidates in alias_any.items():
        unique = unique_records(candidates)
        explicit = db.get(('ANY', crc))
        if crc in explicit_any:
            # Preserve explicit records, but don't expose global fallback if
            # an alias points at a different record for the same CRC.
            if explicit is not None and any(item != explicit for item in unique):
                db.pop(('ANY', crc), None)
                ambiguous_keys.add(('ANY', crc))
                db[('_AMBIGUOUS', ('ANY', crc))] = True
            continue
        if len(unique) == 1:
            index_unique(('ANY', crc), unique[0])
        elif unique:
            ambiguous_keys.add(('ANY', crc))
            db[('_AMBIGUOUS', ('ANY', crc))] = True

    # Event and audio-ID aliases are weaker than exact table/hash links too,
    # but retain the existing conservative unique-record rule.
    for record in valid_records:
        event = record.get('event_name')
        if event:
            index_unique(('EVENT', str(event).strip().upper()), record)
        audio_id = record.get('stable_audio_id')
        if audio_id:
            index_unique(('AID', str(audio_id).strip().upper()), record)

    return db


def carregar_script_cache():
    """Carrega e indexa em memória o manifest de diálogos do GTA (script_mapping_v2.jsonl)."""
    global _SCRIPT_CACHE
    if _SCRIPT_CACHE is not None:
        return _SCRIPT_CACHE

    candidatos = [
        config.get_project_root() / 'manifests' / 'script_mapping_v3.jsonl',
        config.get_project_root() / 'manifests' / 'script_mapping_v2.jsonl',
        config.get_project_root() / 'manifests' / 'script_mapping.jsonl',
        config.get_project_root() / 'script_mapping_v3.jsonl',
        config.get_project_root() / 'script_mapping_v2.jsonl',
        config.get_project_root() / 'script_mapping.jsonl',
    ]
    map_file = next((f for f in candidatos if f.is_file()), None)
    records = []
    if map_file:
        try:
            with open(map_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue

                    records.append(rec)
        except Exception as e:
            print(f"Aviso ao carregar script_mapping: {e}")
    from studio.script.native_index import load_current, merge_records
    records = merge_records(records, load_current(config.get_project_root(), config.CFG.get('game_dir')))
    _SCRIPT_CACHE = build_script_index(records)
    return _SCRIPT_CACHE


def recarregar_script_cache():
    """Invalida o cache em memória e recarrega o manifest."""
    global _SCRIPT_CACHE
    _SCRIPT_CACHE = None
    return carregar_script_cache()


def consultar_script_info(db, tab, h, ev_nome=None):
    """Busca as informações de áudio de uma fala por (tabela, CRC), ('ANY', CRC) ou nome do evento."""
    if not db:
        return None
    if tab and h is not None:
        if db.get(('_AMBIGUOUS', (tab, h))):
            return None
        rec = db.get((tab, h))
        if rec:
            return rec
    if h is not None:
        rec = db.get(('ANY', h))
        if rec and (not tab or _record_table(rec) in (None, tab)):
            return rec
    if ev_nome:
        ev_upper = str(ev_nome).strip().upper()
        rec = db.get(('EVENT', ev_upper))
        if rec and (not tab or _record_table(rec) in (None, tab)):
            return rec
    return None


def _record_table(record):
    """Return explicit GXT table provenance, if the record has it."""
    gid = record.get('gxt_stable_id') if isinstance(record, dict) else None
    if gid and ':' in gid:
        return gid.split(':', 1)[0].strip().upper()
    if isinstance(record, dict):
        table = record.get('table') or record.get('gxt_table')
        if table:
            return str(table).strip().upper()
    return None


def ref_do_registro(rec, pak_padrao='SCRIPT'):
    """Retorna (PAK, banco, som) ou None a partir de um registro de fala.
    Garante índices 1-indexados para casar com os arquivos WAV reais extraídos pelo SAAT
    (Bank_001..Bank_NNN, sound_001.wav..sound_NNN.wav) e com a estrutura do ModLoader.
    """
    if not isinstance(rec, dict):
        return None

    # Stream identities cannot acquire SCRIPT coordinates from stale bank/path
    # metadata. This guard also protects SAAT review/install/batch consumers.
    from studio.audio.source_routing import is_stream_record
    if is_stream_record(rec):
        return None

    pak = None
    for k in ('pak', 'archive', 'package', 'sfx_archive', 'container'):
        v = str(rec.get(k) or '').upper()
        if v in PAKS:
            pak = v
            break
    if not pak:
        for k in ('stable_audio_id', 'audio_id'):
            match = _RE_PAK.search(str(rec.get(k) or ''))
            if match:
                pak = match.group(1).upper()
                break

    # 1. Se tiver bank_num e sound_num explícitos (já 1-indexados, padrão SAAT):
    bn = rec.get('bank_num')
    sn = rec.get('sound_num')
    if bn is not None and sn is not None:
        try:
            return (pak or pak_padrao, int(bn), int(sn))
        except (ValueError, TypeError):
            pass

    # 2. Se tiver source_wav_path ou path contendo Bank_NNN e sound_NNN:
    for k in ('source_wav_path', 'arquivo_audio', 'wav', 'path', 'file', 'rel'):
        s = str(rec.get(k) or '')
        if not s:
            continue
        mb = re.search(r'Bank_(\d+)[\\/]sound_(\d+)', s, flags=re.I)
        if mb:
            mp = _RE_PAK.search(s)
            return (pak or (mp.group(1).upper() if mp else pak_padrao), int(mb.group(1)), int(mb.group(2)))

    # 3. Se tiver bank_index e sound_index (0-indexados do motor GTA), converte para 1-indexado (+1):
    bi = rec.get('bank_index')
    si = rec.get('sound_index')
    if bi is not None and si is not None:
        try:
            return (pak or pak_padrao, int(bi) + 1, int(si) + 1)
        except (ValueError, TypeError):
            pass

    def primeiro_int(chaves):
        for k in chaves:
            v = rec.get(k)
            if v is None or isinstance(v, bool):
                continue
            try:
                return int(str(v).strip(), 0 if str(v).strip().lower().startswith('0x') else 10)
            except ValueError:
                m = re.search(r'\d+', str(v))
                if m:
                    return int(m.group())
        return None

    banco = primeiro_int(('bank', 'bank_id', 'bank_number', 'banco'))
    som = primeiro_int(('sound', 'sound_id', 'sound_number', 'snd', 'som'))
    if banco is None or som is None:
        for k in _CAMPOS_TEXTO:
            s = str(rec.get(k) or '')
            if not s:
                continue
            m = _RE_BS.search(s) or _RE_BS2.search(s)
            if m:
                b_val, s_val = int(m.group(1)), int(m.group(2))
                # stable_audio_id/audio_id bank and sound coordinates are
                # zero-based in every archive, while SAAT paths are one-based.
                if k in ('stable_audio_id', 'audio_id'):
                    b_val += 1
                    s_val += 1
                banco, som = b_val, s_val
                if not pak:
                    mp = _RE_PAK.search(s)
                    pak = mp.group(1).upper() if mp else None
                break

    if banco is None or som is None:
        return None
    return (pak or pak_padrao, banco, som)
