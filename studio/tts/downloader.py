# -*- coding: utf-8 -*-
"""
Gerenciamento e download automático de modelos de IA e síntese TTS com confirmação do usuário.
"""
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import List, Dict, Optional, Callable

from studio.config import CFG, cfg_save, PROJ_ROOT
from studio.tts.base import TTSModelInfo
from studio.saat.downloader import baixar_arquivo_com_progresso

PASTA_MODELOS_PADRAO = PROJ_ROOT / 'tools' / 'models'

CATALOGO_MODELOS: Dict[str, Dict] = {
    'piper_fabricio': {
        'id': 'piper_fabricio',
        'nome': 'Piper-TTS — Fabrício (pt_BR Médio)',
        'descricao': 'Voz neural de alta fidelidade em português brasileiro. Roda 100% offline em CPU, ideal para prévias e dublagens rápidas sem exigir placa de vídeo dedicada.',
        'tamanho_estimado': '~62 MB',
        'categoria': 'TTS (Voz pt_BR)',
        'requisitos': 'CPU pura (leve e rápido)',
        'subpasta': 'piper/fabricio',
        'arquivos': [
            {
                'nome': 'pt_BR-fabricio-medium.onnx',
                'url': 'https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/fabricio/medium/pt_BR-fabricio-medium.onnx'
            },
            {
                'nome': 'pt_BR-fabricio-medium.onnx.json',
                'url': 'https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/fabricio/medium/pt_BR-fabricio-medium.onnx.json'
            }
        ]
    },
    'piper_edresson': {
        'id': 'piper_edresson',
        'nome': 'Piper-TTS — Edresson (pt_BR)',
        'descricao': 'Voz neural ultra rápida e compacta para síntese instantânea em português brasileiro.',
        'tamanho_estimado': '~31 MB',
        'categoria': 'TTS (Voz pt_BR)',
        'requisitos': 'CPU pura (< 50MB RAM)',
        'subpasta': 'piper/edresson',
        'arquivos': [
            {
                'nome': 'pt_BR-edresson-low.onnx',
                'url': 'https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx'
            },
            {
                'nome': 'pt_BR-edresson-low.onnx.json',
                'url': 'https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx.json'
            }
        ]
    },
    'chatterbox_ptbr': {
        'id': 'chatterbox_ptbr',
        'nome': 'Chatterbox Multilingual (Weights)',
        'descricao': 'Pesos neurais para clonagem de voz de alta expressividade (Chatterbox AI). Suporta clonagem de voz a partir de áudios de referência dos personagens do GTA.',
        'tamanho_estimado': '~1.1 GB',
        'categoria': 'Voice Cloning',
        'requisitos': 'GPU (CUDA recomendada) ou CPU com >8GB RAM',
        'subpasta': 'chatterbox',
        'hf_repo': 'ResembleAI/Chatterbox',
        'arquivos': [
            {
                'nome': 'chatterbox_weights.pt',
                'url': 'https://huggingface.co/ResembleAI/Chatterbox/resolve/main/model.safetensors'
            }
        ]
    },
    'f5tts_base': {
        'id': 'f5tts_base',
        'nome': 'F5-TTS Base Checkpoint (EN/ZH)',
        'descricao': (
            'Checkpoint oficial F5-TTS Base para inglês e chinês. O catálogo baixa os pesos e o vocabulário, '
            'mas isso não instala o runtime. Não há modelo pt_BR validado para esta variante; consulte '
            'https://github.com/SWivid/F5-TTS para os requisitos oficiais antes de usar.'
        ),
        'tamanho_estimado': '~1.2 GB',
        'categoria': 'Voice Cloning',
        'requisitos': 'Runtime F5-TTS separado; Base oficial EN/ZH, sem suporte pt_BR validado',
        'subpasta': 'f5tts',
        'hf_repo': 'SWivid/F5-TTS',
        'arquivos': [
            {
                'nome': 'model_1200000.safetensors',
                'url': 'https://huggingface.co/SWivid/F5-TTS/resolve/main/F5TTS_Base/model_1200000.safetensors'
            },
            {
                'nome': 'vocab.txt',
                'url': 'https://huggingface.co/SWivid/F5-TTS/resolve/main/F5TTS_Base/vocab.txt'
            }
        ]
    },
    'whisper_small': {
        'id': 'whisper_small',
        'nome': 'OpenAI Whisper Small (Alinhamento & Transcrição)',
        'descricao': 'Modelo para reconhecimento e alinhamento de fala (usado para transcrever referências originais e sincronizar tempos das legendas).',
        'tamanho_estimado': '~460 MB',
        'categoria': 'Transcrição / Alinhamento',
        'requisitos': 'CPU ou GPU',
        'subpasta': 'whisper/small',
        'arquivos': [
            {
                'nome': 'small.pt',
                'url': 'https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d7205242f44e29a48b380497655e67174a2ec/small.pt'
            }
        ]
    }
}


