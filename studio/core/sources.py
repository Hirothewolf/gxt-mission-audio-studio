# -*- coding: utf-8 -*-
"""
Fontes do jogo (SCM, texto, áudio, cutscenes) e geometria segura da janela.

Lógica pura, sem Tkinter: serve ao painel de início e pode ser testada sem tela.
Somente leitura: nada aqui altera arquivos do jogo.
"""
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from studio.core.utils import nome_insens

# Bancos de falas que o SAAT lê/extrai em audio/SFX.
PAKS_FALAS = ('SCRIPT', 'SPC_EA', 'SPC_FA', 'SPC_GA', 'SPC_NA', 'SPC_PA')


@dataclass
class Fonte:
    """Uma das fontes de dados do jogo que o editor consulta."""
    chave: str                      # 'scm' | 'texto' | 'audio' | 'cutscenes'
    titulo: str
    relativo: str                   # caminho esperado, relativo à pasta do jogo
    caminho: Optional[Path] = None  # onde foi encontrado (None se ausente)
    ok: bool = False
    detalhe: str = ''               # tamanho, quantidade, idiomas...
    dica: str = ''                  # como usar esta fonte
    avisos: List[str] = field(default_factory=list)

    @property
    def pasta(self):
        """Pasta a abrir no Explorer: a própria pasta, ou a que contém o arquivo."""
        if not self.caminho:
            return None
        return self.caminho if self.caminho.is_dir() else self.caminho.parent


def formatar_tamanho(n):
    n = float(n)
    for un in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or un == 'GB':
            return ('%d %s' % (n, un)) if un == 'B' else ('%.1f %s' % (n, un))
        n /= 1024.0


def _tamanho(p):
    try:
        return formatar_tamanho(p.stat().st_size)
    except OSError:
        return ''


def _achar(base, *partes):
    """Resolve base/partes[0]/partes[1]... ignorando maiúsculas; None se faltar."""
    atual = Path(base)
    for parte in partes:
        atual = nome_insens(atual, parte) if atual.is_dir() else None
        if atual is None:
            return None
    return atual


def _mod_dir(game_dir, mod_name):
    if not mod_name:
        return None
    ml = _achar(game_dir, 'modloader', mod_name)
    return ml if ml and ml.is_dir() else None


def _resolver(game_dir, mod, *partes):
    """(caminho, origem): o mod ativo sobrescreve o jogo base, como no ModLoader."""
    if mod:
        p = _achar(mod, *partes)
        if p:
            return p, 'mod'
    p = _achar(game_dir, *partes)
    return (p, 'jogo') if p else (None, None)


def _info_img(p):
    """Descreve um .img sem depender de bibliotecas externas."""
    try:
        with open(p, 'rb') as fh:
            cab = fh.read(8)
    except OSError:
        return ''
    if len(cab) == 8 and cab[:4] == b'VER2':
        return 'IMG v2 · %d entradas' % struct.unpack('<I', cab[4:8])[0]
    if nome_insens(p.parent, p.stem + '.dir'):
        return 'IMG v1 (com .dir)'
    return 'formato IMG não reconhecido'


