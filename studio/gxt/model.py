# -*- coding: utf-8 -*-
"""
Manipulação e representação de arquivos GXT (versão 4) do GTA San Andreas.
"""
import os
import re
import shutil
import struct
from pathlib import Path
from datetime import datetime
from studio.gxt.codec import decode_text, encode_text


class Gxt:
    """Um arquivo GXT carregado na memória (texto em bytes, na codificação do jogo)."""

    def __init__(self, path, data=None):
        self.path = path
        if data is None:
            with open(path, 'rb') as fh:
                d = fh.read()
        else:
            d = data
        if d[4:8] != b'TABL':
            raise ValueError('Arquivo não reconhecido (esperado GXT versão 4 do GTA San Andreas).')
        self.ver, self.bits = struct.unpack('<HH', d[:4])
        n = struct.unpack('<I', d[8:12])[0] // 12
        self.order, self.t = [], {}
        for i in range(n):
            nome = d[12 + i * 12:20 + i * 12].split(b'\0')[0].decode('latin-1')
            off = struct.unpack('<I', d[20 + i * 12:24 + i * 12])[0]
            p = off + (0 if nome == 'MAIN' else 8)
            if d[p:p + 4] != b'TKEY':
                raise ValueError('Tabela %s corrompida.' % nome)
            ksz = struct.unpack('<I', d[p + 4:p + 8])[0]
            keys = [struct.unpack('<II', d[p + 8 + j * 8:p + 16 + j * 8]) for j in range(ksz // 8)]
            p2 = p + 8 + ksz
            dsz = struct.unpack('<I', d[p2 + 4:p2 + 8])[0]
            dados = d[p2 + 8:p2 + 8 + dsz]
            hashes = [h for o, h in keys]
            offs = {h: o for o, h in keys}
            orig = {h: dados[o:dados.index(b'\0', o)] for o, h in keys}
            self.order.append(nome)
            self.t[nome] = {'hashes': hashes, 'off': offs, 'orig': orig, 'cur': dict(orig)}
        self._source_data = bytes(d)
        self._source_layout = tuple((name, tuple(self.t[name]['hashes'])) for name in self.order)

    # -- consultas
    def n_edited(self, tab):
        t = self.t[tab]
        return sum(1 for h in t['hashes'] if t['cur'][h] != t['orig'][h])

    def total_edited(self):
        return sum(self.n_edited(n) for n in self.order)

    # -- gravação
    def build(self):
        layout = tuple((name, tuple(self.t[name]['hashes'])) for name in self.order)
        if layout == self._source_layout and self.total_edited() == 0:
            # Preserve original offsets, shared substrings, padding and any
            # unused data when nothing was edited. Repacking is only needed
            # for an actual text change.
            return self._source_data
        blocos = []
        for nome in self.order:
            t = self.t[nome]
            por_off = sorted(t['hashes'], key=lambda h: t['off'][h])
            dados, novo, prev = bytearray(), {}, None
            for h in por_off:
                s = t['cur'][h]
                if prev is not None and t['off'][prev] == t['off'][h] and t['cur'][prev] == s:
                    novo[h] = novo[prev]          # strings que dividiam o mesmo offset
                else:
                    novo[h] = len(dados)
                    dados += s + b'\0'
                prev = h
            keys = b''.join(struct.pack('<II', novo[h], h) for h in t['hashes'])
            blk = b'' if nome == 'MAIN' else nome.encode('latin-1').ljust(8, b'\0')
            blk += b'TKEY' + struct.pack('<I', len(keys)) + keys
            blk += b'TDAT' + struct.pack('<I', len(dados)) + bytes(dados)
            blocos.append((nome, blk))
        head = struct.pack('<HH', self.ver, self.bits) + b'TABL' + struct.pack('<I', 12 * len(blocos))
        off = len(head) + 12 * len(blocos)
        tabl = b''
        for nome, blk in blocos:
            tabl += nome.encode('latin-1').ljust(8, b'\0') + struct.pack('<I', off)
            off += len(blk)
        return head + tabl + b''.join(b for _, b in blocos)

    def validar(self, data):
        """Reabre os bytes que SERIAM gravados e confere com o que está na memória."""
        chk = Gxt(self.path or '', data=data)
        if chk.order != self.order:
            raise ValueError('Validação falhou: ordem/lista de tabelas diferente.')
        for n in self.order:
            if chk.t[n]['hashes'] != self.t[n]['hashes']:
                raise ValueError('Validação falhou: chaves da tabela %s diferem.' % n)
            for h in self.t[n]['hashes']:
                if chk.t[n]['orig'].get(h) != self.t[n]['cur'][h]:
                    raise ValueError('Validação falhou: texto divergente em %s/%08X.' % (n, h))

    def save(self, path, backups_dir=None, max_backups=20):
        """Valida -> backups (.bak na 1ª vez + cópia versionada) -> grava atômico."""
        data = self.build()
        self.validar(data)
        path = os.fspath(path)
        if os.path.exists(path):
            if not os.path.exists(path + '.bak'):
                shutil.copy2(path, path + '.bak')
            if backups_dir:
                try:
                    bd = Path(backups_dir)
                    bd.mkdir(parents=True, exist_ok=True)
                    stem = Path(path).stem
                    shutil.copy2(path, bd / ('%s_%s.gxt' % (stem, datetime.now().strftime('%Y%m%d_%H%M%S'))))
                    antigos = sorted(bd.glob(stem + '_*.gxt'))
                    for velho in antigos[:max(0, len(antigos) - int(max_backups))]:
                        velho.unlink()
                except OSError:
                    pass
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self.path = path
        for t in self.t.values():
            t['orig'] = dict(t['cur'])
        self._source_data = data
        self._source_layout = tuple((name, tuple(self.t[name]['hashes'])) for name in self.order)


def norm_cmp(b, ignorar_tags):
    """Normaliza bytes para comparação (opcionalmente remove tags ~k~)."""
    if not ignorar_tags:
        return b
    b = re.sub(rb'~[^~]*~', b' ', b)
    return re.sub(rb'\s+', b' ', b).strip()


def selftest(path):
    """Verifica a reconstrução binária fiel e conversão de strings do arquivo GXT."""
    g = Gxt(path)
    raw = open(path, 'rb').read()
    ok1 = g.build() == raw
    erros = 0
    for n in g.order:
        for h in g.t[n]['hashes']:
            b = g.t[n]['cur'][h]
            b2, bad = encode_text(decode_text(b))
            if b2 != b or bad:
                erros += 1
    print('reconstrução idêntica:', ok1)
    print('strings que não fazem ida e volta texto<->bytes:', erros)
    return ok1 and erros == 0