# Verified against the official Hugging Face model API. These are the files
# needed to load each Base checkpoint and its bundled speech tokenizer; runtime
# packages/scripts are installed and detected separately.
_QWEN_BASE_FILES = (
    'config.json',
    'generation_config.json',
    'merges.txt',
    'model.safetensors',
    'preprocessor_config.json',
    'speech_tokenizer/config.json',
    'speech_tokenizer/configuration.json',
    'speech_tokenizer/model.safetensors',
    'speech_tokenizer/preprocessor_config.json',
    'tokenizer_config.json',
    'vocab.json',
)


def _qwen_base_files(repo: str) -> list:
    base = f'https://huggingface.co/{repo}/resolve/main/'
    return [{'nome': name, 'url': base + name} for name in _QWEN_BASE_FILES]


CATALOGO_MODELOS.update({
    'qwen3_tts_06b_base': {
        'id': 'qwen3_tts_06b_base',
        'nome': 'Qwen3-TTS 0.6B Base — Clonagem de Voz',
        'descricao': 'Pesos Base para clonagem de voz a partir de áudio de referência e transcrição. O runtime Python Qwen3-TTS é configurado separadamente.',
        'tamanho_estimado': '~2.52 GB',
        'categoria': 'Voice Cloning',
        'requisitos': 'Pesos do modelo; runtime Python Qwen3-TTS separado',
        'subpasta': 'qwen3-tts/0.6b-base',
        'hf_repo': 'Qwen/Qwen3-TTS-12Hz-0.6B-Base',
        'arquivos': _qwen_base_files('Qwen/Qwen3-TTS-12Hz-0.6B-Base'),
    },
    'qwen3_tts_17b_base': {
        'id': 'qwen3_tts_17b_base',
        'nome': 'Qwen3-TTS 1.7B Base — Clonagem de Voz',
        'descricao': 'Pesos Base de maior capacidade para clonagem de voz a partir de áudio de referência e transcrição. O runtime Python Qwen3-TTS é configurado separadamente.',
        'tamanho_estimado': '~4.54 GB',
        'categoria': 'Voice Cloning',
        'requisitos': 'Pesos do modelo; runtime Python Qwen3-TTS separado',
        'subpasta': 'qwen3-tts/1.7b-base',
        'hf_repo': 'Qwen/Qwen3-TTS-12Hz-1.7B-Base',
        'arquivos': _qwen_base_files('Qwen/Qwen3-TTS-12Hz-1.7B-Base'),
    },
})


_QWEN_LEGACY_FOLDERS = {
    'qwen3_tts_06b_base': 'qwen3_tts_0_6b_base',
    'qwen3_tts_17b_base': 'qwen3_tts_1_7b_base',
}


def _arquivos_presentes(model_dict: Dict, folder: Path) -> bool:
    arquivos = model_dict.get('arquivos', [])
    try:
        return bool(arquivos) and folder.is_dir() and all(
            (folder / item['nome']).is_file() and (folder / item['nome']).stat().st_size > 0
            for item in arquivos
        )
    except OSError:
        return False


def obter_pasta_modelo(model_dict: Dict) -> Path:
    """Retorna o diretório local onde o modelo deve ser instalado."""
    # PROJ_ROOT is synchronized on project changes by studio.config; calculate
    # this per call instead of retaining an import-time destination.
    primary = PROJ_ROOT / 'tools' / 'models' / model_dict.get('subpasta', model_dict['id'])
    model_id = model_dict.get('id')
    legacy_folder = _QWEN_LEGACY_FOLDERS.get(model_id)
    if legacy_folder:
        if _arquivos_presentes(model_dict, primary):
            return primary
        legacy = PROJ_ROOT / 'models' / legacy_folder
        if _arquivos_presentes(model_dict, legacy):
            return legacy
    return primary


def verificar_modelo_instalado(model_dict: Dict) -> bool:
    """Verifica se todos os arquivos do modelo já existem no disco."""
    pasta = obter_pasta_modelo(model_dict)
    return _arquivos_presentes(model_dict, pasta)