def inspecionar_fontes(game_dir, mod_name=None):
    """Confere as quatro fontes que o editor usa e devolve uma lista de `Fonte`.

    game_dir: pasta do GTA San Andreas (ou None). mod_name: mod ativo em
    modloader/ (ou None). Nunca levanta por pasta ausente: devolve ok=False.
    """
    game = Path(game_dir) if game_dir else None
    jogo_ok = bool(game and game.is_dir())
    mod = _mod_dir(game, mod_name) if jogo_ok else None

    scm = Fonte(
        'scm', 'Script das missões', 'data/script/main.scm',
        dica='Abra no Sanny Builder (modo SA) para ver os blocos com load_mission_audio, '
             'attach_mission_audio_to_char e as chaves de texto.')
    texto = Fonte(
        'texto', 'Texto das falas (GXT)', 'text/american.gxt',
        dica='Use american.gxt ou o .gxt do seu idioma. As missões têm tabelas próprias, '
             'carregadas com load_mission_text.')
    audio = Fonte(
        'audio', 'Áudio das missões e falas', 'audio/SFX/',
        dica='Bancos que o SAAT lê e extrai (SCRIPT = missões; SPC_* = pedestres).')
    cuts = Fonte(
        'cutscenes', 'Cutscenes', 'anim/cuts.img',
        dica='Os arquivos .cut ficam dentro de anim/cuts.img.')
    fontes = [scm, texto, audio, cuts]
    if not jogo_ok:
        for f in fontes:
            f.avisos.append('Pasta do GTA San Andreas não configurada.')
        return fontes

    # SCM
    p, origem = _resolver(game, mod, 'data', 'script', 'main.scm')
    if p and p.is_file():
        scm.caminho, scm.ok = p, True
        scm.detalhe = _tamanho(p) + (' · sobrescrito pelo mod ativo' if origem == 'mod' else '')

    # Texto: o idioma ativo + todos os .gxt disponíveis
    pasta_txt = _achar(game, 'text')
    gxts = sorted(c.name for c in pasta_txt.iterdir()
                  if c.is_file() and c.suffix.lower() == '.gxt') if pasta_txt and pasta_txt.is_dir() else []
    p, origem = _resolver(game, mod, 'text', 'american.gxt')
    if p and p.is_file():
        texto.caminho, texto.ok = p, True
        texto.detalhe = _tamanho(p) + (' · sobrescrito pelo mod ativo' if origem == 'mod' else '')
        if len(gxts) > 1:
            texto.detalhe += ' · também: ' + ', '.join(n for n in gxts if n.lower() != 'american.gxt')
    elif gxts:
        texto.caminho, texto.ok = pasta_txt / gxts[0], True
        texto.detalhe = 'sem american.gxt; encontrado: ' + ', '.join(gxts)
        texto.avisos.append('american.gxt não existe; o idioma encontrado pode não ser o esperado.')

    # Áudio
    sfx = _achar(game, 'audio', 'SFX')
    if sfx and sfx.is_dir():
        audio.caminho = sfx
        presentes = [n for n in PAKS_FALAS if nome_insens(sfx, n)]
        audio.ok = 'SCRIPT' in presentes
        audio.detalhe = 'bancos: ' + (', '.join(presentes) if presentes else 'nenhum de falas')
        if not audio.ok:
            audio.avisos.append('Banco SCRIPT ausente: sem ele não há áudio de missões.')

    # Cutscenes
    p, origem = _resolver(game, mod, 'anim', 'cuts.img')
    if p and p.is_file():
        cuts.caminho, cuts.ok = p, True
        cuts.detalhe = ' · '.join(x for x in (_tamanho(p), _info_img(p)) if x)
        if origem == 'mod':
            cuts.detalhe += ' · sobrescrito pelo mod ativo'
    for f in fontes:
        if not f.ok and not f.avisos:
            f.avisos.append('Não encontrado em %s' % f.relativo)
    return fontes


def proximos_passos(game_dir, gxt_aberto, comparacao_aberta, saat_ok):
    """Checklist de primeiros passos: lista de (texto, concluído)."""
    jogo_ok = bool(game_dir and Path(game_dir).is_dir())
    return [
        ('Informar a pasta do GTA San Andreas', jogo_ok),
        ('Abrir o GXT do mod que será editado', bool(gxt_aberto)),
        ('Abrir o american.gxt original para comparação', bool(comparacao_aberta)),
        ('Configurar o SAAT (necessário para ouvir e extrair áudios)', bool(saat_ok)),
    ]


_GEOM_RE = re.compile(r'^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$')


def geometria_segura(geom, vx, vy, vw, vh, min_w=900, min_h=560):
    """Ajusta uma geometria Tk salva ('WxH+X+Y') para caber na área visível.

    Evita janela maior que a tela ou fora dela (monitor removido, resolução
    reduzida). (vx, vy, vw, vh) é a área visível total. None se ilegível.
    """
    m = _GEOM_RE.match((geom or '').strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    w = max(min(w, vw), min(min_w, vw))
    h = max(min(h, vh), min(min_h, vh))
    saida = '%dx%d' % (w, h)
    if m.group(3) is not None:
        x, y = int(m.group(3)), int(m.group(4))
        x = min(max(x, vx), vx + max(vw - w, 0))
        y = min(max(y, vy), vy + max(vh - h, 0))
        saida += '+%d+%d' % (x, y)
    return saida
