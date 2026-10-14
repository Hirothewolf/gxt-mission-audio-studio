# -*- coding: utf-8 -*-
"""
Utilitários de sistema, arquivos e processos para o Studio.
"""
import os
import sys
import json
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def nome_insens(pasta, nome):
    """Localiza 'nome' dentro de 'pasta' ignorando maiúsculas/minúsculas."""
    p = Path(pasta) / nome
    if p.exists():
        return p
    try:
        nome_lower = nome.lower()
        for c in Path(pasta).iterdir():
            if c.name.lower() == nome_lower:
                return c
    except OSError:
        pass
    return None


def sha256_arquivo(p, bloco=1 << 20):
    """Calcula o hash SHA-256 de um arquivo em blocos."""
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while True:
            b = f.read(bloco)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def escrever_atomico(destino, dados):
    """Grava bytes atomicamente via arquivo temporário + os.replace."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(dados)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, destino)


def json_atomico(path, obj):
    """Grava objeto serializável em JSON de forma atômica."""
    escrever_atomico(path, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))


def escrever_json_atomico(path, obj):
    """Compatibilidade retroativa: grava JSON atomicamente."""
    json_atomico(path, obj)


def salvar_json_atomico(path, obj):
    """Alias para json_atomico / escrever_json_atomico."""
    json_atomico(path, obj)


def carregar_json_seguro(path, padrao=None):
    """Lê um arquivo JSON de forma segura sem lançar exceções."""
    if padrao is None:
        padrao = {}
    try:
        p = Path(path)
        if p.is_file():
            dados = json.loads(p.read_text(encoding='utf-8'))
            return dados if isinstance(dados, type(padrao)) else padrao
    except Exception:
        pass
    return padrao


def app_data_dir():
    """Retorna diretório persistente de dados do usuário (ex: %LOCALAPPDATA%/GXTStudio)."""
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if base:
        p = Path(base) / 'GXTStudio'
    else:
        p = Path.home() / '.gxt_studio'
    p.mkdir(parents=True, exist_ok=True)
    return p


def matar_arvore(proc):
    """Encerra com segurança um processo e toda a sua árvore de subprocessos."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, creationflags=CREATE_FLAGS, timeout=10)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def abrir_no_sistema(p):
    """Abre arquivo ou pasta no programa padrão do sistema operacional."""
    p = str(p)
    if os.name == 'nt':
        os.startfile(p)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', p])
    else:
        subprocess.Popen(['xdg-open', p])


def mostrar_no_explorer(p):
    """No Windows, abre o Explorer com o arquivo selecionado."""
    p = Path(p).resolve()
    if not p.exists():
        p = p.parent
    if os.name == 'nt':
        if p.is_file():
            subprocess.Popen(['explorer', '/select,', str(p)])
        else:
            os.startfile(str(p))
    else:
        abrir_no_sistema(p.parent if p.is_file() else p)


def escrever_log(caminho_ou_nome, texto, base_dir=None):
    """Registra uma linha formatada de log com timestamp."""
    try:
        if base_dir:
            p = Path(base_dir) / caminho_ou_nome
        else:
            p = Path(caminho_ou_nome)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent.parent / 'logs' / caminho_ou_nome
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] {texto}\n")
    except Exception:
        pass
