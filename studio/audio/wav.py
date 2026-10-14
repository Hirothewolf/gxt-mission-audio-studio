# -*- coding: utf-8 -*-
"""
Análise, extração de metadados e conversão PCM16 mono de áudios WAV usando stdlib.
"""
import os
import sys
import array
import struct
import re
from pathlib import Path
import studio.config as config

_DUR_CACHE = {}
_WAV_RES = {}


def wav_duracao(path):
    """Retorna a duração de um WAV estruturalmente válido; None se ilegível."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns)
    if key in _DUR_CACHE:
        return _DUR_CACHE[key]
    info = wav_info(path)
    dur = info['dur'] if info else None
    _DUR_CACHE[key] = dur
    return dur


def classe_delta(orig, dub):
    """Calcula a diferença percentual de duração entre áudio original e dublado."""
    if not orig or not dub:
        return ''
    lim = config.CFG.get('limites_delta') or (0.10, 0.25, 0.40)
    pct = (dub - orig) / orig
    a = abs(pct)
    icone = '🟢' if a <= lim[0] else '🟡' if a <= lim[1] else '🟠' if a <= lim[2] else '🔴'
    return 'Δ %+.2fs (%+.1f%%) %s' % (dub - orig, pct * 100, icone)


def resolver_wav(p):
    """Localiza o arquivo WAV mesmo se referenciado de outro caminho absoluto."""
    if not p:
        return None
    if p in _WAV_RES:
        return _WAV_RES[p]
    achado = None
    pp = Path(p)
    project_root = config.get_project_root()
    cands = [pp, project_root / pp]
    partes = [x for x in re.split(r'[\\/]+', str(p)) if x and not x.endswith(':')]
    for n in (4, 3, 2):
        if len(partes) > n - 1:
            cands.append(project_root.joinpath(*partes[-n:]))
    for c in cands:
        try:
            if c.is_file():
                achado = c
                break
        except OSError:
            pass
    _WAV_RES[p] = achado
    return achado


def wav_info(path):
    """Retorna dicionário com metadados do WAV: fmt, canais, taxa, bits, dados, dur."""
    try:
        with open(path, 'rb') as f:
            cab = f.read(12)
            if cab[:4] != b'RIFF' or cab[8:12] != b'WAVE':
                return None
            tam = os.fstat(f.fileno()).st_size
            riff_size = struct.unpack('<I', cab[4:8])[0]
            riff_end = 8 + riff_size
            if riff_size < 4 or riff_end > tam:
                return None
            info = None
            dados = None
            while f.tell() < riff_end:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                cid, sz = ch[:4], struct.unpack('<I', ch[4:])[0]
                off = f.tell()
                end = off + sz
                next_chunk = end + (sz & 1)
                if end > riff_end or next_chunk > riff_end:
                    return None
                if cid == b'fmt ':
                    if sz < 16:
                        return None
                    fmt = f.read(sz + (sz & 1))
                    tag, canais, taxa, _br, _ba, bits = struct.unpack('<HHIIHH', fmt[:16])
                    if tag == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE
                        tag = struct.unpack('<H', fmt[24:26])[0]
                    info = {'fmt': tag, 'canais': canais, 'taxa': taxa, 'bits': bits,
                            'block_align': _ba}
                    if canais < 1 or taxa < 1 or bits < 1 or _ba < 1:
                        return None
                elif cid == b'data':
                    dados = (off, sz)
                    f.seek(sz, 1)
                else:
                    f.seek(sz, 1)
                if sz & 1:
                    f.seek(1, 1)
            if not info or not dados:
                return None
            bytes_per_sample = (info['bits'] + 7) // 8
            expected_align = bytes_per_sample * info['canais']
            if info['block_align'] != expected_align or dados[1] % expected_align:
                return None
            if info['fmt'] == 1 and info['bits'] not in (8, 16, 24, 32):
                return None
            if info['fmt'] == 3 and info['bits'] not in (32, 64):
                return None
            if info['fmt'] not in (1, 3):
                return None
            info['dados'] = dados
            info['dur'] = dados[1] / float(expected_align * info['taxa'])
            return info
    except (OSError, struct.error):
        pass
    return None


def converter_wav_pcm16_mono(origem, taxa_alvo=None, limite_pico=0.98):
    """
    Lê WAV (PCM 8/16/24/32 ou float32/64), converte para mono, reamostra se solicitado
    e devolve os bytes de um WAV PCM 16-bit mono.
    """
    info = wav_info(origem)
    if not info:
        raise ValueError('WAV ilegível')
    off, n = info['dados']
    with open(origem, 'rb') as f:
        f.seek(off)
        bruto = f.read(n)
    canais, bits, tag = info['canais'], info['bits'], info['fmt']
    if tag == 1 and bits == 16:
        a = array.array('h')
        a.frombytes(bruto[: len(bruto) // 2 * 2])
        if sys.byteorder == 'big':
            a.byteswap()
        amostras = [x / 32768.0 for x in a]
    elif tag == 1 and bits == 8:
        amostras = [(x - 128) / 128.0 for x in bruto]
    elif tag == 1 and bits == 24:
        amostras = []
        for i in range(0, len(bruto) - 2, 3):
            v = int.from_bytes(bruto[i:i + 3], 'little', signed=True)
            amostras.append(v / 8388608.0)
    elif tag == 1 and bits == 32:
        a = array.array('i')
        a.frombytes(bruto[: len(bruto) // 4 * 4])
        amostras = [x / 2147483648.0 for x in a]
    elif tag == 3 and bits in (32, 64):
        a = array.array('f' if bits == 32 else 'd')
        a.frombytes(bruto[: len(bruto) // (bits // 8) * (bits // 8)])
        amostras = [max(-1.0, min(1.0, x)) if x == x else 0.0 for x in a]
    else:
        raise ValueError('formato WAV não suportado (tag=%s, bits=%s)' % (tag, bits))
    if canais > 1:
        amostras = [sum(amostras[i:i + canais]) / canais for i in range(0, len(amostras) - canais + 1, canais)]
    taxa = info['taxa']
    if taxa_alvo and taxa_alvo != taxa and amostras:
        novo_n = max(1, int(round(len(amostras) * taxa_alvo / float(taxa))))
        passo = (len(amostras) - 1) / float(max(1, novo_n - 1))
        out = []
        for i in range(novo_n):
            pos = i * passo
            j = int(pos)
            frac = pos - j
            b = amostras[min(j + 1, len(amostras) - 1)]
            out.append(amostras[j] * (1 - frac) + b * frac)
        amostras, taxa = out, taxa_alvo
    pico = max((abs(x) for x in amostras), default=0.0)
    if pico > limite_pico:
        amostras = [x * (limite_pico / pico) for x in amostras]
    pcm = array.array('h', (int(round(max(-1.0, min(1.0, x)) * 32767)) for x in amostras))
    if sys.byteorder == 'big':
        pcm.byteswap()
    dados = pcm.tobytes()
    cab = b'RIFF' + struct.pack('<I', 36 + len(dados)) + b'WAVE'
    cab += b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, taxa, taxa * 2, 2, 16)
    cab += b'data' + struct.pack('<I', len(dados))
    return cab + dados
