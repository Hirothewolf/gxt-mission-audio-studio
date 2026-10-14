# -*- coding: utf-8 -*-
"""
Gerenciamento de configuração e detecção de caminhos de projeto.
"""
import os
import json
import sys
from pathlib import Path
from studio.core.utils import json_atomico, app_data_dir, carregar_json_seguro

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / 'gxt_editor_config.json'
SESSION_FILE = app_data_dir() / 'session.json'
AUDIO_INDEX_FILE = app_data_dir() / 'base_audio_index.json'


def cfg_load():
    """Carrega as configurações salvas em gxt_editor_config.json."""
    try:
        if CONFIG_PATH.is_file():
            d = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


CFG = cfg_load()


def cfg_save():
    """Salva o dicionário global CFG no arquivo de configuração atomicamente."""
    try:
        json_atomico(CONFIG_PATH, CFG)
    except OSError:
        pass


def _parece_projeto_dub(p):
    """Verifica se o diretório contém manifestos ou pastas estruturais de dublagem."""
    try:
        p = Path(p).resolve()
        if any((p / 'manifests' / name).is_file() for name in (
            'script_mapping_v3.jsonl', 'script_mapping_v2.jsonl', 'script_mapping.jsonl'
        )):
            return True
        if (p / 'missoes_extraidas').is_dir() and (p / 'redub_output').is_dir():
            return True
    except Exception:
        pass
    return False


def achar_projeto_root(extra_cands=None):
    """Localiza a raiz do projeto de localização/dublagem pesquisando múltiplos locais prováveis."""
    cand = []
    for env in ('GXT_PROJECT_ROOT', 'GXT_STUDIO_ROOT'):
        v = os.environ.get(env, '').strip()
        if v:
            cand.append(Path(v))

    if CFG.get('project_root'):
        cand.append(Path(CFG['project_root']))

    # Verificar raiz gravada na sessão de usuário
    sessao = carregar_json_seguro(SESSION_FILE, {})
    if sessao.get('project_root'):
        cand.append(Path(sessao['project_root']))

    if extra_cands:
        for ec in extra_cands:
            if ec:
                cand.append(Path(ec))

    # Aplicação, diretório atual e caminhos estruturais descobertos acima.
    cand += [
        APP_DIR,
        APP_DIR.parent,
        Path.cwd(),
    ]

    # Verificar pastas pai de arquivos recentes ou diretório do jogo
    for rec in (CFG.get('recentes') or []):
        try:
            cand.extend(list(Path(rec).parents)[:4])
        except Exception:
            pass

    for p in cand:
        if _parece_projeto_dub(p):
            resolved = p.resolve()
            if CFG.get('project_root') != str(resolved):
                CFG['project_root'] = str(resolved)
                cfg_save()
            return resolved

    # Fallback: pasta configurada ou APP_DIR
    fallback = Path(CFG['project_root']) if CFG.get('project_root') else APP_DIR
    return fallback


PROJ_ROOT = achar_projeto_root()


def get_project_root():
    """Return the current project root for callers that must follow root switches."""
    return PROJ_ROOT


def set_projeto_root(novo_path):
    """Atualiza a raiz do projeto em tempo de execução e persiste na configuração."""
    global PROJ_ROOT
    p = Path(novo_path).resolve()
    PROJ_ROOT = p
    CFG['project_root'] = str(p)
    cfg_save()
    try:
        import studio.script.mapping
        studio.script.mapping._SCRIPT_CACHE = None
    except Exception:
        pass
    try:
        import studio.audio.wav as wav
        wav._DUR_CACHE.clear()
        wav._WAV_RES.clear()
    except Exception:
        pass
    # Keep compatibility imports (including the legacy facade) synchronized.
    for nome, modulo in tuple(sys.modules.items()):
        if modulo is not None and (nome.startswith('studio.') or nome in ('gxt_editor', 'saat_modloader')):
            if hasattr(modulo, 'PROJ_ROOT'):
                try:
                    modulo.PROJ_ROOT = p
                except Exception:
                    pass
    return p
