# -*- coding: utf-8 -*-
from .base import (
    TTSCapabilities, TTSModelInfo, SynthesisRequest, SynthesisResult
)
from .registry import (
    ENGINES, detectar_engines, achar_python_ai, achar_python_chatterbox, _python_de_venv
)
from .downloader import (
    CATALOGO_MODELOS, listar_modelos, obter_info_modelo,
    baixar_modelo, verificar_modelo_instalado
)
