# -*- coding: utf-8 -*-
"""
Janela e relatório de diagnóstico do ambiente, dependências e caminhos.
"""
import sys
import shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import studio.config as config
from studio.config import CFG, cfg_save, APP_DIR, CONFIG_PATH
from studio.core.utils import abrir_no_sistema
from studio.tts.registry import detectar_engines

try:
    import winsound
except ImportError:
    winsound = None


def abrir_diagnostico(app):
    """Exibe o relatório de diagnóstico do Studio."""
    ok = lambda b: '✓' if b else '✗'
    L = [
        'GXT Editor & Dubbing Studio — diagnóstico (%s)' % datetime.now().strftime('%d/%m/%Y %H:%M'),
        'Python %s · %s' % (sys.version.split()[0], sys.platform), ''
    ]
    L.append('%s GXT em edição: %s' % (ok(app.g), app.g.path if app.g else '—'))
    L.append('%s GXT de comparação: %s' % (ok(app.c), app.c.path if app.c else '—'))
    n = sum(1 for k in app.script_db if k[0] != 'ANY')
    L.append('%s Mapeamento SCRIPT (manifests/script_mapping_v2.jsonl): %d fala(s)' % (ok(n), n))
    project_root = config.get_project_root()
    L.append('   Raiz do projeto: %s' % project_root)
    L.append('')
    L.append('Engines de dublagem (config manual: python_<id>/script_<id> em %s):' % CONFIG_PATH.name)
    for nome, py, script in detectar_engines():
        L.append('%s %s' % (ok(py and script.is_file()), nome))
        L.append('     python: %s' % (py or 'não encontrado (.venv* no projeto ou python_<id> na config)'))
        L.append('     script: %s%s' % (script, '' if script.is_file() else '   ← não existe'))
    L.append('')

    # Modelos instalados
    from studio.tts.downloader import listar_modelos
    L.append('Modelos Neurais de IA / TTS:')
    for m in listar_modelos():
        L.append('   %s %s (%s, %s)' % (ok(m.instalado), m.nome, m.tamanho_estimado, m.categoria))
        if m.instalado:
            L.append('        local: %s' % m.caminho_local)
    L.append('')

    # SAAT
    if getattr(app, 'jogo', None) is not None:
        ex = app.jogo.saat()
        L.append('SAAT (San Andreas Audio Toolkit):')
        L.append('   %s saat_sfx: %s' % (ok(ex.get('sfx')), ex.get('sfx') or 'não configurado'))
        L.append('   %s saat_stream: %s' % (ok(ex.get('stream')), ex.get('stream') or 'não configurado'))
        L.append('')

    gravavel = False
    try:
        d = project_root / 'redub_output'
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / '.teste_escrita'
        tmp.write_text('x')
        tmp.unlink()
        gravavel = True
    except OSError:
        pass
    L.append('%s Pasta de saída gravável: %s' % (ok(gravavel), project_root / 'redub_output'))
    L.append('%s Reprodução de áudio (winsound): %s' % (ok(winsound), 'disponível' if winsound else 'indisponível'))
    L.append('%s FFmpeg no PATH: %s' % (ok(shutil.which('ffmpeg')), shutil.which('ffmpeg') or 'não encontrado (opcional)'))
    L.append('%s Marcas do usuário (favoritas/notas): %s' % (ok(True), app.ws.path))
    texto = '\n'.join(L)

    win = tk.Toplevel(app.root)
    win.title('Diagnóstico do Sistema')
    win.geometry('900x560')
    win.transient(app.root)

    t = tk.Text(win, font=('Consolas', 10), wrap='none')
    t.insert('1.0', texto)
    t.config(state='disabled')
    t.pack(fill='both', expand=True, padx=8, pady=8)

    bts = ttk.Frame(win, padding=(8, 0, 8, 8))
    bts.pack(fill='x')

    def copiar():
        anon = texto
        for real, rot in sorted(((str(project_root), '<projeto>'), (str(APP_DIR), '<app>'), (str(Path.home()), '<home>')),
                                key=lambda x: -len(x[0])):
            anon = anon.replace(real, rot)
        app._copiar(anon, 'Relatório (caminhos anonimizados)')

    def editar_cfg():
        cfg_save()
        try:
            abrir_no_sistema(CONFIG_PATH)
        except OSError as e:
            messagebox.showerror('Erro', str(e), parent=win)

    ttk.Button(bts, text='Copiar relatório', command=copiar).pack(side='left')
    ttk.Button(bts, text='Editar config (JSON)', command=editar_cfg).pack(side='left', padx=6)
    ttk.Button(bts, text='Fechar', command=win.destroy).pack(side='right')
