# -*- coding: utf-8 -*-
"""
saat_modloader.py — áudio original do jogo (via SAAT, SOMENTE LEITURA) + instalação via ModLoader.

Parceiro do gxt_editor.py (opcional: se este arquivo não existir, o editor funciona como antes).
Agora estruturado de forma modular sob o pacote `studio.saat`.
"""
import sys
from pathlib import Path

# Re-exportações para total compatibilidade retroativa
from studio.core.utils import (
    CREATE_FLAGS, nome_insens as _nome_insens, sha256_arquivo,
    escrever_atomico, json_atomico, matar_arvore
)
from studio.audio.wav import (
    wav_info, converter_wav_pcm16_mono
)
from studio.script.mapping import (
    PAKS, _RE_BS, _RE_BS2, _RE_PAK, _CAMPOS_TEXTO, ref_do_registro
)
from studio.saat.detection import (
    _RE_BANCO, _RE_SOM, validar_pasta_jogo, detectar_pastas_jogo,
    achar_saat, varrer_sfx, caminho_relativo_sfx
)
from studio.saat.client import SaatClient
from studio.saat.audio import GameAudio, MOD_PADRAO
from studio.ui.saat_ui import texto_status, install_ui
from studio.saat.downloader import (
    buscar_instalador_saat_local, obter_info_instalacao_saat,
    executar_instalacao_saat
)


if __name__ == '__main__':
    # uso rápido sem GUI:  python saat_modloader.py "D:\Rockstar Games\GTA San Andreas"
    alvo = sys.argv[1] if len(sys.argv) > 1 else None
    achados = [Path(alvo)] if alvo else detectar_pastas_jogo()
    print("GTA San Andreas detectado em:", [str(p) for p in achados] or 'nenhum')
    for c in achados:
        v = validar_pasta_jogo(c)
        print(('✓' if v['ok'] else '✗'), c, '' if v['ok'] else v['faltando'])
    if achados:
        st = achar_saat(achados[0])
        print("SAAT SFX/Stream:", st)
