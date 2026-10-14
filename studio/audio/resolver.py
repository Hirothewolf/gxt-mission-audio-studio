# -*- coding: utf-8 -*-
"""
Motor de resolução e localização de áudios originais e dublados (BaseAudioResolver).
Suporta busca por manifest, stable_audio_id, Bank_XXX/sound_XXX, chave/JAMCRC e sidecars.
"""
import os
import re
import csv
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime

import studio.config as config
from studio.config import CFG, AUDIO_INDEX_FILE, APP_DIR
from studio.core.utils import salvar_json_atomico, carregar_json_seguro
from studio.script.mapping import PAKS


def _norm_audio_token(valor):
    """Normaliza chave, evento, ID ou caminho para comparação tolerante."""
    if valor is None:
        return ''
    return re.sub(r'[^A-Z0-9]+', '_', str(valor).upper()).strip('_')


def _bank_sound_from_text(valor):
    """Extrai (banco, som) de strings como 'SCRIPT/Bank_117/sound_015.wav' ou 'SCRIPT:B000:S001'."""
    txt = str(valor or '')
    pats = [
        r'(?i)BANK[ _:/\\-]*0*(\d{1,4}).*?SOUND[ _:/\\-]*0*(\d{1,4})',
        r'(?i)\bB(?:ANK)?[_-]?0*(\d{1,4})\D+S(?:OUND)?[_-]?0*(\d{1,4})\b',
    ]
    for pat in pats:
        m = re.search(pat, txt)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _bank_sound_from_record(rec):
    """Extrai tupla (banco, som) 1-indexada para casar com os arquivos físicos em disco (Bank_NNN, sound_NNN.wav)."""
    if not isinstance(rec, dict):
        return None
    # 1. bank_num e sound_num explícitos (1-indexados)
    bn = rec.get('bank_num')
    sn = rec.get('sound_num')
    if bn is not None and sn is not None:
        try:
            return int(bn), int(sn)
        except (ValueError, TypeError):
            pass
    # 2. source_wav_path ou caminhos com Bank_NNN e sound_NNN
    for k in ('source_wav_path', 'arquivo_audio', 'wav', 'path', 'file', 'rel'):
        s = str(rec.get(k) or '')
        if not s:
            continue
        mb = re.search(r'Bank_(\d+)[\\/]sound_(\d+)', s, flags=re.I)
        if mb:
            return int(mb.group(1)), int(mb.group(2))
    # 3. bank_index e sound_index (0-indexados do motor GTA -> +1)
    bi = rec.get('bank_index')
    si = rec.get('sound_index')
    if bi is not None and si is not None:
        try:
            return int(bi) + 1, int(si) + 1
        except (ValueError, TypeError):
            pass
    # 4. stable_audio_id (ex: SCRIPT:B015:S000 -> 0-indexado -> +1)
    aid = str(rec.get('stable_audio_id') or rec.get('audio_id') or '')
    if aid:
        m = re.search(r'B(\d+)\D+S(\d+)', aid, flags=re.I)
        if m:
            return int(m.group(1)) + 1, int(m.group(2)) + 1
        bs = _bank_sound_from_text(aid)
        if bs:
            return bs
    return None


def _pak_from_record(rec):
    """Return an archive name only when the record or stable ID states it."""
    if not isinstance(rec, dict):
        return None
    for key in ('pak', 'archive', 'package', 'sfx_archive', 'container'):
        name = str(rec.get(key) or '').strip().upper()
        if name in PAKS:
            return name
    for key in ('stable_audio_id', 'audio_id', 'source_wav_path', 'path', 'file'):
        match = re.search(r'(?i)(?<![A-Z0-9_])(%s)(?![A-Z0-9])' % '|'.join(PAKS),
                          str(rec.get(key) or ''))
        if match:
            return match.group(1).upper()
    return None


def _pcm_hash_matches(path, expected):
    """Compare a manifest PCM hash against only the WAV data chunk bytes."""
    if not expected:
        return True
    from studio.audio.wav import wav_info
    info = wav_info(path)
    if not info:
        return False
    offset, length = info['dados']
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            remaining = length
            while remaining:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return False
    return digest.hexdigest().lower() == str(expected).strip().lower()


