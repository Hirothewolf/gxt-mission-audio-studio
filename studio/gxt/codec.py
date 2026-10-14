# -*- coding: utf-8 -*-
"""
Codificação e decodificação de texto para a fonte do GTA San Andreas (GXT v4).
"""
import re
import functools

ACENTOS = {
    0x80: 'À', 0x81: 'Á', 0x82: 'Â', 0x83: 'Ã', 0x84: 'Æ', 0x85: 'Ç', 0x86: 'È', 0x87: 'É',
    0x88: 'Ê', 0x89: 'Ë', 0x8a: 'Ì', 0x8b: 'Í', 0x8c: 'Î', 0x8d: 'Ï', 0x8e: 'Ò', 0x8f: 'Ó',
    0x90: 'Ô', 0x91: 'Õ', 0x92: 'Ù', 0x93: 'Ú', 0x94: 'Û', 0x95: 'Ü', 0x96: 'ß', 0x97: 'à',
    0x98: 'á', 0x99: 'â', 0x9a: 'ã', 0x9b: 'æ', 0x9c: 'ç', 0x9d: 'è', 0x9e: 'é', 0x9f: 'ê',
    0xa0: 'ë', 0xa1: 'ì', 0xa2: 'í', 0xa3: 'î', 0xa4: 'ï', 0xa5: 'ò', 0xa6: 'ó', 0xa7: 'ô',
    0xa8: 'õ', 0xa9: 'ù', 0xaa: 'ú', 0xab: 'û', 0xac: 'ü',
}

REV = {v: k for k, v in ACENTOS.items()}

# Caracteres tipográficos que o jogo não suporta -> substituto compatível
NORMALIZA = {
    '\u201c': '"', '\u201d': '"', '\u2018': "'", '\u2019': "'", '\u2013': '-',
    '\u2014': '-', '\u2026': '...', '\u00a0': ' ', '\u00ab': '"', '\u00bb': '"'
}


def decode_text(b):
    """Converte bytes do jogo para texto legível/editável (bytes desconhecidos viram \\xNN)."""
    out = []
    for c in b:
        if c == 0x5C:
            out.append('\\\\')
        elif c >= 0x80 and c in ACENTOS:
            out.append(ACENTOS[c])
        elif c >= 0x80 or c < 0x20:
            out.append('\\x%02x' % c)
        else:
            out.append(chr(c))
    return ''.join(out)


def encode_text(s):
    """Converte texto editável para (bytes do jogo, lista de caracteres impossíveis de gravar)."""
    s = ''.join(NORMALIZA.get(ch, ch) for ch in s).replace('\r', '').replace('\n', ' ')
    out, bad, i = bytearray(), [], 0
    while i < len(s):
        ch = s[i]
        if ch == '\\':
            if s.startswith('\\\\', i):
                out.append(0x5C)
                i += 2
                continue
            m = re.match(r'\\x([0-9a-fA-F]{2})', s[i:])
            if m:
                out.append(int(m.group(1), 16))
                i += 4
                continue
            out.append(0x5C)
            i += 1
            continue
        if ch in REV:
            out.append(REV[ch])
        elif ord(ch) < 128:
            out.append(ord(ch))
        else:
            bad.append(ch)
        i += 1
    return bytes(out), bad


@functools.lru_cache(maxsize=60000)
def decode_text_c(b):
    """Versão de decode_text com cache LRU para renderização rápida."""
    return decode_text(b)