def obter_modelos_disponiveis() -> List[TTSModelInfo]:
    """Retorna a lista de modelos do catálogo com status de instalação."""
    resultado = []
    preferred = ('qwen3_tts_06b_base', 'qwen3_tts_17b_base')
    ordered_ids = [model_id for model_id in preferred if model_id in CATALOGO_MODELOS]
    ordered_ids.extend(model_id for model_id in CATALOGO_MODELOS if model_id not in preferred)
    for m_id in ordered_ids:
        m = CATALOGO_MODELOS[m_id]
        instalado = verificar_modelo_instalado(m)
        pasta = obter_pasta_modelo(m)
        nomes_arqs = [a['nome'] for a in m.get('arquivos', [])]
        info = TTSModelInfo(
            id=m_id,
            nome=m['nome'],
            descricao=m['descricao'],
            tamanho_estimado=m['tamanho_estimado'],
            arquivos=nomes_arqs,
            url_download=m.get('arquivos', [{}])[0].get('url'),
            hf_repo=m.get('hf_repo'),
            categoria=m.get('categoria', 'TTS'),
            requisitos=m.get('requisitos', 'Geral'),
            instalado=instalado,
            caminho_local=pasta if instalado else None
        )
        resultado.append(info)
    return resultado


def listar_modelos() -> List[TTSModelInfo]:
    """Compatibility alias used by the current UI and diagnostic views."""
    return obter_modelos_disponiveis()


def obter_info_modelo(model_id: str) -> Optional[Dict]:
    """Retorna dicionário com dados completos para exibição no diálogo de confirmação."""
    m = CATALOGO_MODELOS.get(model_id)
    if not m:
        return None
    pasta = obter_pasta_modelo(m)
    return {
        'id': model_id,
        'nome': m['nome'],
        'descricao': m['descricao'],
        'tamanho_estimado': m['tamanho_estimado'],
        'categoria': m['categoria'],
        'requisitos': m['requisitos'],
        'destino': str(pasta),
        'arquivos': [a['nome'] for a in m.get('arquivos', [])],
        'urls': [a['url'] for a in m.get('arquivos', []) if 'url' in a]
    }


def baixar_modelo(model_id: str,
                  confirmar_callback: Optional[Callable[[Dict], bool]] = None,
                  on_progress: Optional[Callable[[int, Optional[int], str], None]] = None,
                  cancel_event = None) -> Dict:
    """
    Executa o download de um modelo de IA respeitando a confirmação do usuário:
    1. Verifica metadados.
    2. Pede confirmação via confirmar_callback(info). Se o usuário recusar, cancela.
    3. Cria a pasta destino.
    4. Baixa os arquivos com callbacks de progresso.
    5. Registra o modelo na configuração.
    """
    m = CATALOGO_MODELOS.get(model_id)
    if not m:
        return {'ok': False, 'motivo': f'Modelo não encontrado no catálogo: {model_id}'}

    info = obter_info_modelo(model_id)
    if confirmar_callback:
        aceitou = confirmar_callback(info)
        if not aceitou:
            return {'ok': False, 'motivo': 'Download cancelado pelo usuário.', 'cancelado': True}

    destino = obter_pasta_modelo(m)
    destino.mkdir(parents=True, exist_ok=True)

    arquivos = m.get('arquivos', [])
    total_arqs = len(arquivos)

    for idx, arq in enumerate(arquivos, start=1):
        nome_arq = arq['nome']
        url = arq['url']
        dest_arq = destino / nome_arq

        if cancel_event and cancel_event.is_set():
            return {'ok': False, 'motivo': 'Download cancelado pelo usuário.'}

        msg = f"Baixando {nome_arq} ({idx}/{total_arqs})..."
        if on_progress:
            on_progress(0, None, msg)

        # Baixa em arquivo temporário antes de substituir
        with tempfile.NamedTemporaryFile(delete=False, suffix='.part', dir=destino) as tmp_f:
            tmp_path = Path(tmp_f.name)

        try:
            sucesso = baixar_arquivo_com_progresso(
                url, tmp_path,
                on_progress=lambda b, t: on_progress(b, t, msg) if on_progress else None,
                cancel_event=cancel_event
            )
            if not sucesso or (cancel_event and cancel_event.is_set()):
                if tmp_path.exists():
                    tmp_path.unlink()
                return {'ok': False, 'motivo': 'Download cancelado ou interrompido.'}

            dest_arq.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, dest_arq)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink()
            return {'ok': False, 'motivo': f'Erro ao baixar {nome_arq}: {e}'}

    # Registra o modelo instalado em CFG
    CFG.setdefault('modelos_instalados', {})[model_id] = {
        'nome': m['nome'],
        'pasta': str(destino),
        'arquivos': [a['nome'] for a in arquivos]
    }
    cfg_save()

    return {
        'ok': True,
        'motivo': f"Modelo {m['nome']} instalado com sucesso em {destino}!",
        'pasta': destino
    }