def resolver_path_projeto(valor):
    """Resolve caminhos de áudio do manifest de forma portável."""
    if not valor:
        return None
    p = Path(str(valor)).expanduser()
    try:
        if p.is_file():
            return p
    except OSError:
        pass

    # Tenta resolver relativo à raiz atual do projeto
    project_root = config.get_project_root()
    candidatos = [project_root / p]
    partes = [x for x in re.split(r'[\\/]+', str(valor)) if x and not x.endswith(':')]
    for n in (6, 5, 4, 3, 2):
        if len(partes) >= n:
            candidatos.append(project_root.joinpath(*partes[-n:]))
    for c in candidatos:
        try:
            if c.is_file():
                return c
        except OSError:
            pass
    return None


def _parece_gta_root(path):
    """Verifica se o diretório aparenta ser a instalação do GTA San Andreas."""
    try:
        p = Path(path)
        return (p / 'audio' / 'SFX' / 'SCRIPT').is_file() and (p / 'audio' / 'CONFIG' / 'BankLkup.dat').is_file()
    except Exception:
        return False


def detectar_gta_sa_instalacoes(extra=()):
    """Procura instalações do GTA SA em locais comuns, ModLoader e bibliotecas Steam."""
    candidatos = [Path(x) for x in (extra or []) if x]
    if CFG.get('game_dir'):
        candidatos.append(Path(CFG['game_dir']))
    for env in ('ProgramFiles', 'ProgramFiles(x86)'):
        base = os.environ.get(env)
        if not base:
            continue
        b = Path(base)
        candidatos += [
            b / 'Rockstar Games' / 'GTA San Andreas',
            b / 'Rockstar Games' / 'Grand Theft Auto San Andreas',
            b / 'Steam' / 'steamapps' / 'common' / 'Grand Theft Auto San Andreas',
            b / 'Steam' / 'steamapps' / 'common' / 'GTA San Andreas',
        ]
        vdf = b / 'Steam' / 'steamapps' / 'libraryfolders.vdf'
        if vdf.is_file():
            try:
                txt = vdf.read_text(encoding='utf-8', errors='replace')
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    lib = Path(m.group(1).replace('\\\\', '\\'))
                    candidatos += [
                        lib / 'steamapps' / 'common' / 'Grand Theft Auto San Andreas',
                        lib / 'steamapps' / 'common' / 'GTA San Andreas',
                    ]
            except Exception:
                pass
    out, seen = [], set()
    for c in candidatos:
        try:
            c = c.expanduser().resolve()
        except Exception:
            c = c.expanduser()
        k = os.path.normcase(str(c))
        if k not in seen and _parece_gta_root(c):
            seen.add(k)
            out.append(c)
    return out


def detectar_saat_sfx(configurado=None, gta_root=None):
    """Localiza o executável saat_sfx.exe nas ferramentas do projeto ou no jogo."""
    candidatos = []
    if configurado:
        candidatos.append(Path(configurado).expanduser())
    if CFG.get('saat_sfx'):
        candidatos.append(Path(CFG['saat_sfx']).expanduser())
    if gta_root:
        g = Path(gta_root)
        candidatos += [g / 'saat_sfx.exe', g / 'saat_sfx', g / 'saat' / 'saat_sfx.exe']
    candidatos += [
        APP_DIR / 'tools' / 'SAAT' / 'saat_sfx.exe',
        config.get_project_root() / 'tools' / 'SAAT' / 'saat_sfx.exe',
        config.get_project_root() / 'saat_sfx.exe',
        APP_DIR / 'saat_sfx.exe',
    ]
    for p in candidatos:
        try:
            if p.is_file():
                return p
        except OSError:
            pass
    return None


