# -*- coding: utf-8 -*-
from .codec import ACENTOS, REV, NORMALIZA, decode_text, encode_text, decode_text_c
from .jamcrc import jamcrc, recuperar_nomes, carregar_cache_nomes, salvar_cache_nomes
from .model import Gxt, norm_cmp, selftest
from .lint import TAG_RE, _PONT_DUP, FIX_LABELS, lint_avisos, aplicar_fix
from .workspace import Workspace
