# -*- coding: utf-8 -*-
"""
Validação (lint), avisos de estilo e correções automáticas para textos do GTA San Andreas.
"""
import re
from studio.gxt.codec import encode_text

TAG_RE = re.compile(r'~[^~]*~')
_PONT_DUP = re.compile(r'(?<!\.)\.\.(?!\.)|,,|;;|\?\?|!!')

FIX_LABELS = {
    'dup': 'Corrigir espaços duplos',
    'trim': 'Tirar espaços das pontas',
    'zspace': 'Tirar espaço após ~z~',
    'tags': 'Adicionar ~z~ inicial',
}


def lint_avisos(txt, ref=None):
    """Retorna [(mensagem, id_do_quick_fix ou None)]. Nunca altera o texto."""
    av = []
    if not txt.strip():
        av.append(('Texto vazio.', None))
    b, bad = encode_text(txt)
    if bad:
        av.append(('Caracteres que o jogo não tem: %s' % ' '.join(sorted(set(bad))), None))
    if txt.count('~') % 2:
        av.append(('Número ímpar de "~": alguma tag está aberta.', None))
    if re.search(r'(?<!~k)~~', txt):
        av.append(('Sequência "~~" (tag vazia ou colada).', None))
    if '  ' in txt:
        av.append(('Espaço duplo.', 'dup'))
    if txt != txt.strip():
        av.append(('Espaço no começo/fim.', 'trim'))
    if re.match(r'~z~ ', txt):
        av.append(('Espaço depois de ~z~.', 'zspace'))
    if _PONT_DUP.search(txt) and not (ref and _PONT_DUP.search(ref)):
        av.append(('Pontuação duplicada.', None))
    if ref is not None:
        if ref.startswith('~z~') and not txt.startswith('~z~'):
            av.append(('A comparação começa com ~z~ e o texto atual não.', 'tags'))
        tr, tt = TAG_RE.findall(ref), TAG_RE.findall(txt)
        if len(tr) != len(tt):
            falta = [t for t in tr if t not in tt]
            sobra = [t for t in tt if t not in tr]
            det = ''
            if falta:
                det += ' faltando: %s' % ' '.join(falta)
            if sobra:
                det += ' a mais: %s' % ' '.join(sobra)
            av.append(('Quantidade de tags diferente da comparação (%d vs %d);%s' % (len(tt), len(tr), det or ' confira'), None))
        elif tr != tt:
            av.append(('Tags em ordem/tipo diferente da comparação.', None))
        if txt.count('%') != ref.count('%'):
            av.append(('Quantidade de "%" diferente da comparação (placeholder?).', None))
        if len(re.findall(r'\\x[0-9a-fA-F]{2}', txt)) != len(re.findall(r'\\x[0-9a-fA-F]{2}', ref)):
            av.append(('Bytes especiais \\xNN diferentes da comparação.', None))
        limpo = TAG_RE.sub('', txt)
        if len(limpo) > 1.8 * len(TAG_RE.sub('', ref).strip()) + 25:
            av.append(('Bem mais longo que a comparação (pode estourar a caixa de legenda).', None))
    return av


def aplicar_fix(txt, fix, ref=None):
    """Aplica uma correção rápida (quick fix) ao texto."""
    if fix == 'dup':
        return re.sub(r' {2,}', ' ', txt)
    if fix == 'trim':
        return txt.strip()
    if fix == 'zspace':
        return re.sub(r'^(~z~) +', r'\1', txt)
    if fix == 'tags' and ref is not None and ref.startswith('~z~') and not txt.startswith('~z~'):
        return '~z~' + txt
    return txt