class BaseAudioResolver:
    """Índice e motor de resolução para arquivos WAV originais e sidecars."""

    CACHE_SCHEMA = 2

    def __init__(self):
        self.roots = []
        self.records = []
        self.by_bank_sound = {}
        self.by_bank_sound_any = {}
        self.by_alias = {}
        self.by_stem = {}
        self.audio_events = {}
        self.last_scan = 0.0
        self.scanning = False

    @staticmethod
    def normalize_roots(roots):
        out, seen = [], set()
        for valor in roots or []:
            if not valor:
                continue
            try:
                p = Path(valor).expanduser().resolve()
            except Exception:
                p = Path(valor).expanduser()
            key = os.path.normcase(str(p))
            if key in seen or not p.is_dir():
                continue
            seen.add(key)
            out.append(p)
        return out

    def clear(self):
        self.records = []
        self.by_bank_sound = {}
        self.by_bank_sound_any = {}
        self.by_alias = {}
        self.by_stem = {}
        self.audio_events = {}
        self.last_scan = 0.0

    def _add_alias(self, alias, path):
        a = _norm_audio_token(alias)
        if not a or len(a) < 3:
            return
        self.by_alias.setdefault(a, [])
        if path not in self.by_alias[a]:
            self.by_alias[a].append(path)

    def _add_record(self, wav, root):
        wav = Path(wav)
        try:
            rel = wav.relative_to(root)
        except Exception:
            rel = wav.name
        rels = str(rel).replace('\\', '/')
        bs = _bank_sound_from_text(rels)
        pak = root.name.upper() if root.name.upper() in PAKS else _pak_from_record({'source_wav_path': rels})
        stem = wav.stem
        stem_norm = _norm_audio_token(stem)
        rec = {
            'path': wav,
            'root': root,
            'rel': rels,
            'stem_norm': stem_norm,
            'path_norm': _norm_audio_token(rels),
            'bank_sound': bs,
            'pak': pak,
        }
        self.records.append(rec)
        if bs:
            if pak:
                self.by_bank_sound.setdefault((pak, *bs), []).append(wav)
            self.by_bank_sound_any.setdefault(bs, []).append(wav)
        self.by_stem[stem_norm] = wav

        self._add_alias(stem, wav)
        stem_sem_num = re.sub(r'^\d+[ _.-]+', '', stem)
        if stem_sem_num != stem:
            self._add_alias(stem_sem_num, wav)
            self.by_stem[_norm_audio_token(stem_sem_num)] = wav
        stem_limpo = re.sub(r'_(?:orig|dub|test|final)$', '', stem_sem_num, flags=re.I)
        if stem_limpo != stem_sem_num:
            self._add_alias(stem_limpo, wav)
            self.by_stem[_norm_audio_token(stem_limpo)] = wav

    def _sidecar_rows(self, path):
        try:
            suf = path.suffix.lower()
            if suf == '.csv':
                with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
                    yield from csv.DictReader(f, delimiter=';')
                return
            if suf == '.json':
                obj = json.loads(path.read_text(encoding='utf-8', errors='replace'))
                if isinstance(obj, list):
                    for x in obj:
                        if isinstance(x, dict):
                            yield x
                elif isinstance(obj, dict):
                    rows = obj.get('rows') or obj.get('items') or obj.get('dialogos')
                    if isinstance(rows, list):
                        for x in rows:
                            if isinstance(x, dict):
                                yield x
                return
        except Exception:
            return

    def _index_sidecar(self, sidecar):
        base = sidecar.parent
        for row in self._sidecar_rows(sidecar):
            arquivo = (row.get('source_wav_path') or row.get('arquivo_audio') or
                       row.get('wav') or row.get('path') or row.get('file'))
            if not arquivo:
                continue
            p = Path(str(arquivo)).expanduser()
            if not p.is_absolute():
                p1 = base / p
                p2 = config.get_project_root() / p
                p = p1 if p1.is_file() else p2
            if not p.is_file() or p.suffix.lower() != '.wav':
                continue
            aliases = [
                row.get('stable_audio_id'), row.get('audio_id'), row.get('event_name'),
                row.get('evento'), row.get('gxt_key'), row.get('key'), row.get('chave'),
                row.get('jamcrc'), row.get('gxt_stable_id')
            ]
            for a in aliases:
                self._add_alias(a, p)
            bs = _bank_sound_from_record(row)
            if bs:
                pak = _pak_from_record(row)
                if pak:
                    key = (pak, *bs)
                    self.by_bank_sound.setdefault(key, [])
                    if p not in self.by_bank_sound[key]:
                        self.by_bank_sound[key].append(p)
                self.by_bank_sound_any.setdefault(bs, [])
                if p not in self.by_bank_sound_any[bs]:
                    self.by_bank_sound_any[bs].append(p)

    def _load_audio_events_file(self, path):
        try:
            text = Path(path).read_text(encoding='latin-1', errors='replace')
        except Exception:
            return
        for m in re.finditer(r'(?mi)^\s*SOUND_([A-Z0-9_]+)\s+(-?\d+)\b', text):
            self.audio_events[m.group(1).upper()] = int(m.group(2))

    def scan(self, roots, extra_event_files=()):
        self.scanning = True
        try:
            self.clear()
            self.roots = self.normalize_roots(roots)
            sidecar_names = {'dialogos.json', 'dialogos.csv', 'audio_mapping.json'}
            sidecars = []
            for root in self.roots:
                try:
                    for wav in root.rglob('*.wav'):
                        if wav.is_file():
                            self._add_record(wav, root)
                except (OSError, PermissionError):
                    pass
                try:
                    for sp in root.glob('*.json'):
                        if sp.name.lower() in sidecar_names:
                            sidecars.append(sp)
                    for sp in root.glob('*.csv'):
                        if sp.name.lower() in sidecar_names:
                            sidecars.append(sp)
                    for sub in root.iterdir():
                        if sub.is_dir():
                            for sp in sub.glob('*.json'):
                                if sp.name.lower() in sidecar_names:
                                    sidecars.append(sp)
                            for sp in sub.glob('*.csv'):
                                if sp.name.lower() in sidecar_names:
                                    sidecars.append(sp)
                except (OSError, PermissionError):
                    pass
            for sp in sidecars:
                self._index_sidecar(sp)
            for ep in extra_event_files or ():
                if ep and Path(ep).is_file():
                    self._load_audio_events_file(ep)
            self.last_scan = time.time()
            self._save_cache_best_effort()
        finally:
            self.scanning = False

    def _save_cache_best_effort(self):
        try:
            payload = {
                'schema': self.CACHE_SCHEMA,
                'created_at': datetime.now().isoformat(timespec='seconds'),
                'roots': [str(x) for x in self.roots],
                'records': [
                    {'path': str(r['path']), 'root': str(r['root']), 'rel': r['rel']}
                    for r in self.records
                ],
                'audio_events': self.audio_events,
            }
            salvar_json_atomico(AUDIO_INDEX_FILE, payload)
        except Exception:
            pass

    def load_cache(self, roots, extra_event_files=()):
        roots = self.normalize_roots(roots)
        obj = carregar_json_seguro(AUDIO_INDEX_FILE, {})
        if obj.get('schema') != self.CACHE_SCHEMA:
            return False
        cached_roots = [os.path.normcase(str(Path(x))) for x in obj.get('roots', [])]
        wanted_roots = [os.path.normcase(str(x)) for x in roots]
        if cached_roots != wanted_roots:
            return False
        self.clear()
        self.roots = roots
        root_map = {os.path.normcase(str(x)): x for x in roots}
        valid = 0
        for x in obj.get('records', []):
            wav = Path(x.get('path', ''))
            r = root_map.get(os.path.normcase(str(Path(x.get('root', '')))))
            if r is None:
                continue
            self._add_record(wav, r)
            valid += 1
        self.audio_events.update({str(k).upper(): int(v) for k, v in obj.get('audio_events', {}).items()})
        for ep in extra_event_files or ():
            if ep and Path(ep).is_file():
                self._load_audio_events_file(ep)
        self.last_scan = time.time()
        return valid > 0

    def event_id(self, key):
        return self.audio_events.get(str(key or '').upper())

    def resolve(self, tab, h, key=None, ainfo=None):
        """
        Resolve o caminho do arquivo WAV original com base nas melhores evidências disponíveis.
        Retorna (Path, resolved_by, confianca_percentual).
        """
        ainfo = ainfo or {}
        expected_pcm = ainfo.get('pcm_sha256') or ainfo.get('source_pcm_sha256')
        # 1. Caminho oficial do manifest. A identidade/hash conflitando é
        # evidência insuficiente: não tente substituí-lo silenciosamente.
        src = resolver_path_projeto(ainfo.get('source_wav_path'))
        if src and src.is_file():
            if _pcm_hash_matches(src, expected_pcm):
                return src, 'manifest', 100
            if expected_pcm:
                return None, '', 0

        stable = str(ainfo.get('stable_audio_id') or '')
        event = str(ainfo.get('event_name') or '')

        # 2. Bank/Sound extraído de stable_audio_id ou metadados
        bs = _bank_sound_from_record(ainfo)
        if bs:
            pak = _pak_from_record(ainfo)
            candidates = (self.by_bank_sound.get((pak, *bs), []) if pak
                          else self.by_bank_sound_any.get(bs, []))
            hits = list(dict.fromkeys(
                p for p in candidates if p.is_file() and _pcm_hash_matches(p, expected_pcm)
            ))
            if len(hits) == 1:
                return hits[0], 'stable_audio_id → Bank/Sound', 99
            if hits:
                return None, '', 0
            if candidates and expected_pcm:
                return None, '', 0

        aliases = []
        crc_str = '%08X' % int(h) if h is not None else ''
        for v in (stable, key, event, crc_str, ('0x' + crc_str if crc_str else ''), f'{tab}:{crc_str}' if tab and crc_str else ''):
            n = _norm_audio_token(v)
            if n and n not in aliases:
                aliases.append(n)

        # 3. Aliases diretos e stem map (O(1))
        for a in aliases:
            candidates = self.by_alias.get(a, [])
            if candidates:
                hits = [p for p in candidates if p.is_file()]
                if len(hits) == 1:
                    if _pcm_hash_matches(hits[0], expected_pcm):
                        return hits[0], f'alias {a}', 95
                    if expected_pcm:
                        return None, '', 0
                if hits:
                    verified = [p for p in hits if _pcm_hash_matches(p, expected_pcm)]
                    if len(verified) == 1:
                        return verified[0], f'alias {a}', 95
                    return None, '', 0
            hit_stem = self.by_stem.get(a)
            if hit_stem and hit_stem.is_file():
                if _pcm_hash_matches(hit_stem, expected_pcm):
                    return hit_stem, f'stem {a}', 94
                if expected_pcm:
                    return None, '', 0

        # 4. Busca tolerante apenas em conjuntos reduzidos (não varre desnecessariamente)
        valid_aliases = [a for a in aliases if a and len(a) >= 4]
        if valid_aliases and len(self.records) <= 8000 and (ainfo or tab):
            scored = []
            tabn = _norm_audio_token(tab)
            for rec in self.records:
                score = 0
                stem = rec['stem_norm']
                pathn = rec['path_norm']
                for a in valid_aliases:
                    if stem == a:
                        score = max(score, 94)
                    elif stem.endswith('_' + a) or stem.startswith(a + '_'):
                        score = max(score, 91)
                    elif len(a) >= 5 and a in stem:
                        score = max(score, 87)
                    elif len(a) >= 6 and a in pathn:
                        score = max(score, 82)
                if tabn and tabn in pathn and score:
                    score += 3
                if score >= 82:
                    scored.append((score, rec['path']))
            if scored:
                scored.sort(key=lambda x: (-x[0], len(str(x[1]))))
                top = scored[0]
                if len(scored) == 1 or top[0] >= scored[1][0] + 3:
                    if top[1].is_file() and _pcm_hash_matches(top[1], expected_pcm):
                        return top[1], 'AutoSearch por chave/evento', top[0]

        return None, '', 0
