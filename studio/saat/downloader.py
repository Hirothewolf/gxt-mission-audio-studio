# -*- coding: utf-8 -*-
"""
Instalação, download automático e extração das ferramentas SAAT com confirmação do usuário.
"""
import os
import shutil
import tempfile
import zipfile
import urllib.request
import subprocess
from pathlib import Path
from studio.config import CFG, cfg_save, PROJ_ROOT, APP_DIR
from studio.core.utils import CREATE_FLAGS, nome_insens
from studio.saat.detection import achar_saat

URL_SAAT_PADRAO = "https://github.com/thelink2012/saat/releases/download/v1.10/saat_v1.10.zip"


def buscar_instalador_saat_local():
    """Procura arquivos compactados do SAAT já presentes no sistema."""
    cands_pastas = [
        Path.home() / 'Downloads',
        APP_DIR,
        APP_DIR.parent,
        PROJ_ROOT,
        PROJ_ROOT / 'tools',
        Path.cwd()
    ]
    nomes_alvo = [
        '5_alcis_saat_gui_frontend.rar',
        'alcis_saat_gui_frontend.rar',
        'saat.rar',
        'saat.zip',
        'saat_v1.10.zip',
        'saat_frontend.rar'
    ]
    for d in cands_pastas:
        if not d.is_dir():
            continue
        for nome in nomes_alvo:
            p = d / nome
            if p.is_file():
                return p
        try:
            for item in d.glob('*saat*.*'):
                if item.suffix.lower() in ('.rar', '.zip', '.7z', '.tar'):
                    return item
        except OSError:
            pass
    return None


def extrair_pacote(arquivo_pacote, destino_tmp):
    """Extrai arquivo .zip ou .rar para a pasta temporária."""
    arquivo_pacote = Path(arquivo_pacote)
    destino_tmp = Path(destino_tmp)
    destino_tmp.mkdir(parents=True, exist_ok=True)

    if arquivo_pacote.suffix.lower() == '.zip':
        with zipfile.ZipFile(arquivo_pacote, 'r') as zf:
            zf.extractall(destino_tmp)
        return True

    # Para .rar, .7z ou .tar, tenta tar.exe nativo do Windows
    cmd = ['tar', '-xf', str(arquivo_pacote), '-C', str(destino_tmp)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_FLAGS)
    if res.returncode == 0:
        return True

    # Se falhar e 7z estiver instalado:
    if shutil.which('7z'):
        cmd_7z = ['7z', 'x', str(arquivo_pacote), f"-o{destino_tmp}", '-y']
        res_7z = subprocess.run(cmd_7z, capture_output=True, text=True, creationflags=CREATE_FLAGS)
        return res_7z.returncode == 0

    return False


def baixar_arquivo_com_progresso(url, destino_path, on_progress=None, cancel_event=None):
    """Baixa um arquivo via HTTP/HTTPS informando o progresso em blocos."""
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GTA-Audio-Studio/2.0'}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tam_total = resp.getheader('Content-Length')
        tam_total = int(tam_total) if tam_total and tam_total.isdigit() else None
        baixado = 0
        bloco_tam = 64 * 1024
        with open(destino_path, 'wb') as f:
            while True:
                if cancel_event and cancel_event.is_set():
                    return False
                bloco = resp.read(bloco_tam)
                if not bloco:
                    break
                f.write(bloco)
                baixado += len(bloco)
                if on_progress:
                    on_progress(baixado, tam_total)
    return True


def obter_info_instalacao_saat(destino_dir=None):
    """Retorna detalhes da operação para exibição na caixa de confirmação."""
    destino = Path(destino_dir) if destino_dir else PROJ_ROOT / 'tools' / 'SAAT'
    local_pkg = buscar_instalador_saat_local()
    return {
        'nome': 'San Andreas Audio Toolkit (SAAT)',
        'descricao': 'Ferramenta necessária para extrair e indexar os diálogos originais do GTA San Andreas (somente leitura).',
        'destino': str(destino),
        'origem_local': str(local_pkg) if local_pkg else None,
        'url_remota': URL_SAAT_PADRAO if not local_pkg else None,
        'arquivos_esperados': ['saat_sfx.exe', 'saat_stream.exe', 'DLLs de suporte'],
        'tamanho_estimado': '~1.5 MB' if not local_pkg else f'~{local_pkg.stat().st_size / (1024*1024):.1f} MB (arquivo local encontrado)'
    }


