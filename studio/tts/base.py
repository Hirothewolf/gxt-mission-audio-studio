# -*- coding: utf-8 -*-
"""
Contrato e abstrações padronizadas para engines de síntese de voz e modelos de IA.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from pathlib import Path


@dataclass
class TTSCapabilities:
    # Default to unavailable. Adapters opt in only when the real engine
    # interface exposes and supports the feature.
    voice_cloning: bool = False
    reference_audio: bool = False
    reference_text: bool = False
    emotion_control: bool = False
    speed_control: bool = False
    batch_generation: bool = False
    custom_checkpoint: bool = False
    offline_cpu: bool = False


@dataclass
class TTSModelInfo:
    id: str
    nome: str
    descricao: str
    tamanho_estimado: str
    arquivos: List[str]
    url_download: Optional[str] = None
    hf_repo: Optional[str] = None
    categoria: str = 'TTS'  # 'TTS', 'Transcrição', 'Alinhamento'
    requisitos: str = 'CPU / GPU'
    instalado: bool = False
    caminho_local: Optional[Path] = None


@dataclass
class SynthesisRequest:
    text: str
    output_path: Path
    reference_audio: Optional[Path] = None
    reference_text: Optional[str] = None
    speed: float = 1.0
    emotion: Optional[str] = None
    seed: Optional[int] = None
    extra_args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SynthesisResult:
    ok: bool
    output_path: Optional[Path] = None
    duration: Optional[float] = None
    logs: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
