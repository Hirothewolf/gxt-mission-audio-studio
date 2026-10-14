# -*- coding: utf-8 -*-
"""
Detecção da instalação do GTA San Andreas, ferramentas SAAT e varredura de pastas de áudio.
"""
import os
import re
import string
from pathlib import Path
from studio.core.utils import nome_insens

PAKS = ('FEET', 'GENRL', 'PAIN_A', 'SCRIPT', 'SPC_EA', 'SPC_FA', 'SPC_GA', 'SPC_NA', 'SPC_PA')
_RE_BANCO = re.compile(r'(?i)^bank[_\-]?(\d+)$')
_RE_SOM = re.compile(r'(?i)^sound[_\-]?(\d+)\.wav$')


def validar_pasta_jogo(p):
    """Confere a estrutura mínima do GTA SA (audio/SFX/SCRIPT e audio/CONFIG/BankLkup.dat)."""
    r = {'pasta': None, 'ok': False, 'exe': None, 'script': None, 'banklkup': None, 'gxt': None,
         'modloader': None, 'faltando': []}
    if not p:
        r['faltando'].append('pasta não informada')
        return r
    p = Path(p)
    r['pasta'] = p
    if not p.is_dir():
        r['faltando'].append('a pasta não existe')
        return r
    audio = nome_insens(p, 'audio')
    sfx = nome_insens(audio, 'SFX') if audio else None
    cfg = nome_insens(audio, 'CONFIG') if audio else None
    r['script'] = nome_insens(sfx, 'SCRIPT') if sfx else None
    r['banklkup'] = nome_insens(cfg, 'BankLkup.dat') if cfg else None
    if not r['script']:
        r['faltando'].append('audio\\SFX\\SCRIPT')
    if not r['banklkup']:
        r['faltando'].append('audio\\CONFIG\\BankLkup.dat')
    try:
        for c in p.iterdir():
            n = c.name.lower()
            if n.endswith('.exe') and n.startswith('gta') and 'sa' in n:
                r['exe'] = c
                break
    except OSError:
        pass
    texto = nome_insens(p, 'text')
    r['gxt'] = nome_insens(texto, 'american.gxt') if texto else None
    ml = nome_insens(p, 'modloader')
    r['modloader'] = ml if ml and ml.is_dir() else None
    r['ok'] = not r['faltando']
    return r


def detectar_pastas_jogo(extras=()):
    """Detecta diretórios prováveis do GTA San Andreas no sistema."""
    cands = [Path(x) for x in extras if x]
    if os.environ.get('GTA_SA_DIR'):
        cands.append(Path(os.environ['GTA_SA_DIR']))
    sufixos = [
        'Rockstar Games/GTA San Andreas', 'Rockstar Games/Grand Theft Auto San Andreas', 'Games/GTA San Andreas',
        'GTA San Andreas', 'Program Files (x86)/Rockstar Games/GTA San Andreas',
        'Program Files/Rockstar Games/GTA San Andreas', 'Program Files (x86)/Steam/steamapps/common/Grand Theft Auto San Andreas'
    ]
    if os.name == 'nt':
        for d in string.ascii_uppercase:
            raiz = Path('%s:\\' % d)
            try:
                if not raiz.exists():
                    continue
            except OSError:
                continue
            cands += [raiz / s for s in sufixos]
    vistos, ok = set(), []
    for c in cands:
        k = str(c).lower()
        if k in vistos:
            continue
        vistos.add(k)
        if validar_pasta_jogo(c)['ok']:
            ok.append(c)
    return ok


def achar_saat(game_dir=None, extras=()):
    """Procura saat_sfx e saat_stream na config, pasta do jogo, ./SAAT, ./tools/SAAT."""
    dirs = [Path(x) for x in extras if x]
    here = Path(__file__).resolve().parent.parent.parent
    if game_dir:
        g = Path(game_dir)
        dirs += [g, g / 'SAAT', g / 'saat', g / 'tools', g / 'tools' / 'SAAT']
    dirs += [here, here / 'SAAT', here / 'tools', here / 'tools' / 'SAAT', here.parent / 'SAAT']
    res = {'sfx': None, 'stream': None}
    for d in dirs:
        for chave, base in (('sfx', 'saat_sfx'), ('stream', 'saat_stream')):
            if res[chave]:
                continue
            for nome in (base + '.exe', base):
                p = nome_insens(d, nome) if d.is_dir() else None
                if p and p.is_file():
                    res[chave] = p
                    break
    return res


def varrer_sfx(raiz):
    """Mapeia {(PAK, banco, som): Path} para raiz/<PAK>/Bank_NNN/sound_NNN.wav ou raiz/Bank_NNN/sound_NNN.wav."""
    mapa = {}
    raiz = Path(raiz)
    if not raiz.is_dir():
        return mapa
    if raiz.name.upper() in PAKS:
        pak = raiz.name.upper()
        for b in os.scandir(raiz):
            mb = _RE_BANCO.match(b.name)
            if not (b.is_dir() and mb):
                continue
            for s in os.scandir(b.path):
                ms = _RE_SOM.match(s.name)
                if ms and s.is_file():
                    mapa[(pak, int(mb.group(1)), int(ms.group(1)))] = Path(s.path)
        return mapa
    for pak_dir in os.scandir(raiz):
        if not pak_dir.is_dir() or pak_dir.name.upper() not in PAKS:
            continue
        pak = pak_dir.name.upper()
        for b in os.scandir(pak_dir.path):
            mb = _RE_BANCO.match(b.name)
            if not (b.is_dir() and mb):
                continue
            for s in os.scandir(b.path):
                ms = _RE_SOM.match(s.name)
                if ms and s.is_file():
                    mapa[(pak, int(mb.group(1)), int(ms.group(1)))] = Path(s.path)
    return mapa


def caminho_relativo_sfx(pak, banco, som):
    """Retorna o caminho relativo padrão no ModLoader/jogo para um som."""
    return Path('audio') / 'SFX' / pak / ('Bank_%03d' % banco) / ('sound_%03d.wav' % som)
