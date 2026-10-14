# -*- coding: utf-8 -*-
"""
Cálculo de JamCRC e heurísticas para recuperação de nomes originais de chave GXT.
"""
import zlib
import string
import json
from studio.config import PROJ_ROOT
from studio.core.utils import json_atomico


def jamcrc(nome):
    """Calcula o JamCRC (NOT do CRC32) de 32 bits usado como chave de texto no GTA."""
    return (~zlib.crc32(nome.encode())) & 0xFFFFFFFF


def recuperar_nomes(tabela, hashes):
    """Tenta descobrir nomes de chave do tipo SMO3_AB a partir do nome da tabela."""
    alvo = set(hashes)
    achados = {}
    pref = set()
    for i in range(len(tabela)):
        for j in range(i + 3, len(tabela) + 1):
            p = tabela[i:j]
            pref.add(p)
            for dg in string.digits:
                pref.add(p + dg)
                pref.add(p + '0' + dg)
    L = string.ascii_uppercase
    for p in pref:
        for a in L:
            for b in L:
                nome = '%s_%s%s' % (p, a, b)
                h = jamcrc(nome)
                if h in alvo:
                    achados[h] = nome
    return achados


def carregar_cache_nomes():
    """Carrega o cache de nomes de chaves descobertos."""
    try:
        p = PROJ_ROOT / '.cache' / 'gxt_names.json'
        if p.is_file():
            d = json.loads(p.read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def salvar_cache_nomes(d):
    """Salva o cache de nomes de chaves de forma atômica."""
    try:
        json_atomico(PROJ_ROOT / '.cache' / 'gxt_names.json', d)
    except OSError:
        pass