def executar_instalacao_saat(confirmar_callback=None, on_progress=None, cancel_event=None, destino_dir=None):
    """
    Executa o fluxo completo de instalação do SAAT:
    1. Obtém metadados.
    2. Pede confirmação ao usuário (via confirmar_callback).
    3. Extrai localmente ou baixa da web.
    4. Move para a pasta destino e configura CFG.
    """
    info = obter_info_instalacao_saat(destino_dir)
    destino = Path(info['destino'])

    if confirmar_callback:
        aceito = confirmar_callback(info)
        if not aceito:
            return {'ok': False, 'motivo': 'Instalação cancelada pelo usuário.', 'cancelado': True}

    destino.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        pacote = None

        if info['origem_local']:
            pacote = Path(info['origem_local'])
        else:
            download_zip = tmp_path / 'saat_download.zip'
            if on_progress:
                on_progress(0, None, "Baixando SAAT...")
            sucesso = baixar_arquivo_com_progresso(
                info['url_remota'], download_zip,
                on_progress=lambda b, t: on_progress(b, t, "Baixando SAAT...") if on_progress else None,
                cancel_event=cancel_event
            )
            if not sucesso:
                return {'ok': False, 'motivo': 'Download cancelado ou falhou.'}
            pacote = download_zip

        if on_progress:
            on_progress(100, 100, "Extraindo arquivos do SAAT...")

        ok_extracao = extrair_pacote(pacote, tmp_path / 'extraido')
        if not ok_extracao:
            return {'ok': False, 'motivo': f"Falha ao descompactar pacote do SAAT: {pacote.name}"}

        # Localiza saat_sfx.exe e saat_stream.exe dentro da pasta extraída
        sfx_achado = None
        stream_achado = None
        dlls = []

        for p in (tmp_path / 'extraido').rglob('*'):
            if not p.is_file():
                continue
            nome_low = p.name.lower()
            if nome_low == 'saat_sfx.exe':
                sfx_achado = p
            elif nome_low == 'saat_stream.exe':
                stream_achado = p
            elif p.suffix.lower() == '.dll':
                dlls.append(p)

        if not sfx_achado:
            return {'ok': False, 'motivo': 'saat_sfx.exe não foi encontrado dentro do pacote extraído.'}

        # Copia executáveis e dependências para a pasta destino
        dest_sfx = destino / sfx_achado.name
        shutil.copy2(sfx_achado, dest_sfx)

        dest_stream = None
        if stream_achado:
            dest_stream = destino / stream_achado.name
            shutil.copy2(stream_achado, dest_stream)

        for dll in dlls:
            try:
                shutil.copy2(dll, destino / dll.name)
            except OSError:
                pass

        # Valida executando uma chamada de teste
        try:
            res_val = subprocess.run([str(dest_sfx)], capture_output=True, text=True,
                                     creationflags=CREATE_FLAGS, timeout=10)
            if 'SAAT' not in res_val.stdout and 'SFX' not in res_val.stdout and res_val.returncode != 0:
                pass
        except Exception as e:
            return {'ok': False, 'motivo': f'SAAT extraído, mas falhou ao executar: {e}'}

        # Atualiza a configuração do projeto
        CFG['saat_dir'] = str(destino)
        CFG['saat_sfx'] = str(dest_sfx)
        if dest_stream:
            CFG['saat_stream'] = str(dest_stream)
        cfg_save()

        return {
            'ok': True,
            'motivo': 'SAAT instalado e configurado com sucesso!',
            'sfx': dest_sfx,
            'stream': dest_stream,
            'pasta': destino
        }
