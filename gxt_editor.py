#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GXT Editor & Mission Audio Studio - editor, comparador de american.gxt e extrator de áudios de missão (GTA San Andreas, GXT versão 4)
Versão Modular 2.0 com suporte a download com confirmação de Modelos de IA e SAAT.

 * abre um GXT para editar (ex.: o pt-br) e outro só para comparar (ex.: o original)
 * navega por tabela (missão), mostra lado a lado o texto de comparação e o texto atual
 * destaca as diferenças palavra a palavra, filtra, busca em todas as tabelas
 * edita com acentos normais (á, ã, õ, ç...) e converte para os códigos do jogo ao salvar
 * exporta / importa CSV (útil para revisar fora do programa)
 * salva reconstruindo o arquivo (faz backup .bak na primeira gravação)
 * INTEGRADO: identifica o áudio de cada fala no arquivo nativo SCRIPT do GTA SA
 * INTEGRADO: player de áudio direto na interface (F5 ou duplo clique para ouvir a fala)
 * INTEGRADO: botão para extrair todos os áudios e comparativos CSV da missão selecionada
 * INTEGRADO: download automático com confirmação de Modelos de IA (TTS) e ferramentas SAAT

Requisitos: Python 3.8+ com tkinter (já vem no Python do Windows). Nenhuma biblioteca extra obrigatória.
Uso:   python gxt_editor.py [arquivo_para_editar.gxt] [arquivo_de_comparacao.gxt]
Teste: python gxt_editor.py --selftest arquivo.gxt
"""
import sys
from pathlib import Path

# Re-exportações dos módulos para retrocompatibilidade total com quaisquer scripts externos
from studio.config import (
    APP_DIR, CONFIG_PATH, CFG, cfg_load, cfg_save, achar_projeto_root, PROJ_ROOT,
    SESSION_FILE, AUDIO_INDEX_FILE, set_projeto_root
)
from studio.core.utils import (
    CREATE_FLAGS, escrever_json_atomico, matar_arvore, abrir_no_sistema,
    mostrar_no_explorer, escrever_log, app_data_dir, carregar_json_seguro, salvar_json_atomico
)
from studio.gxt.codec import (
    ACENTOS, REV, NORMALIZA, decode_text, encode_text, decode_text_c
)
from studio.gxt.jamcrc import (
    jamcrc, recuperar_nomes, carregar_cache_nomes, salvar_cache_nomes
)
from studio.gxt.model import (
    Gxt, norm_cmp, selftest
)
from studio.gxt.lint import (
    TAG_RE, _PONT_DUP, lint_avisos, FIX_LABELS, aplicar_fix
)
from studio.gxt.workspace import Workspace
from studio.audio.wav import (
    _DUR_CACHE, _WAV_RES, wav_duracao, classe_delta, resolver_wav
)
from studio.audio.player import Player
from studio.audio.resolver import (
    BaseAudioResolver, resolver_path_projeto,
    detectar_gta_sa_instalacoes, detectar_saat_sfx
)
from studio.script.mapping import (
    _SCRIPT_CACHE, carregar_script_cache, recarregar_script_cache, consultar_script_info
)
from studio.tts.registry import (
    ENGINES, _python_de_venv, detectar_engines, achar_python_ai, achar_python_chatterbox
)
from studio.ui.widgets import Tooltip
from studio.ui.app import (
    App, run_gui, FILTROS, FILTRO_PRED, PREDICADOS, NOMES_NAV, ATALHOS_TEXTO
)
from studio.tts.downloader import (
    CATALOGO_MODELOS, listar_modelos, obter_info_modelo, baixar_modelo
)
from studio.saat.downloader import (
    buscar_instalador_saat_local, obter_info_instalacao_saat, executar_instalacao_saat
)
from studio.ui.dialogs import (
    ConfirmarDownloadDialog, ProgressoOperacaoDialog, GerenciadorModelosDialog
)

try:
    import saat_modloader as SM
except Exception:
    SM = None


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--selftest':
        sys.exit(0 if selftest(sys.argv[2]) else 1)
    run_gui(*(sys.argv[1:3]))
