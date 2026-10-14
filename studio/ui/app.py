# -*- coding: utf-8 -*-
"""
Interface gráfica principal do GXT Editor & Mission Audio Studio.
"""
import os
import re
import sys
import csv
import json
import time
import shutil
import difflib
import subprocess
import threading
import queue
import uuid
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from studio.config import (
    CFG, cfg_save, PROJ_ROOT, APP_DIR, CONFIG_PATH,
    SESSION_FILE, AUDIO_INDEX_FILE, set_projeto_root
)
from studio.core.utils import (
    CREATE_FLAGS, matar_arvore, escrever_log, abrir_no_sistema,
    mostrar_no_explorer, escrever_atomico
)
from studio.gxt.codec import decode_text, encode_text, decode_text_c
from studio.gxt.jamcrc import jamcrc, recuperar_nomes, carregar_cache_nomes, salvar_cache_nomes
from studio.gxt.model import Gxt, norm_cmp, selftest
from studio.gxt.lint import lint_avisos, aplicar_fix, FIX_LABELS, TAG_RE
from studio.gxt.workspace import Workspace
from studio.audio.wav import wav_duracao, classe_delta, resolver_wav, _DUR_CACHE
from studio.audio.player import Player
from studio.audio.source_routing import is_stream_record
from studio.audio.resolver import (
    BaseAudioResolver, resolver_path_projeto,
    detectar_gta_sa_instalacoes, detectar_saat_sfx
)
from studio.script.mapping import (
    carregar_script_cache, recarregar_script_cache, consultar_script_info, ref_do_registro
)
from studio.tts.registry import ENGINES, detectar_engines, achar_python_ai, select_engine
from studio.tts.jobs import JobQueue
from studio.tts.results import import_generation_run as import_run
from studio.projects.review import ReviewStore
from studio.audio.voice_identity import VoiceIdentityStore
from studio.ui.widgets import Tooltip
from studio.ui.category_navigation import CategoryNavigationMixin
from studio.ui.dialogs import (
    ConfirmarDownloadDialog, ProgressoOperacaoDialog, GerenciadorModelosDialog
)
from studio.ui.batch_redub import abrir_popup_redublar_lote
from studio.ui.diagnostic import abrir_diagnostico
from studio.ui.start_panel import StartPanel, abrir_guia
from studio.core.sources import geometria_segura
from studio.saat.downloader import executar_instalacao_saat

# Filtros da lista de falas
FILTRO_PRED = {
    'Todas': lambda f: True,
    'Diferentes da comparação': lambda f: 'dif' in f,
    'Iguais à comparação': lambda f: 'igual' in f,
    'Editadas (não salvas)': lambda f: 'edit' in f,
    'Só no arquivo editado': lambda f: 'solo' in f,
    'Com aviso de lint': lambda f: 'lint' in f,
    'Com áudio original': lambda f: bool({'audio', 'stream_link'} & f),
    'Sem áudio original': lambda f: not ({'audio', 'stream_link'} & f),
    'Com dublagem': lambda f: 'dub' in f,
    'Sem dublagem (tem original)': lambda f: bool({'audio', 'stream_link'} & f) and 'dub' not in f,
    'Favoritas ★': lambda f: 'fav' in f,
    'Com nota': lambda f: 'note' in f,
    'Revisadas': lambda f: 'rev' in f,
    'Não revisadas': lambda f: 'rev' not in f,
    'Instalada no ModLoader': lambda f: 'inst' in f,
    'Dublada, ainda não instalada': lambda f: 'dub' in f and 'inst' not in f,
}


def _generation_failure_detail(result, engine_id=None):
    """Surface the worker's final validation error instead of hiding it in logs."""
    detail = str(getattr(result, 'error', '') or '').strip()
    logs = list(getattr(result, 'logs', []) or [])
    if getattr(result, 'returncode', None) == 0 and not detail:
        return ''
    if engine_id == 'omnivoice':
        for line in reversed(logs):
            try:
                payload = json.loads(str(line))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get('status') == 'QUARANTINED_DURATION':
                duration = payload.get('duration_seconds')
                window = payload.get('window_seconds')
                return ('A fala excedeu a janela: %.2f s produzidos para %.2f s disponíveis. '
                        'O WAV foi preservado em quarentena: %s. Ajuste speed/duration nativos; '
                        'nenhum trecho foi cortado ou alongado.' %
                        (float(duration or 0), float(window or 0), payload.get('output_path', '')))
            if payload.get('status') == 'QUARANTINED_SILENCE':
                return ('OmniVoice retornou silêncio; o WAV foi preservado em quarentena: %s.' %
                        payload.get('output_path', ''))
    if not detail:
        candidates = [str(line).strip() for line in logs
                      if str(line).strip().startswith(('ValueError:', 'RuntimeError:', 'FileNotFoundError:'))]
        detail = candidates[-1] if candidates else (str(logs[-1]).strip() if logs else '')
    if engine_id == 'omnivoice' and 'desatualizad' in detail.casefold():
        return ('A janela do GXT Editor está desatualizada para OmniVoice. Feche e reabra o editor; '
                'nenhuma referência alternativa foi usada.')
    return detail[-800:]


def _generation_user_message(detail):
    """Keep technical diagnostics in logs; explain the action in Portuguese."""
    text = str(detail or '').strip()
    lower = text.casefold()
    if 'texto ou engine' in lower or ('texto' in lower and 'receipt' in lower):
        return ('O áudio foi gerado, mas o texto usado não corresponde ao texto desta fala. '
                'Confira a frase no editor e tente gerar novamente. O WAV foi preservado.')
    if 'out of memory' in lower or 'cuda' in lower and 'memory' in lower:
        return ('A placa de vídeo ficou sem memória para gerar a fala. '
                'Feche outros programas que usam a GPU e tente novamente. Os áudios anteriores continuam salvos.')
    if 'permission' in lower or 'acesso negado' in lower:
        return 'Não foi possível acessar um arquivo. Verifique se a pasta permite gravação e tente novamente.'
    if 'referência' in lower and ('ausente' in lower or 'encontrad' in lower):
        return 'O áudio original de referência não foi encontrado. Confira a pasta do jogo e extraia novamente essa fala.'
    if 'receipt' in lower or 'checksum' in lower or 'hash do wav' in lower:
        return ('Não foi possível confirmar que este áudio pertence à fala selecionada. '
                'Gere um novo take para essa fala. O arquivo anterior foi preservado.')
    if 'runtime' in lower and ('indisponível' in lower or 'not found' in lower):
        return ('O motor de dublagem não está pronto para uso. '
                'Confira o modelo e a instalação nas configurações de dublagem e tente novamente.')
    if not text or text.startswith(('{', '[', 'Traceback')):
        return 'Não foi possível preparar o áudio para revisão. Abra o log da tarefa para consultar os detalhes.'
    for prefix in ('ValueError: ', 'RuntimeError: ', 'FileNotFoundError: '):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text[:600]


def _generation_validation_detail(validation):
    details = (validation or {}).get('details', [])
    errors = [row.get('error') for row in details if isinstance(row, dict) and row.get('error')]
    return _generation_user_message(errors[0] if errors else '')


def _intro_caption_audio_status(ainfo, original_path=None, mod_path=None, mod_name='ModLoader'):
    """Explain stream windows without pretending they are individual SCRIPT slots."""
    if not ainfo:
        return None
    from studio.audio.source_routing import audio_route
    try:
        route = audio_route(ainfo)
    except ValueError:
        route = None
    if route and route['family'] == 'streams':
        event = ainfo.get('event_name') or ainfo.get('stable_audio_id') or 'trecho'
        original = 'Original %s/%s · %s' % (route['container'], route['track'], event)
        original += (' [WAV ✓ %s]' % Path(original_path).name) if original_path else ' (trecho ainda não vinculado)'
        status = ainfo.get('stream_timing_status')
        if status == 'ALIGNMENT_CANDIDATE':
            original += ' · limite sugerido pelo alinhamento; conferir ao ouvir'
        mod = ('ModLoader (%s): [WAV ✓ %s]' % (mod_name, Path(mod_path).name)) if mod_path else (
            'ModLoader (%s): áudio em %s/%s; montagem da faixa necessária para instalar' %
            (mod_name, route['container'], route['track']))
        return original, mod
    if ainfo.get('state') != 'MAPPED_CUTSCENE_INTRO' or ref_do_registro(ainfo):
        return None
    audio_id = ainfo.get('stable_audio_id') or ainfo.get('event_name') or 'ID GXT'
    original = 'Metadados de legenda GXT (%s; sem coordenada de banco/som SCRIPT)' % audio_id
    original += (' [WAV ✓ %s]' % Path(original_path).name) if original_path else ' (sem WAV vinculado)'
    mod = ('ModLoader (%s): [WAV ✓ %s]' % (mod_name, Path(mod_path).name)) if mod_path else 'ModLoader (%s): sem referência por linha no SCRIPT' % mod_name
    return original, mod
FILTROS = list(FILTRO_PRED)

PREDICADOS = {
    'dif': lambda f: 'dif' in f,
    'edit': lambda f: 'edit' in f,
    'semdub': lambda f: bool({'audio', 'stream_link'} & f) and 'dub' not in f,
    'lint': lambda f: 'lint' in f,
    'fav': lambda f: 'fav' in f,
    'naorev': lambda f: 'rev' not in f,
}
NOMES_NAV = {
    'dif': 'diferenças', 'edit': 'editadas', 'semdub': 'sem dublagem',
    'lint': 'com aviso de lint', 'fav': 'favoritas', 'naorev': 'não revisadas'
}

ATALHOS_TEXTO = """\
ARQUIVO
  Ctrl+O            abrir GXT                 Ctrl+S        salvar
  Ctrl+P            paleta de comandos        F1            esta ajuda
EDIÇÃO
  Ctrl+Enter        aplicar texto             Ctrl+Z / Y    desfazer / refazer digitação
  Ctrl+B            favorita ★                Ctrl+Shift+M  marcar revisada
  Ctrl+Shift+N      nota da fala
BUSCA E NAVEGAÇÃO
  Ctrl+F            buscar                    Alt+↓ / Alt+↑ linha seguinte / anterior
  Ctrl+↓ / Ctrl+↑   próxima / anterior diferença
  Ctrl+Shift+↓/↑    próxima / anterior SEM dublagem
ÁUDIO
  F5                tocar original            F6            tocar dublado
  F7                original → dublado (A/B)  Shift+F7      A/B em loop
  F8                versão instalada no ModLoader   Ctrl+F7  original → dublado → instalado
  Shift+F5          fala anterior (original)  Ctrl+F5       fala seguinte (original)
  Esc               parar áudio               Espaço        (na lista) tocar dublado/original
  Duplo clique      (na lista) tocar          Botão direito menu da fala
"""


class App(CategoryNavigationMixin):
    MAX_LINHAS = 3000

    def __init__(self, root):
        self.root = root
        self.closed = False
        self.jobs = JobQueue()
        self.redub_job = None
        self.review_windows = []
        self.library_windows = []
        self.voice_windows = []
        self.native_windows = []
        self.cutscene_windows = []
        self.voice_store = VoiceIdentityStore(PROJ_ROOT)
        self.archive_busy = False
        self.archive_cancel = None
        self.review_error = None
        try:
            self.review_store = ReviewStore(PROJ_ROOT)
        except ValueError as exc:
            self.review_store = None
            self.review_error = str(exc)
        root.title('GTA San Andreas Localization & Dubbing Studio')
        root.update_idletasks()
        vw = root.winfo_vrootwidth() or root.winfo_screenwidth()
        vh = root.winfo_vrootheight() or root.winfo_screenheight()
        root.geometry(geometria_segura(CFG.get('geometry') or '1180x760', root.winfo_vrootx(),
                                       root.winfo_vrooty(), vw, vh) or '1180x760')
        root.minsize(860, 520)
        self.g = None            # arquivo em edição
        self.c = None            # arquivo de comparação
        self.nomes = {}          # cache: tabela -> {hash: nome}
        self.sel = None          # (tabela, hash) selecionado
        self.ignorando_sel = False
        self.diff_job = None
        self.audio_atual = None          # caminho do WAV original da linha selecionada
        self.audio_dublado_atual = None  # caminho do WAV dublado (Chatterbox/TTS) da linha selecionada
        self.dub_cache = {}              # cache de áudios dublados por missão
        self.script_db = carregar_script_cache()
        self.base_audio = BaseAudioResolver()
        self.original_audio_cache = {}
        self.audio_scan_thread = None
        self.player = Player(root)
        self.ui_q = queue.Queue()
        root.after(50, self._drenar_ui)
        self.ws = Workspace(PROJ_ROOT / 'workspace' / 'gxt_editor_workspace.json')
        self.cache_nomes = carregar_cache_nomes()
        self.row_info = {}            # iid -> flags da linha (dif, edit, audio, dub, lint, fav...)
        self.busca_job = None
        self.busca_token = 0
        self.busca_contexto = None
        self.busca_cache_linhas = {}
        self.busca_cache_filtro = None
        self.autoplay_job = None
        root.after(200, self._inicializar_base_audio)
        self.proc_redub = None
        self.redub_ativo = False
        self.redub_cancelado = False
        self.undo_csv = {}
        self.MAX_LINHAS = int(CFG.get('max_linhas', self.MAX_LINHAS))
        self.var_autoplay = tk.StringVar(value=CFG.get('autoplay', 'nada'))
        self.var_pausa_ab = tk.IntVar(value=int(CFG.get('pausa_ab_ms', 300)))
        self.var_engine = tk.StringVar(value=CFG.get('engine_padrao', 'auto'))
        from studio.tts.timing import initial_timing_settings
        timing_speed, timing_duration = initial_timing_settings(CFG)
        self.var_omnivoice_speed = tk.StringVar(value=str(timing_speed))
        self.var_omnivoice_duration = tk.StringVar(value=str(timing_duration))
        self.var_omnivoice_cj_reference = tk.BooleanVar(value=bool(CFG.get('omnivoice_cj_reference', False)))
        self.var_omnivoice_gpu_guard = tk.BooleanVar(value=bool(CFG.get('omnivoice_gpu_guard', True)))
        self.var_omnivoice_keep_model_loaded = tk.BooleanVar(
            value=bool(CFG.get('omnivoice_keep_model_loaded', False)))
        self.jobs.set_keep_model_loaded(self.var_omnivoice_keep_model_loaded.get())

        self.var_busca = tk.StringVar()
        self.var_todas = tk.BooleanVar(value=False)
        self.var_filtro = tk.StringVar(value='Todas')
        self.var_tags = tk.BooleanVar(value=False)
        self.jogo = None
        self.comandos_extra = []
        self.audio_instalado_atual = None

        self.montar_ui()

        # Integração SAAT / ModLoader
        try:
            from studio.ui.saat_ui import install_ui
            install_ui(self, tk, ttk, messagebox, filedialog, CFG, cfg_save, PROJ_ROOT, log=escrever_log)
        except Exception as e:
            escrever_log('app.log', 'SAAT/ModLoader indisponível: %r' % (e,))
            self.jogo = None
        self.atualizar_painel_inicio()

        root.protocol('WM_DELETE_WINDOW', self.fechar)
        self.atalhos = [
            ('<Control-s>', self.salvar), ('<Control-o>', self.abrir), ('<Control-f>', self.focar_busca),
            ('<Control-Return>', self.aplicar),
            ('<Control-Down>', lambda: self.proxima_dif(+1)), ('<Control-Up>', lambda: self.proxima_dif(-1)),
            ('<Control-Shift-Down>', lambda: self.ir_proxima('semdub', +1)),
            ('<Control-Shift-Up>', lambda: self.ir_proxima('semdub', -1)),
            ('<Alt-Down>', lambda: self.mover_linha(+1)), ('<Alt-Up>', lambda: self.mover_linha(-1)),
            ('<F5>', self.tocar_audio), ('<F6>', self.tocar_audio_dublado), ('<F7>', self.tocar_ab),
            ('<Shift-F7>', lambda: self.tocar_ab(loop=True)), ('<F8>', self.tocar_instalado), ('<Control-F7>', self.tocar_tres),
            ('<Shift-F5>', lambda: self.tocar_vizinha(-1)), ('<Control-F5>', lambda: self.tocar_vizinha(+1)),
            ('<Escape>', self.parar_audio), ('<Control-b>', self.alternar_favorito),
            ('<Control-Shift-M>', self.alternar_revisada), ('<Control-Shift-N>', self.editar_nota),
            ('<Control-p>', self.paleta_comandos), ('<F1>', self.mostrar_atalhos),
        ]
        for seq, fn in self.atalhos:
            root.bind(seq, lambda e, f=fn: (f(), 'break')[1])
        for w in (self.t_cur, self.e_busca):
            for seq, fn in self.atalhos:
                if seq.startswith(('<Control-', '<Alt-')):
                    w.bind(seq, lambda e, f=fn: (f(), 'break')[1])

    def montar_ui(self):
        menubar = tk.Menu(self.root)
        self.menubar = menubar
        self.root.config(menu=menubar)

        # Arquivo
        m_arq = tk.Menu(menubar, tearoff=0)
        m_arq.add_command(label='Abrir GXT… (Ctrl+O)', command=self.abrir)
        m_arq.add_command(label='Comparar com…', command=self.abrir_cmp)
        self.m_rec = tk.Menu(m_arq, tearoff=0, postcommand=self.montar_recentes)
        m_arq.add_cascade(label='Recentes', menu=self.m_rec)
        m_arq.add_separator()
        m_arq.add_command(label='Salvar (Ctrl+S)', command=self.salvar)
        m_arq.add_command(label='Salvar como…', command=self.salvar_como)
        m_arq.add_separator()
        m_arq.add_command(label='Exportar CSV…', command=self.exportar_csv)
        m_arq.add_command(label='Importar CSV… (com prévia)', command=self.importar_csv)
        m_arq.add_command(label='Desfazer última importação CSV', command=self.desfazer_csv)
        m_arq.add_separator()
        m_arq.add_command(label='Sair', command=self.fechar)
        menubar.add_cascade(label='Arquivo', menu=m_arq)

        # Editar
        m_ed = tk.Menu(menubar, tearoff=0)
        m_ed.add_command(label='Aplicar (Ctrl+Enter)', command=self.aplicar)
        m_ed.add_command(label='Restaurar versão salva', command=self.desfazer)
        m_ed.add_command(label='Copiar texto da comparação', command=self.copiar_cmp)
        m_ed.add_separator()
        m_ed.add_command(label='★ Favorita (Ctrl+B)', command=self.alternar_favorito)
        m_ed.add_command(label='✓ Marcar revisada (Ctrl+Shift+M)', command=self.alternar_revisada)
        m_ed.add_command(label='📝 Nota da fala… (Ctrl+Shift+N)', command=self.editar_nota)
        menubar.add_cascade(label='Editar', menu=m_ed)

        # Navegar
        m_nav = tk.Menu(menubar, tearoff=0)
        m_nav.add_command(label='Próxima diferença (Ctrl+↓)', command=lambda: self.proxima_dif(+1))
        m_nav.add_command(label='Diferença anterior (Ctrl+↑)', command=lambda: self.proxima_dif(-1))
        m_nav.add_separator()
        m_nav.add_command(label='Próxima sem dublagem (Ctrl+Shift+↓)', command=lambda: self.ir_proxima('semdub', +1))
        m_nav.add_command(label='Anterior sem dublagem (Ctrl+Shift+↑)', command=lambda: self.ir_proxima('semdub', -1))
        m_nav.add_command(label='Próxima editada', command=lambda: self.ir_proxima('edit', +1))
        m_nav.add_command(label='Próxima com aviso de lint', command=lambda: self.ir_proxima('lint', +1))
        m_nav.add_command(label='Próxima favorita', command=lambda: self.ir_proxima('fav', +1))
        m_nav.add_command(label='Próxima não revisada', command=lambda: self.ir_proxima('naorev', +1))
        m_nav.add_separator()
        m_nav.add_command(label='Linha seguinte (Alt+↓)', command=lambda: self.mover_linha(+1))
        m_nav.add_command(label='Linha anterior (Alt+↑)', command=lambda: self.mover_linha(-1))
        menubar.add_cascade(label='Navegar', menu=m_nav)

        # Áudio
        m_aud = tk.Menu(menubar, tearoff=0)
        m_aud.add_command(label='Tocar original (F5)', command=self.tocar_audio)
        m_aud.add_command(label='Tocar dublado (F6)', command=self.tocar_audio_dublado)
        m_aud.add_command(label='Original → Dublado (F7)', command=self.tocar_ab)
        m_aud.add_command(label='A/B em loop (Shift+F7)', command=lambda: self.tocar_ab(loop=True))
        m_aud.add_command(label='Fala anterior (Shift+F5)', command=lambda: self.tocar_vizinha(-1))
        m_aud.add_command(label='Fala seguinte (Ctrl+F5)', command=lambda: self.tocar_vizinha(+1))
        m_aud.add_command(label='Tocar versão instalada no ModLoader (F8)', command=self.tocar_instalado)
        m_aud.add_command(label='Original → Dublado → Instalado (Ctrl+F7)', command=self.tocar_tres)
        m_aud.add_command(label='⏹ Parar (Esc)', command=self.parar_audio)
        m_aud.add_command(label='Revisar voz, takes e efeitos…', command=self.abrir_revisao_audio)
        m_aud.add_command(label='Biblioteca de áudio (com ou sem legenda)…', command=self.abrir_biblioteca_audio)
        m_aud.add_command(label='Identificar personagens por voz…', command=self.abrir_identidade_voz)
        m_aud.add_command(label='Análise nativa: SCM, cutscenes e legendas…', command=self.abrir_fontes_jogo)
        m_aud.add_command(label='Lote automático de cutscenes…', command=self.abrir_lote_cutscenes)
        m_aud.add_command(label='Estúdio de cutscenes (manual)…', command=self.abrir_estudio_cutscenes)
        m_base = tk.Menu(m_aud, tearoff=0)
        m_base.add_command(label='Detectar instalação do GTA SA automaticamente', command=self.detectar_gta_automaticamente)
        m_base.add_command(label='Configurar pasta do GTA SA…', command=self.configurar_gta_root)
        m_base.add_command(label='Configurar raiz do projeto…', command=self.configurar_raiz_projeto)
        m_base.add_command(label='Adicionar pasta de WAVs ao AutoSearch…', command=self.adicionar_pasta_audio_base)
        m_base.add_command(label='Gerenciar pastas de busca…', command=self.gerenciar_pastas_audio_base)
        m_base.add_separator()
        m_base.add_command(label='Reindexar áudios base agora', command=self.reindexar_audios_base)
        m_base.add_command(label='Status do AutoSearch…', command=self.mostrar_status_audio_base)
        menubar.add_cascade(label='Áudio', menu=m_aud)

        # Modelos & IA (Novo menu centralizado)
        m_mod = tk.Menu(menubar, tearoff=0)
        m_mod.add_command(label='🤖 Gerenciador de Modelos de IA & SAAT…', command=self.popup_gerenciador_modelos)
        m_mod.add_separator()
        m_mod.add_command(label='🎙️ Redublar fala selecionada agora (AI)', command=self.redublar_fala_selecionada)
        m_mod.add_command(label='✓ Áudios AI: confirmar e aplicar em lote…', command=self.abrir_revisao_audio_lote)
        m_mod.add_command(label='Atualizar vínculos de faixas (BEATS / CUTSCENE)…', command=self.atualizar_vinculos_faixas)
        m_mod.add_command(label='⚡ Redublar missão em lote…', command=self.popup_redublar_lote)
        m_mod.add_command(label='↩ Trocar pela dublagem anterior da fala', command=self.restaurar_dub_anterior)
        m_mod.add_checkbutton(
            label='Manter OmniVoice carregado (reserva GPU até desativar/fechar)',
            variable=self.var_omnivoice_keep_model_loaded,
            command=self.set_omnivoice_keep_model_loaded)
        m_mod.add_separator()
        m_mod.add_command(label='📁 Abrir pasta de áudios dublados', command=self.abrir_pasta_dublados)
        m_mod.add_command(label='🔄 Recarregar lista de dublados', command=self.recarregar_audios_dublados)
        menubar.add_cascade(label='🎙️ Dublagem (IA)', menu=m_mod)

        # Opções
        m_op = tk.Menu(menubar, tearoff=0)
        m_auto = tk.Menu(m_op, tearoff=0)
        for rot, val in (('Nada', 'nada'), ('Tocar original', 'original'), ('Tocar dublado', 'dublado'), ('Original → Dublado', 'ab')):
            m_auto.add_radiobutton(label=rot, variable=self.var_autoplay, value=val, command=self.salvar_opcoes)
        m_op.add_cascade(label='Ao selecionar uma fala', menu=m_auto)
        m_pausa = tk.Menu(m_op, tearoff=0)
        for ms in (0, 150, 300, 600, 1000):
            m_pausa.add_radiobutton(label='%d ms' % ms, variable=self.var_pausa_ab, value=ms, command=self.salvar_opcoes)
        m_op.add_cascade(label='Pausa no A/B', menu=m_pausa)
        m_eng = tk.Menu(m_op, tearoff=0)
        for rot in ['auto'] + [e[0] for e in ENGINES]:
            m_eng.add_radiobutton(label=rot, variable=self.var_engine, value=rot, command=self.salvar_opcoes)
        m_op.add_cascade(label='Engine de dublagem padrão', menu=m_eng)
        m_op.add_separator()
        m_op.add_cascade(label='Pastas de áudio base (AutoSearch)', menu=m_base)
        menubar.add_cascade(label='Opções', menu=m_op)

        # Ferramentas
        m_fer = tk.Menu(menubar, tearoff=0)
        m_fer.add_command(label='🧭 Guia do projeto e fontes do jogo…', command=self.abrir_guia_projeto)
        m_fer.add_command(label='Paleta de comandos (Ctrl+P)', command=self.paleta_comandos)
        m_fer.add_command(label='Diagnóstico / detectar instalações…', command=self.diagnostico)
        m_fer.add_command(label='Atalhos de teclado (F1)', command=self.mostrar_atalhos)
        menubar.add_cascade(label='Ferramentas', menu=m_fer)

        # Barra superior de atalhos rápidos
        top = ttk.Frame(self.root, padding=(4, 4, 4, 0))
        top.pack(fill='x')
        for texto, cmd in (('Abrir GXT…', self.abrir), ('Comparar…', self.abrir_cmp),
                           ('Salvar', self.salvar), ('Salvar como…', self.salvar_como)):
            ttk.Button(top, text=texto, command=cmd).pack(side='left', padx=(0, 2))
        mb_csv = ttk.Menubutton(top, text='CSV ▾')
        m_csv = tk.Menu(mb_csv, tearoff=0)
        m_csv.add_command(label='Exportar CSV…', command=self.exportar_csv)
        m_csv.add_command(label='Importar CSV… (com prévia)', command=self.importar_csv)
        m_csv.add_command(label='Desfazer última importação CSV', command=self.desfazer_csv)
        mb_csv.configure(menu=m_csv)
        mb_csv.pack(side='left', padx=(8, 2))
        b_aud_missao = ttk.Button(top, text='🎵 Áudios da missão', command=self.extrair_audios_missao)
        b_aud_missao.pack(side='left', padx=(10, 2))
        Tooltip(b_aud_missao, 'Extrai os áudios do banco SCRIPT da missão selecionada na lista e gera o comparativo CSV.')
        b_lote = ttk.Button(top, text='🎙️ Redublar lote…', command=self.popup_redublar_lote)
        b_lote.pack(side='left', padx=2)
        Tooltip(b_lote, 'Redubla várias falas da missão selecionada de uma vez.')
        b_ai = ttk.Button(top, text='✓ Áudios AI…', command=self.abrir_revisao_audio_lote)
        b_ai.pack(side='left', padx=2)
        Tooltip(b_ai, 'Confirma áudios existentes em lote e aplica os aprovados ao mod, com backup.')
        mb_mais = ttk.Menubutton(top, text='Mais ▾')
        m_mais = tk.Menu(mb_mais, tearoff=0)
        m_mais.add_command(label='⚙️ Extrair/atualizar áudio do jogo e do mod (SAAT)…', command=self.popup_extrair_saat)
        m_mais.add_command(label='🤖 Modelos de IA & SAAT…', command=self.popup_gerenciador_modelos)
        m_mais.add_command(label='📁 Abrir pasta de áudios dublados', command=self.abrir_pasta_dublados)
        mb_mais.configure(menu=m_mais)
        mb_mais.pack(side='left', padx=2)
        b_cmd = ttk.Button(top, text='⌘ Comandos', command=self.paleta_comandos)
        b_cmd.pack(side='right')
        Tooltip(b_cmd, 'Paleta de comandos (Ctrl+P): digite para achar qualquer ação.')
        b_guia = ttk.Button(top, text='🧭 Guia', command=self.abrir_guia_projeto)
        b_guia.pack(side='right', padx=(0, 4))
        Tooltip(b_guia, 'Mostra as fontes do jogo (script, texto, áudio, cutscenes) e o que falta configurar.')

        # Barra de busca e filtros
        top2 = ttk.Frame(self.root, padding=(4, 3, 4, 2))
        top2.pack(fill='x')
        ttk.Label(top2, text='Buscar:').pack(side='left')
        self.e_busca = ttk.Entry(top2, textvariable=self.var_busca, width=20)
        self.e_busca.pack(side='left')
        ttk.Button(top2, text='✕', width=3, command=self.limpar_busca).pack(side='left', padx=(2, 0))
        self.var_busca.trace_add('write', lambda *a: self.agendar_atualizacao())
        cb_todas = ttk.Checkbutton(top2, text='em todas', variable=self.var_todas, command=self.atualizar_linhas)
        cb_todas.pack(side='left', padx=3)
        ttk.Label(top2, text='Filtro:').pack(side='left', padx=(8, 2))
        ttk.Combobox(top2, textvariable=self.var_filtro, width=24, state='readonly', values=FILTROS).pack(side='left')
        self.var_filtro.trace_add('write', lambda *a: self.atualizar_linhas())
        cb_tags = ttk.Checkbutton(top2, text='ignorar tags', variable=self.var_tags, command=self.recalcular_tudo)
        cb_tags.pack(side='left', padx=6)
        b_semdub = ttk.Button(top2, text='⏭ Sem dublagem', command=lambda: self.ir_proxima('semdub', +1))
        b_semdub.pack(side='left', padx=(10, 2))
        b_dif = ttk.Button(top2, text='⏭ Diferença', command=lambda: self.proxima_dif(+1))
        b_dif.pack(side='left')
        Tooltip(b_semdub, 'Vai para a próxima fala que tem áudio original mas ainda não tem dublagem (Ctrl+Shift+↓).')
        Tooltip(b_dif, 'Vai para a próxima fala diferente da comparação (Ctrl+↓).')
        Tooltip(self.e_busca, 'Busca no texto atual, comparação, chave, JAMCRC e notas. Atalho: Ctrl+F.')
        Tooltip(cb_todas, 'Procura em TODAS as tabelas, não só na selecionada.')
        Tooltip(cb_tags, 'Ignora tags ~x~ e espaços múltiplos na comparação.')

        self.lbl_arq = ttk.Label(self.root, text='Nenhum arquivo aberto', foreground='#555')
        self.lbl_arq.pack(fill='x', padx=6)

        pan = ttk.PanedWindow(self.root, orient='horizontal')
        pan.pack(fill='both', expand=True)
        self.pan = pan

        # Esquerda: Lista de tabelas/missões
        nav_host = ttk.Frame(pan)
        table_notebook = ttk.Notebook(nav_host)
        fl = ttk.Frame(table_notebook)
        self.tv_tab = ttk.Treeview(fl, columns=('n', 'aud', 'dif', 'ed'), selectmode='browse', height=20)
        self.tv_tab.heading('#0', text='Tabela (missão)')
        self.tv_tab.heading('n', text='Linhas')
        self.tv_tab.heading('aud', text='Áudios')
        self.tv_tab.heading('dif', text='≠ comp.')
        self.tv_tab.heading('ed', text='Editadas')
        self.tv_tab.column('#0', width=130)
        for c, w in (('n', 50), ('aud', 52), ('dif', 52), ('ed', 50)):
            self.tv_tab.column(c, width=w, anchor='e')
        sb = ttk.Scrollbar(fl, orient='vertical', command=self.tv_tab.yview)
        self.tv_tab.configure(yscrollcommand=sb.set)
        self.tv_tab.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tv_tab.bind('<<TreeviewSelect>>', lambda e: self.ao_escolher_tabela())
        self.montar_navegacao(nav_host, fl, table_notebook).pack(fill='both', expand=True)
        pan.add(nav_host, weight=0)

        # Direita: Falas e Editor
        fr = ttk.PanedWindow(pan, orient='vertical')
        pan.add(fr, weight=1)
        fe = ttk.Frame(fr)
        cols = ('ed', 'fl', 'tab', 'chave', 'audio', 'mod', 'dub', 'cmp', 'cur', 'st')
        self.tv = ttk.Treeview(fe, columns=cols, show='headings', selectmode='browse')
        for c, t, w in (('ed', '✎', 26), ('fl', '★✓📝', 46), ('tab', 'Tabela', 64), ('chave', 'Chave', 84),
                        ('audio', 'Original F5', 96), ('mod', 'Mod F8', 96),
                        ('dub', 'Projeto F6', 80),
                        ('cmp', 'Comparação (Original)', 240),
                        ('cur', 'Texto atual (Mod)', 240), ('st', 'Estado', 64)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, stretch=c in ('cmp', 'cur'))
        self.tv.tag_configure('dif', background='#fff0d6')
        self.tv.tag_configure('edit', background='#d9ecff')
        self.tv.tag_configure('solo', foreground='#888')
        self.tv.tag_configure('has_audio', foreground='#004488')
        self.tv.tag_configure('has_mod', foreground='#7a1fa2')
        self.tv.tag_configure('has_both', foreground='#005577')
        self.tv.tag_configure('has_dub', foreground='#007722')
        self.tv.tag_configure('warn', foreground='#b00020')
        for c, w in (CFG.get('colunas') or {}).items():
            if c in cols:
                try:
                    self.tv.column(c, width=int(w))
                except (ValueError, tk.TclError):
                    pass
        sb2 = ttk.Scrollbar(fe, orient='vertical', command=self.tv.yview)
        sbh = ttk.Scrollbar(fe, orient='horizontal', command=self.tv.xview)
        self.tv.configure(yscrollcommand=sb2.set, xscrollcommand=sbh.set)
        fe.columnconfigure(0, weight=1)
        fe.rowconfigure(0, weight=1)
        self.tv.grid(row=0, column=0, sticky='nsew')
        sb2.grid(row=0, column=1, sticky='ns')
        sbh.grid(row=1, column=0, sticky='ew')
        self.tv.bind('<<TreeviewSelect>>', lambda e: self.ao_escolher_linha())
        self.tv.bind('<Double-1>', lambda e: self.tocar_audio_se_foco_na_lista(e))
        self.tv.bind('<space>', lambda e: self.tocar_audio_se_foco_na_lista(e))
        self.tv.bind('<Button-3>', self.menu_contexto)
        fr.add(fe, weight=3)

        # Painel do Editor
        ed = ttk.Frame(fr, padding=4)
        fr.add(ed, weight=2)
        ed.columnconfigure(0, weight=1)
        ed.columnconfigure(1, weight=1)
        ed.rowconfigure(1, weight=1)
        ttk.Label(ed, text='Comparação (somente leitura)').grid(row=0, column=0, sticky='w')
        ttk.Label(ed, text='Texto atual (editável)').grid(row=0, column=1, sticky='w')
        self.t_cmp = tk.Text(ed, width=20, height=4, wrap='word', font=('Consolas', 11), state='disabled', bg='#f4f4f4')
        self.t_cur = tk.Text(ed, width=20, height=4, wrap='word', font=('Consolas', 11), undo=True)
        self.t_cmp.grid(row=1, column=0, sticky='nsew', padx=(0, 3))
        self.t_cur.grid(row=1, column=1, sticky='nsew')
        for w in (self.t_cmp, self.t_cur):
            w.tag_configure('diff', background='#ffd54f')
        self.t_cur.bind('<<Modified>>', self.ao_digitar)
        self.lbl_info = ttk.Label(ed, text='', foreground='#a33', wraplength=900, justify='left')
        self.lbl_info.grid(row=2, column=0, columnspan=2, sticky='w', pady=1)

        bt = ttk.Frame(ed)
        bt.grid(row=3, column=0, columnspan=2, sticky='w')
        ttk.Button(bt, text='Aplicar (Ctrl+Enter)', command=self.aplicar).pack(side='left')
        b_copiar = ttk.Button(bt, text='Copiar da comp.', command=self.copiar_cmp)
        b_copiar.pack(side='left', padx=3)
        Tooltip(b_copiar, 'Copia o texto da comparação (original) para o campo editável.')
        b_rest = ttk.Button(bt, text='Restaurar salvo', command=self.desfazer)
        b_rest.pack(side='left')
        Tooltip(b_rest, 'Volta ao texto salvo. Ctrl+Z/Y desfazem digitação.')
        b_dant = ttk.Button(bt, text='◀ Dif.', command=lambda: self.proxima_dif(-1))
        b_dant.pack(side='left', padx=(10, 2))
        Tooltip(b_dant, 'Diferença anterior (Ctrl+↑).')
        b_dprox = ttk.Button(bt, text='Dif. ▶', command=lambda: self.proxima_dif(+1))
        b_dprox.pack(side='left')
        Tooltip(b_dprox, 'Próxima diferença (Ctrl+↓).')

        bt2 = ttk.Frame(ed)
        bt2.grid(row=4, column=0, columnspan=2, sticky='w', pady=(2, 0))
        self.btn_audio = ttk.Button(bt2, text='🔊 Original', command=self.tocar_audio)
        self.btn_audio.pack(side='left')
        self.btn_audio_dub = ttk.Button(bt2, text='🎙️ Dublado', command=self.tocar_audio_dublado)
        self.btn_audio_dub.pack(side='left', padx=2)
        self.btn_ab = ttk.Button(bt2, text='A/B', command=self.tocar_ab)
        self.btn_ab.pack(side='left', padx=2)
        self.btn_audio_inst = ttk.Button(bt2, text='📦 ModLoader', command=self.tocar_instalado)
        self.btn_audio_inst.pack(side='left', padx=2)
        b_parar = ttk.Button(bt2, text='⏹ Parar', command=self.parar_audio)
        b_parar.pack(side='left', padx=2)
        Tooltip(b_parar, 'Para o áudio (Esc).')
        self.btn_redub_fala = ttk.Button(bt2, text='⚡ Redublar Fala (AI)', command=self.redublar_fala_selecionada)
        self.btn_redub_fala.pack(side='left', padx=(10, 2))
        bt3 = ttk.Frame(ed)
        bt3.grid(row=5, column=0, columnspan=2, sticky='w', pady=(2, 0))
        ttk.Button(bt3, text='Revisar voz / efeitos…', command=self.abrir_revisao_audio).pack(side='left', padx=(0, 2))
        self.btn_ajustes = ttk.Button(bt3, text='⚙ Ajustes ▸', command=self.alternar_ajustes_dub)
        self.btn_ajustes.pack(side='left', padx=2)
        self.btn_fav = ttk.Button(bt3, text='☆', width=3, command=self.alternar_favorito)
        self.btn_fav.pack(side='left', padx=(10, 2))
        Tooltip(self.btn_fav, 'Favorita (Ctrl+B).')
        self.btn_rev = ttk.Button(bt3, text='✓ Revisada', command=self.alternar_revisada)
        self.btn_rev.pack(side='left', padx=2)
        ttk.Button(bt3, text='📝 Nota', command=self.editar_nota).pack(side='left')
        Tooltip(self.btn_ajustes, 'Mostra/oculta velocidade, duração e referência do CJ usadas ao redublar.')
        self.f_ajustes = ttk.Frame(ed)
        self.f_ajustes.grid(row=6, column=0, columnspan=2, sticky='w', pady=(2, 0))
        self.f_ajustes.grid_remove()
        ttk.Label(self.f_ajustes, text='Velocidade da fala').pack(side='left')
        omni_speed = ttk.Entry(self.f_ajustes, textvariable=self.var_omnivoice_speed, width=5)
        omni_speed.pack(side='left', padx=(4, 0))
        omni_speed.bind('<FocusOut>', lambda _e: self.salvar_opcoes())
        Tooltip(omni_speed, 'Ex.: 1.0 normal, 1.15 mais rápida, 0.8 mais lenta.')
        ttk.Label(self.f_ajustes, text='Duração (original / livre / s)').pack(side='left', padx=(12, 0))
        omni_duration = ttk.Entry(self.f_ajustes, textvariable=self.var_omnivoice_duration, width=8)
        omni_duration.pack(side='left', padx=(4, 0))
        omni_duration.bind('<FocusOut>', lambda _e: self.salvar_opcoes())
        Tooltip(omni_duration, 'original: usa a duração desta fala base (2,47 s → 2,4 s); livre: ritmo natural; número: alvo manual. Sem corte ou aceleração após gerar.')
        ttk.Checkbutton(self.f_ajustes, text='Usar CJ aceito (manual)', variable=self.var_omnivoice_cj_reference,
                        command=self.salvar_opcoes).pack(side='left', padx=(12, 0))
        gpu_guard = ttk.Checkbutton(self.f_ajustes, text='Trava de GPU', variable=self.var_omnivoice_gpu_guard,
                                    command=self.salvar_opcoes)
        gpu_guard.pack(side='left', padx=(12, 0))
        Tooltip(gpu_guard, 'Recusa gerar se a GPU tiver menos de 5 GB livres ou mais de 10% de uso. '
                           'Desmarque se a leitura estiver errada (ex.: confundindo com a GPU integrada).')
        Tooltip(self.btn_audio, 'Toca o áudio original limpo do jogo extraído pelo SAAT (F5).')
        Tooltip(self.btn_audio_dub, 'Toca a fala dublada gerada pelo projeto (F6).')
        Tooltip(self.btn_ab, 'Compara alternando áudio original e dublado (F7).')
        Tooltip(self.btn_audio_inst, 'Toca o áudio ativo na pasta do mod no ModLoader (F8).')
        Tooltip(self.btn_redub_fala, 'Redubla só esta fala com o modelo TTS selecionado.')
        self.lbl_audio = ttk.Label(ed, text='', foreground='#006600')
        self.lbl_audio.grid(row=7, column=0, columnspan=2, sticky='w', pady=(2, 0))
        self.lbl_nota = ttk.Label(ed, text='', foreground='#7a5c00', wraplength=900, justify='left')
        self.lbl_nota.grid(row=8, column=0, columnspan=2, sticky='w')
        self.f_fix = ttk.Frame(ed)
        self.f_fix.grid(row=9, column=0, columnspan=2, sticky='w')

        barra = ttk.Frame(self.root)
        barra.pack(fill='x', side='bottom')
        self.lbl_st = ttk.Label(barra, text='', relief='sunken', anchor='w')
        self.lbl_st.pack(side='left', fill='x', expand=True)
        self.lbl_stats = ttk.Label(barra, text='', relief='sunken', anchor='e', padding=(8, 0))
        self.lbl_stats.pack(side='right')

        # Painel de início: ocupa a área principal enquanto nenhum GXT está aberto
        self.painel_inicio = StartPanel(self.root, self)
        self.atualizar_painel_inicio()

    def atualizar_painel_inicio(self):
        painel = getattr(self, 'painel_inicio', None)
        if painel is None:
            return
        if self.g is None:
            painel.place(in_=self.pan, x=0, y=0, relwidth=1, relheight=1)
            painel.lift()
            painel.atualizar()
        else:
            painel.place_forget()

    def abrir_guia_projeto(self):
        abrir_guia(self)

    def alternar_ajustes_dub(self):
        if self.f_ajustes.winfo_ismapped():
            self.f_ajustes.grid_remove()
            self.btn_ajustes.config(text='⚙ Ajustes ▸')
        else:
            self.f_ajustes.grid()
            self.btn_ajustes.config(text='⚙ Ajustes ▾')

    # ---------- Diálogos e Gerenciamento
    def popup_gerenciador_modelos(self):
        GerenciadorModelosDialog(self.root, on_refresh_callback=self.recarregar_audios_dublados)

    def baixar_saat_gui(self):
        manager = GerenciadorModelosDialog(self.root, on_refresh_callback=self.recarregar_audios_dublados)
        manager._disparar_download_saat()

    def popup_redublar_lote(self):
        if not self.exigir_modo_tabela():
            return
        abrir_popup_redublar_lote(self)

    def popup_extrair_saat(self):
        if hasattr(self, 'jogo_api') and 'indexar' in self.jogo_api:
            self.jogo_api['indexar']('ambos', False)
        elif hasattr(self, 'reindexar_audios_base'):
            self.reindexar_audios_base(silencioso=False)

    def popup_extrair_arquivos(self):
        from studio.ui.archive_extract import open_archive_extraction
        open_archive_extraction(self)

    def abrir_biblioteca_audio(self):
        from studio.ui.audio_library import AudioLibraryWindow
        if not self.commit():
            return
        self.library_windows = [w for w in self.library_windows if not w.closed]
        self.library_windows.append(AudioLibraryWindow(self))

    def abrir_identidade_voz(self):
        from studio.ui.voice_identity import VoiceIdentityWindow
        if not self.commit():
            return
        self.voice_windows = [w for w in self.voice_windows if not w.closed]
        if self.voice_windows:
            self.voice_windows[0].win.lift()
            return
        self.voice_windows.append(VoiceIdentityWindow(self))

    def abrir_fontes_jogo(self):
        from studio.ui.native_sources import NativeSourcesWindow
        self.native_windows = [w for w in self.native_windows if not w.closed]
        if self.native_windows:
            self.native_windows[0].win.lift()
        else:
            self.native_windows.append(NativeSourcesWindow(self))

    def atualizar_vinculos_faixas(self):
        if getattr(self, '_stream_catalog_updating', False):
            self.status('A atualização de vínculos já está em andamento.')
            return
        self._stream_catalog_updating = True
        project = Path(PROJ_ROOT)
        self.status('Indexando as faixas contínuas a partir das transcrições e alinhamentos salvos…')
        def work():
            error = None
            try:
                from studio.audio.stream_catalog import build_catalog
                _, summary = build_catalog(project)
            except (OSError, ValueError, KeyError) as exc:
                error = str(exc)
                summary = {}
            def done():
                self._stream_catalog_updating = False
                if self.closed or Path(PROJ_ROOT) != project:
                    return
                if error:
                    self.status('Não foi possível atualizar os vínculos: ' + error)
                    return
                self._recarregar_vinculos_audio()
                self.status('Vínculos atualizados: %s. As fontes do jogo foram preservadas.' % summary)
            self.ui(done)
        threading.Thread(target=work, daemon=True, name='stream-catalog-index').start()

    def abrir_estudio_cutscenes(self):
        from studio.ui.cutscene_studio import CutsceneStudioWindow
        if not self.commit():
            return
        self.cutscene_windows = [w for w in self.cutscene_windows if not w.closed]
        if self.cutscene_windows:
            self.cutscene_windows[0].win.lift()
        else:
            self.cutscene_windows.append(CutsceneStudioWindow(self))

    def abrir_lote_cutscenes(self):
        from studio.ui.cutscene_batch_ui import CutsceneBatchWindow
        if not self.commit():
            return
        self.batch_windows = [w for w in getattr(self, 'batch_windows', []) if not w.closed]
        if self.batch_windows:
            self.batch_windows[0].win.lift()
        else:
            self.batch_windows.append(CutsceneBatchWindow(self))

    def voice_identity_for(self, record):
        if not getattr(self, 'voice_store', None) or not getattr(self, 'jogo', None):
            return None
        try:
            original = self.jogo.original_para(record)
            return self.voice_store.lookup(record, original) if original else None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            escrever_log('Identificação de voz indisponível: %s' % exc)
            return None

    def refresh_voice_identities(self):
        self.atualizar_linhas()
        if self.sel:
            self._carregar_linha(*self.sel)
        for window in self.library_windows:
            if not window.closed:
                window.refresh()

    def diagnostico(self):
        abrir_diagnostico(self)

    # ---------- Áudio dublado, original e AutoSearch
    def caminhos_audio_base(self):
        roots = []
        for x in CFG.get('audio_search_roots', []) or []:
            if x and Path(x).is_dir():
                roots.append(str(Path(x).resolve()))
        # Prioridade máxima: exportações originais limpas do SAAT
        saat_base = PROJ_ROOT / '.cache' / 'saat'
        if saat_base.is_dir():
            for sub in saat_base.iterdir():
                exp = sub / 'export' / 'SCRIPT'
                if exp.is_dir():
                    roots.append(str(exp.resolve()))
                elif (sub / 'export').is_dir():
                    roots.append(str((sub / 'export').resolve()))
        for p in (
            PROJ_ROOT / 'audio_original',
            PROJ_ROOT / 'original_audio',
            PROJ_ROOT / 'audio_extracted',
            PROJ_ROOT / 'extracted_audio',
        ):
            if p.is_dir():
                roots.append(str(p.resolve()))
        gta = CFG.get('game_dir')
        if gta and (Path(gta) / 'audio' / 'SFX' / 'SCRIPT').is_dir():
            roots.append(str((Path(gta) / 'audio' / 'SFX' / 'SCRIPT').resolve()))
        return BaseAudioResolver.normalize_roots(roots)

    def caminhos_audio_events(self):
        candidatos = [PROJ_ROOT / 'data' / 'AudioEvents.txt', PROJ_ROOT / 'AudioEvents.txt']
        gta = CFG.get('game_dir')
        if gta:
            candidatos.append(Path(gta) / 'data' / 'AudioEvents.txt')
        return [p for p in candidatos if p.is_file()]

    def _inicializar_base_audio(self):
        roots = self.caminhos_audio_base()
        events = self.caminhos_audio_events()
        if not self.base_audio.load_cache(roots, events):
            self.reindexar_audios_base(silencioso=True)

    def reindexar_audios_base(self, silencioso=False):
        if self.audio_scan_thread and self.audio_scan_thread.is_alive():
            if not silencioso:
                self.status('AutoSearch já está indexando os áudios…')
            return
        roots = self.caminhos_audio_base()
        events = self.caminhos_audio_events()
        if not silencioso:
            self.status(f'Indexando áudios em {len(roots)} pasta(s) e atualizando vínculos de falas…')

        project = Path(PROJ_ROOT)

        def worker():
            summary = {}
            stream_error = None
            try:
                self.base_audio.scan(roots, events)
                erro = None
            except Exception as ex:
                erro = ex
            try:
                from studio.audio.stream_catalog import refresh_available_catalog
                summary = refresh_available_catalog(project)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                stream_error = str(exc)

            def done():
                if self.closed or Path(PROJ_ROOT) != project:
                    return
                self._recarregar_vinculos_audio()
                if erro:
                    self.status(f'Falha ao indexar áudios: {erro}')
                    if not silencioso:
                        messagebox.showerror('AutoSearch', str(erro))
                    return
                if stream_error:
                    self.status(f'WAVs e vínculos recarregados; atualização das faixas não concluída: {stream_error}')
                    return
                self.status(f'AutoSearch pronto: {len(self.base_audio.records)} WAVs; faixas: {summary or "sem alinhamentos salvos"}. Vínculos de falas recarregados.')

            self.ui(done)

        self.audio_scan_thread = threading.Thread(target=worker, daemon=True)
        self.audio_scan_thread.start()

    def _recarregar_vinculos_audio(self):
        """Publish fresh identities and invalidate every derived row/audio cache."""
        self.script_db = recarregar_script_cache()
        self.original_audio_cache.clear()
        self.nomes.clear()
        self.busca_cache_linhas.clear()
        self.busca_cache_filtro = None
        self.limpar_cache_dublado()
        if self.g:
            self.preencher_tabelas()
            self.atualizar_linhas()
            if self.sel:
                self.ao_escolher_linha()

    def mostrar_status_audio_base(self):
        roots = self.caminhos_audio_base()
        gta = CFG.get('game_dir')
        saat = detectar_saat_sfx(CFG.get('saat_sfx'), gta)
        linhas = [
            f'Raiz do projeto: {PROJ_ROOT}',
            f'GTA SA: {gta or "—"}',
            f'SAAT SFX: {saat or "—"}',
            f'Índice persistente: {AUDIO_INDEX_FILE}',
            f'WAVs indexados: {len(self.base_audio.records)}',
            f'Chaves AudioEvents: {len(self.base_audio.audio_events)}',
            f'Última indexação: {datetime.fromtimestamp(self.base_audio.last_scan).strftime("%d/%m/%Y %H:%M:%S") if self.base_audio.last_scan else "—"}',
            '', 'Pastas pesquisadas:',
        ] + [f'  • {x}' for x in roots]
        messagebox.showinfo('AutoSearch de Áudio Original', '\n'.join(linhas))

    def configurar_raiz_projeto(self):
        escolhido = filedialog.askdirectory(title='Selecione a raiz do projeto de dublagem', initialdir=str(PROJ_ROOT))
        if not escolhido:
            return
        if self.redub_ativo or getattr(self, 'batch_ativo', False) or self.jobs.busy or self.archive_busy or any(w.busy for w in self.library_windows) or (self.audio_scan_thread and self.audio_scan_thread.is_alive()) or any(w.busy for w in self.review_windows):
            messagebox.showinfo('Projeto em uso', 'Aguarde ou cancele as tarefas antes de trocar de projeto.')
            return
        for window in self.review_windows:
            if not window.closed:
                window.close()
        for window in self.library_windows:
            window.close()
        for window in self.voice_windows:
            window.close()
        for window in self.native_windows:
            window.close()
        for window in self.cutscene_windows:
            window.close()
        self.ws.salvar()
        set_projeto_root(escolhido)
        self.voice_store = VoiceIdentityStore(PROJ_ROOT)
        self.original_audio_cache.clear()
        self.base_audio.clear()
        self.ws = Workspace(PROJ_ROOT / 'workspace' / 'gxt_editor_workspace.json')
        try:
            self.review_store = ReviewStore(PROJ_ROOT)
            self.review_error = None
        except ValueError as exc:
            self.review_store = None
            self.review_error = str(exc)
        if getattr(self, 'jogo', None):
            from studio.saat.audio import GameAudio
            replacement = GameAudio(CFG, cfg_save, PROJ_ROOT / '.cache' / 'saat', PROJ_ROOT / 'backups' / 'modloader', PROJ_ROOT / 'workspace' / 'modloader_manifest.json', log=escrever_log)
            self.jogo.__dict__.update(replacement.__dict__)
        self.script_db = carregar_script_cache()
        self.limpar_cache_dublado()
        self.reindexar_audios_base(silencioso=False)
        self.status(f'Raiz do projeto configurada: {escolhido}')

    def configurar_gta_root(self):
        inicial = CFG.get('game_dir') or str(PROJ_ROOT)
        escolhido = filedialog.askdirectory(title='Pasta do GTA San Andreas', initialdir=inicial)
        if not escolhido:
            return False
        g = Path(escolhido)
        CFG['game_dir'] = str(g)
        saat = detectar_saat_sfx(CFG.get('saat_sfx'), g)
        if saat:
            CFG['saat_sfx'] = str(saat)
        cfg_save()
        self.reindexar_audios_base(silencioso=True)
        self.status(f'GTA SA configurado: {g}')
        return True

    def detectar_gta_automaticamente(self, silencioso=False):
        achados = detectar_gta_sa_instalacoes()
        if not achados:
            if not silencioso:
                messagebox.showinfo('Detectar GTA SA', 'Nenhuma instalação encontrada nos locais padrão/Steam.')
            return False
        escolhido = achados[0]
        CFG['game_dir'] = str(escolhido)
        saat = detectar_saat_sfx(CFG.get('saat_sfx'), escolhido)
        if saat:
            CFG['saat_sfx'] = str(saat)
        cfg_save()
        self.reindexar_audios_base(silencioso=True)
        self.status(f'GTA SA detectado: {escolhido}')
        if not silencioso:
            messagebox.showinfo('Detectar GTA SA', f'GTA San Andreas detectado em:\n{escolhido}')
        return True

    def adicionar_pasta_audio_base(self):
        escolhido = filedialog.askdirectory(title='Pasta com WAVs originais extraídos', initialdir=str(PROJ_ROOT))
        if not escolhido:
            return
        p = str(Path(escolhido).resolve())
        roots = list(CFG.get('audio_search_roots') or [])
        if os.path.normcase(p) not in {os.path.normcase(x) for x in roots}:
            roots.append(p)
        CFG['audio_search_roots'] = roots
        cfg_save()
        self.reindexar_audios_base(silencioso=False)

    def gerenciar_pastas_audio_base(self):
        win = tk.Toplevel(self.root)
        win.title('Pastas do AutoSearch')
        win.geometry('720x380')
        win.transient(self.root)
        ttk.Label(win, text='Pastas adicionais para busca de arquivos WAV:').pack(anchor='w', padx=10, pady=(10, 4))
        lb = tk.Listbox(win, selectmode='extended')
        lb.pack(fill='both', expand=True, padx=10, pady=4)

        def refresh():
            lb.delete(0, 'end')
            for x in CFG.get('audio_search_roots', []) or []:
                lb.insert('end', x)

        def add():
            escolhido = filedialog.askdirectory(parent=win, title='Adicionar pasta de áudio')
            if not escolhido:
                return
            roots = list(CFG.get('audio_search_roots', []) or [])
            p = str(Path(escolhido).resolve())
            if os.path.normcase(p) not in {os.path.normcase(str(x)) for x in roots}:
                roots.append(p)
            CFG['audio_search_roots'] = roots
            refresh()

        def remove():
            idx = set(lb.curselection())
            roots = list(CFG.get('audio_search_roots', []) or [])
            CFG['audio_search_roots'] = [x for i, x in enumerate(roots) if i not in idx]
            refresh()

        def save_close():
            cfg_save()
            win.destroy()
            self.reindexar_audios_base(silencioso=False)

        b = ttk.Frame(win)
        b.pack(fill='x', padx=10, pady=10)
        ttk.Button(b, text='Adicionar…', command=add).pack(side='left')
        ttk.Button(b, text='Remover selecionadas', command=remove).pack(side='left', padx=6)
        ttk.Button(b, text='Salvar e Reindexar', command=save_close).pack(side='right')
        refresh()

    def carregar_cache_dublado(self, tab):
        if not tab or tab == '*':
            return {}
        if tab in self.dub_cache:
            return self.dub_cache[tab]
        cache = {}
        candidatos = [
            PROJ_ROOT / 'redub_output' / 'qwen' / tab,
            PROJ_ROOT / 'redub_output' / tab,
            PROJ_ROOT / 'redub_output' / 'chatterbox' / tab,
            PROJ_ROOT / 'missoes_extraidas' / tab,
        ]
        for pasta_missao in candidatos:
            if not pasta_missao.is_dir():
                continue
            json_path = pasta_missao / 'redub_relatorio.json'
            if json_path.is_file():
                try:
                    relatorio = json.loads(json_path.read_text(encoding='utf-8'))
                    linhas = relatorio if isinstance(relatorio, list) else relatorio.get('linhas', [])
                    for linha in linhas:
                        wname = linha.get('wav_gerado') or linha.get('arquivo_audio')
                        if not wname:
                            continue
                        wpath = pasta_missao / wname
                        if not wpath.is_file():
                            continue
                        if linha.get('jamcrc'):
                            try:
                                h = int(linha['jamcrc'], 16)
                                cache[(tab, h)] = wpath
                                cache[h] = wpath
                            except ValueError:
                                pass
                        evento = linha.get('event_name') or linha.get('evento')
                        if evento:
                            ev_up = evento.upper()
                            cache[(tab, ev_up)] = wpath
                            cache[ev_up] = wpath
                except Exception:
                    pass
            for f in pasta_missao.glob('*.wav'):
                nome = f.stem.upper()
                if nome.startswith('_'):
                    continue
                cache.setdefault((tab, nome), f)
                cache.setdefault(nome, f)

                # Remove prefixo numérico sequencial (ex: "001_INT1_AA" -> "INT1_AA")
                limpo = re.sub(r'^\d+[ _.-]+', '', nome)
                cache.setdefault((tab, limpo), f)
                cache.setdefault(limpo, f)

                # Remove sufixos como "_DUB", "_ORIG", "_TEST", "_FINAL"
                sem_suf = re.sub(r'_(?:DUB|ORIG|TEST|FINAL)$', '', limpo)
                if sem_suf != limpo:
                    cache.setdefault((tab, sem_suf), f)
                    cache.setdefault(sem_suf, f)

                # Se for CRC em hex (8 caracteres)
                for s in (nome, limpo, sem_suf):
                    if len(s) == 8:
                        try:
                            h_val = int(s, 16)
                            cache.setdefault((tab, h_val), f)
                            cache.setdefault(h_val, f)
                        except ValueError:
                            pass
        self.dub_cache[tab] = cache
        return cache

    def achar_audio_dublado(self, tab, h, ev=None):
        if not tab or tab == '*':
            return None
        if self.review_store and self.g and tab in self.g.t and h in self.g.t[tab]['cur']:
            approved = self.review_store.approved_path('%s|%08x' % (tab, h), decode_text_c(self.g.t[tab]['cur'][h]))
            if approved:
                return approved
        c = self.carregar_cache_dublado(tab)
        if (tab, h) in c:
            return c[(tab, h)]
        if h in c:
            return c[h]
        if ev:
            ev_u = str(ev).strip().upper()
            if (tab, ev_u) in c:
                return c[(tab, ev_u)]
            if ev_u in c:
                return c[ev_u]
            limpo = re.sub(r'^\d+[ _.-]+', '', ev_u)
            if (tab, limpo) in c:
                return c[(tab, limpo)]
            if limpo in c:
                return c[limpo]
        return None

    def limpar_cache_dublado(self, tab=None):
        if tab:
            self.dub_cache.pop(tab, None)
        else:
            self.dub_cache.clear()

    def resolver_audio_original(self, tab, h, ainfo=None, chave=None):
        ck = (tab, int(h)) if h is not None else (tab, chave)
        if ck in self.original_audio_cache:
            return self.original_audio_cache[ck]
        if chave is None:
            chave = self.nomes.get(tab, {}).get(h)
        ainfo = ainfo or self.info_script(tab, h, chave)
        if is_stream_record(ainfo):
            from studio.tts.stream_reference import resolve_stream_reference
            try:
                reference = resolve_stream_reference(ainfo, PROJ_ROOT)
                res = (Path(reference.get('audition_path', reference['path'])), 'Trecho original %s/%s (%s)' % (reference['provenance']['stream_container'], reference['provenance']['stream_track'], reference.get('timing_status', 'alinhado')), 100)
                self.original_audio_cache[ck] = res
                return res
            except (OSError, ValueError, KeyError):
                # Unresolved stream dialogue must not fall back to an unrelated SCRIPT voice.
                return (None, 'Faixa contínua: intervalo de fala ainda não vinculado', 0)
        if getattr(self, 'jogo', None) is not None and ainfo:
            p = self.jogo.original_para(ainfo)
            if p and Path(p).is_file():
                res = (p, 'SAAT (áudio original do jogo)', 100)
                self.original_audio_cache[ck] = res
                return res
        result = self.base_audio.resolve(tab, h, chave, ainfo)
        self.original_audio_cache[ck] = result
        return result

    def resolver_audio_mod(self, tab, h, ainfo=None, chave=None):
        jogo = getattr(self, 'jogo', None)
        if jogo is None:
            return None
        if chave is None:
            chave = self.nomes.get(tab, {}).get(h)
        ainfo = ainfo or self.info_script(tab, h, chave)
        if is_stream_record(ainfo):
            try:
                from studio.audio.stream_install import installed_stream_path
                return installed_stream_path(ainfo, PROJ_ROOT, jogo)
            except (OSError, ValueError, KeyError):
                return None
        if ainfo:
            p = jogo.instalado_para(ainfo)
            if p and Path(p).is_file():
                return p
        return None

    def wav_original(self, ainfo=None, tab=None, h=None, chave=None):
        if tab is None and h is None and self.sel:
            tab, h = self.sel
        res = self.resolver_audio_original(tab, h, ainfo=ainfo, chave=chave)
        return res[0] if res and res[0] else None

    def tocar_audio_se_foco_na_lista(self, event=None):
        if self.root.focus_get() == self.tv:
            if self.audio_dublado_atual:
                self.tocar_audio_dublado()
            elif self.audio_atual:
                self.tocar_audio()

    def _tocar(self, path, rotulo):
        if not path or not Path(path).is_file():
            self.status(f'Áudio {rotulo} não disponível para esta fala.')
            return
        if not self.player.disponivel:
            self.status('Reprodução de áudio não suportada neste sistema.')
            return
        self.player.play(path)
        d = wav_duracao(path)
        self.status('▶ %s (%s, %.2fs)' % (rotulo, Path(path).name, d or 0.0))

    def tocar_audio(self):
        self._tocar(self.audio_atual, 'original')

    def tocar_audio_dublado(self):
        self._tocar(self.audio_dublado_atual, 'dublado')

    def tocar_ab(self, loop=False):
        p1, p2 = self.audio_atual, self.audio_dublado_atual
        if not p1 and not p2:
            self.status('Nenhum áudio (original ou dublado) disponível para esta fala.')
            return
        if not (p1 and p2):
            self.status('A/B necessita de ambos os áudios (tocando o único disponível: %s)' % ('Original' if p1 else 'Dublado'))
            self._tocar(p1 or p2, 'único disponível')
            return
        self.player.play_sequence(
            [p1, p2],
            pausa_ms=self.var_pausa_ab.get(),
            loop=loop,
            on_step=lambda idx, f: self.status('▶ A/B: %s (%s)' % ('Original' if idx == 0 else 'Dublado', Path(f).name)),
            on_done=lambda: self.status('✓ Comparação A/B concluída. F7 para repetir.')
        )
        self.status('▶ A/B: original → dublado%s' % (' (loop — Esc para parar)' if loop else ''))

    def abrir_pasta_sistema(self, p):
        abrir_no_sistema(p)

    def tocar_instalado(self):
        self._tocar(self.audio_instalado_atual, 'instalado no ModLoader')

    def tocar_tres(self):
        p1, p2, p3 = self.audio_atual, self.audio_dublado_atual, self.audio_instalado_atual
        seq = [p for p in (p1, p2, p3) if p]
        if not seq:
            self.status('Nenhum áudio disponível.')
            return
        nomes = ['original' if p == p1 else 'dublado' if p == p2 else 'instalado' for p in seq]
        self.player.play_sequence(seq, pausa_ms=self.var_pausa_ab.get(), loop=False)
        self.status('▶ Sequência: ' + ' → '.join(nomes))

    def parar_audio(self):
        self.player.stop()
        self.status('⏹ Áudio parado.')

    def tocar_vizinha(self, passo):
        if not self.sel or not self.g:
            return
        tab, h = self.sel
        hashes = self.g.t[tab]['hashes']
        try:
            i = hashes.index(h) + passo
        except ValueError:
            return
        if 0 <= i < len(hashes):
            self.selecionar_iid('%s|%08x' % (tab, hashes[i]))
            self.root.after(80, self.tocar_audio)

    def abrir_pasta_dublados(self):
        if not self.exigir_modo_tabela():
            return
        tab = self.tabela_atual()
        if tab and tab != '*':
            pasta = PROJ_ROOT / 'redub_output' / 'qwen' / tab
            if not pasta.is_dir():
                pasta = PROJ_ROOT / 'redub_output' / tab
        else:
            pasta = PROJ_ROOT / 'redub_output'
        pasta.mkdir(parents=True, exist_ok=True)
        abrir_no_sistema(pasta)

    def recarregar_audios_dublados(self):
        self.limpar_cache_dublado()
        self.atualizar_linhas()
        if self.sel:
            self.ao_escolher_linha()
        self.status('Cache de áudios dublados recarregado.')

    def guardar_historico_dub(self, wav):
        if not wav or not Path(wav).is_file():
            return
        p = Path(wav)
        hist_dir = p.parent / '_historico'
        try:
            hist_dir.mkdir(parents=True, exist_ok=True)
            carimbo = datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(p, hist_dir / ('%s_%s.wav' % (p.stem, carimbo)))
        except OSError:
            pass

    def restaurar_dub_anterior(self):
        if not self.audio_dublado_atual:
            messagebox.showinfo('Dublagem anterior', 'Nenhum áudio dublado na fala selecionada.')
            return
        p = Path(self.audio_dublado_atual)
        hist_dir = p.parent / '_historico'
        cands = sorted(hist_dir.glob('%s_*.wav' % p.stem)) if hist_dir.is_dir() else []
        if not cands:
            messagebox.showinfo('Dublagem anterior', 'Nenhum take anterior encontrado em _historico/ para esta fala.')
            return
        ultimo = cands[-1]
        if messagebox.askyesno('Trocar take', 'Substituir a dublagem atual por:\n%s\n(salvo em %s)?'
                               % (ultimo.name, datetime.fromtimestamp(ultimo.stat().st_mtime).strftime('%d/%m %H:%M'))):
            self.guardar_historico_dub(p)
            shutil.copy2(ultimo, p)
            self.recarregar_audios_dublados()
            self.status('Take anterior restaurado: %s' % ultimo.name)
            self.tocar_audio_dublado()

    def abrir_revisao_audio_lote(self):
        if self.g and not self.commit():
            return
        from studio.ui.bulk_audio_review import open_bulk_audio_review
        return open_bulk_audio_review(self)

    def abrir_revisao_audio(self):
        if not self.sel or not self.g:
            self.status('Selecione uma fala para revisar voz e áudio.')
            return
        if not self.review_store:
            messagebox.showerror('Dados de revisão', self.review_error or 'Revisão indisponível.')
            return
        if not self.commit():
            return
        from studio.ui.audio_review import AudioReviewWindow
        self.review_windows = [w for w in self.review_windows if not w.closed]
        self.review_windows.append(AudioReviewWindow(self))

    def _prepare_generation(self, mission, selected_hash=None):
        if not self.g or mission not in self.g.t:
            raise ValueError('Abra o GXT que contém a missão antes de gerar.')
        if self.jobs.busy:
            raise RuntimeError('Outra geração está em andamento; aguarde ou cancele.')
        if not self.commit():
            raise ValueError('A edição não foi aplicada.')
        engine = select_engine(root=PROJ_ROOT)
        run = PROJ_ROOT / 'workspace' / 'studio_runs' / uuid.uuid4().hex
        data = self.g.build()
        self.g.validar(data)
        escrever_atomico(run / 'american.gxt', data)
        hashes = [selected_hash] if selected_hash is not None else self.g.t[mission]['hashes']
        texts = {h: decode_text_c(self.g.t[mission]['cur'][h]) for h in hashes}
        return engine, run, texts

    def import_generation_run(self, run_output, mission, texts, engine_name):
        if not self.review_store:
            raise ValueError(self.review_error or 'Dados de revisão indisponíveis.')
        durations = {h: (self.info_script(mission, h) or {}).get('duration_seconds') for h in texts}
        return import_run(self.review_store, run_output, mission, texts, engine_name, durations)

    def cancelar_redub(self):
        self.redub_cancelado = True
        if self.redub_job:
            self.redub_job.cancel()

    def _fim_redub(self):
        self.redub_ativo = False
        self.redub_job = None
        self.proc_redub = None
        if not self.closed:
            self.btn_redub_fala.config(text='⚡ Redublar Fala (AI)')

    def _omnivoice_command_for_selection(self, engine, run, tab, h, event, target_text, settings=None, announce=True):
        """Prepare one provenance-pinned OmniVoice take for the selected GXT line."""
        settings = settings or {}
        from studio.tts.omnivoice import (
            build_command as build_omnivoice_command,
            load_accepted_cj_reference, resolve_original_reference, sha256, spoken_text,
        )
        record = self.info_script(tab, h, event)
        if not record:
            raise ValueError('OmniVoice requer um vínculo GXT→PAK/banco/slot original verificado.')
        from studio.audio.library_context import base_gxt_path, base_texts
        base_path = base_gxt_path(self)
        english_full = base_texts(self).get((tab, h), '')
        english = spoken_text(english_full)
        if not english:
            raise ValueError('A chave não existe no GXT original do jogo; não use comparação/tradução como transcript.')
        use_cj = bool(settings.get('use_cj_reference', self.var_omnivoice_cj_reference.get()))
        if use_cj:
            speaker = str(record.get('speaker_identity') or '').strip().casefold()
            if record.get('speaker_identity_status') != 'MANUAL' or speaker not in {'cj', 'carl', 'carl johnson'}:
                raise ValueError('A referência CJ aceita só pode ser usada quando CJ/Carl foi confirmado manualmente nesta fala.')
            reference = load_accepted_cj_reference(
                PROJ_ROOT, target_identity='CJ', identity_confirmed=True)
            ref_path, ref_text = reference['path'], reference['reference_text']
            provenance = dict(reference['provenance'])
            provenance.update(source_kind=reference['reference_kind'],
                              source_path=reference['provenance']['reference_source_path'],
                              source_sha256=reference['provenance']['reference_source_sha256'],
                              reference_sha256=reference['sha256'],
                              reference_audio_id=reference['provenance']['utterance_id'],
                              reference_transcript_authority='ACCEPTED_CUTSCENE_BLIND_ASR',
                              reference_asr_verification='ACCEPTED_EXPERIMENT',
                              target_speaker_identity=record.get('speaker_identity'),
                              target_identity_status='MANUAL')
            seed = int(reference.get('seed', 2026100871))
        elif is_stream_record(record):
            from studio.tts.stream_reference import resolve_stream_reference
            reference = resolve_stream_reference(record, PROJ_ROOT, english_full)
            ref_path, ref_text = reference['path'], reference['reference_text']
            provenance = dict(reference['provenance'])
            provenance.update(gxt_source_path=str(base_path),
                              gxt_source_sha256=sha256(base_path),
                              target_speaker_identity=record.get('speaker_identity'),
                              target_identity_status=record.get('speaker_identity_status', 'UNKNOWN'))
            seed = int(settings.get('seed', CFG.get('omnivoice_seed', 20261009)))
        else:
            reference = resolve_original_reference(record, self.jogo, PROJ_ROOT)
            ref_path, ref_text = reference['path'], english
            provenance = dict(reference)
            provenance.update(source_path=reference['path'], source_sha256=reference['sha256'],
                              source_text_en=english, target_speaker_identity=record.get('speaker_identity'),
                              target_identity_status=record.get('speaker_identity_status', 'UNKNOWN'),
                              reference_transcript_authority='NATIVE_GXT_CAPTION_NOT_BLIND_ASR',
                              reference_asr_verification='PENDING')
            seed = int(settings.get('seed', CFG.get('omnivoice_seed', 20261009)))
        # Timing belongs to this target's original utterance, including when
        # the manual CJ option chooses a different voice-cloning reference.
        if is_stream_record(record):
            from studio.tts.stream_reference import resolve_stream_reference
            timing_reference = (reference if provenance.get('source_kind') == 'verified_native_cutscene_window'
                                else resolve_stream_reference(record, PROJ_ROOT, english_full))
            raw_duration = timing_reference.get('duration_seconds')
        else:
            timing_reference = (reference if provenance.get('source_kind') == 'original_game_saat_cache'
                                else resolve_original_reference(record, self.jogo, PROJ_ROOT))
            raw_duration = wav_duracao(timing_reference['path'])
        if raw_duration is None:
            raise ValueError('Duração do áudio original indisponível; reindexe a fala do jogo base.')
        try:
            speed = float(str(settings.get('speed', self.var_omnivoice_speed.get())).replace(',', '.'))
            duration = str(settings.get('duration', self.var_omnivoice_duration.get())).strip()
            window = float(raw_duration)
        except (TypeError, ValueError):
            raise ValueError('Velocidade e duração do áudio original precisam ser válidas.')
        from studio.audio.source_routing import audio_route, output_directory
        route = audio_route(record)
        output = output_directory(run, route, tab) / ('%08X.wav' % h)
        provenance['audio_route'] = route
        editor_gxt = getattr(self, 'g', None)
        target_gxt_path = getattr(editor_gxt, 'path', None)
        provenance.update(target_stable_id=record.get('stable_audio_id'),
                          target_text_source='current_GXT_editor_text',
                          timing_source='exact_original_game_utterance_duration',
                          timing_reference_path=timing_reference['path'],
                          timing_window_seconds=window,
                          base_english_gxt_path=str(base_path) if base_path else None,
                          base_english_gxt_sha256=sha256(base_path) if base_path else None,
                          base_english_text=english_full,
                          base_english_gxt_id='%s:%08X' % (tab, h),
                          target_gxt_path=str(target_gxt_path) if target_gxt_path else None,
                          target_gxt_sha256=sha256(target_gxt_path) if target_gxt_path and Path(target_gxt_path).is_file() else None)
        details = ('OmniVoice | ref=%s | ID=%s | EN: %s | voz=%s (%s) | PT: %s' % (
            ref_path, provenance.get('reference_audio_id') or provenance.get('coordinates'),
            ref_text, record.get('speaker_identity') or 'não confirmada',
            record.get('speaker_identity_status', 'UNKNOWN'), target_text))
        from studio.tts.timing import resolve_duration
        duration_hint = resolve_duration(duration, window)
        details += ' | original %.2fs → alvo %s | velocidade %.2f' % (window, ('%.2fs' % duration_hint) if duration_hint is not None else 'livre', speed)
        label = getattr(self, 'lbl_audio', None) if announce else None  # lote roda fora da thread de UI
        if label:
            label.config(text=details, foreground='#305080')
        return build_omnivoice_command(
            PROJ_ROOT, ref_path, ref_text, target_text, output,
            provenance=provenance, speed=speed, duration=duration,
            window_seconds=window, python_path=engine.python, script_path=engine.script,
            mission=tab, jamcrc='%08X' % h, seed=seed)

    def redublar_fala_selecionada(self):
        if self.redub_ativo:
            self.cancelar_redub()
            return
        if not self.sel:
            self.status('Selecione uma fala para redublar.')
            return
        if not self.review_store:
            messagebox.showerror('Revisão indisponível', self.review_error or 'Dados de revisão inválidos.')
            return
        tab, h = self.sel
        try:
            engine, run, texts = self._prepare_generation(tab, h)
            ev = self.nomes_tabela(tab, forcar=True).get(h)
            if getattr(engine, 'id', None) == 'omnivoice':
                cmd = self._omnivoice_command_for_selection(engine, run, tab, h, ev, texts[h])
            else:
                cmd = engine.build_command(tab, run / 'american.gxt', run / 'output', jamcrc='%08X' % h, event=ev)
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror('Preparar dublagem', _generation_user_message(exc))
            return
        self.redub_ativo, self.redub_cancelado = True, False
        self.btn_redub_fala.config(text='⏹ Cancelar dublagem', state='normal')
        # The task dialog owns a compact progress bar and optional log details.
        progress = ProgressoOperacaoDialog(self.root, titulo='Gerando novo take — %s' % engine.name)
        progress.cancel_event = threading.Event()
        def cancel_task():
            progress._cancelar()
            self.cancelar_redub()
        progress.btn_cancelar.config(command=cancel_task)
        progress.win.protocol('WM_DELETE_WINDOW', cancel_task)
        def line(message):
            escrever_log('tts.log', message, base_dir=run)
            self.ui(lambda m=message: progress.atualizar(0, 0, m) if not progress.cancel_event.is_set() else None)
        try:
            self.redub_job = self.jobs.submit_exclusive(cmd, cwd=PROJ_ROOT, on_line=line,
                                                        timeout=float(CFG.get('tts_timeout_seconds', 600)))
        except RuntimeError as exc:
            progress.fechar()
            self._fim_redub()
            messagebox.showerror('Geração em andamento', str(exc))
            return
        job = self.redub_job
        self.status('Gerando take com o texto atual; áudios anteriores preservados.')
        def wait_for_job():
            result = job.wait()
            validation = None
            error = _generation_failure_detail(result, getattr(engine, 'id', None))
            if not self.closed and result.returncode == 0 and not result.cancelled and not result.timed_out:
                try:
                    from studio.audio.source_routing import command_output_directory
                    output_root = command_output_directory(cmd, run / 'output' / tab)
                    validation = self.import_generation_run(output_root, tab, texts, engine.name)
                except Exception as exc:
                    error = str(exc)
            def finish():
                if self.closed:
                    return
                progress.fechar()
                self._fim_redub()
                if result.cancelled or self.redub_cancelado:
                    self.status('Dublagem cancelada; áudios anteriores preservados.')
                elif result.timed_out:
                    self.status('Tempo máximo da geração excedido. Consulte o log da tarefa.')
                elif validation and validation['valid']:
                    self.status('%d novo take para ouvir e aprovar; %d em quarentena; %d ausente(s).' % (
                        validation['valid'], validation['quarantined'], validation['missing']))
                    if self.sel == (tab, h):
                        self.abrir_revisao_audio()
                else:
                    detail = (_generation_validation_detail(validation) if validation else _generation_user_message(error))
                    messagebox.showerror('Não foi possível revisar o áudio', '%s\n\nSe precisar dos detalhes, consulte tts.log e studio_validation.json nesta pasta:\n%s' % (detail, run))
                    self.status('Geração não validada; saída preservada para diagnóstico.')
            self.ui(finish)
        threading.Thread(target=wait_for_job, daemon=True).start()

    def extrair_audios_missao(self):
        if not self.exigir_modo_tabela():
            return
        tab = self.tabela_atual()
        if not tab or tab == '*':
            messagebox.showinfo('Extrair Áudios', 'Selecione uma missão específica na lista à esquerda.')
            return
        if not self.g:
            messagebox.showwarning('Aviso', 'Abra um arquivo GXT primeiro.')
            return

        out_dir = PROJ_ROOT / 'missoes_extraidas' / tab / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
        out_dir.mkdir(parents=True, exist_ok=True)

        t = self.g.t[tab]
        nomes = self.nomes_tabela(tab, forcar=True)
        linhas_csv = []
        copiados = 0

        for h in t['hashes']:
            chave = nomes.get(h, '0x%08X' % h)
            ainfo = self.info_script(tab, h, chave)
            ev = (ainfo.get('event_name') if ainfo else None) or chave
            aid = (ainfo.get('stable_audio_id') if ainfo else None) or chave
            wav_src = self.wav_original(ainfo=ainfo, tab=tab, h=h, chave=chave) or Path('')
            dur = (ainfo.get('duration_seconds', 0) if ainfo else 0) or (wav_duracao(wav_src) if (wav_src and Path(wav_src).is_file()) else 0)

            if not ainfo and not (wav_src and Path(wav_src).is_file()):
                continue

            from studio.audio.source_routing import audio_route, output_directory
            try:
                route = audio_route(ainfo)
            except ValueError:
                route = None
            routed = output_directory(out_dir, route, tab).relative_to(out_dir / 'output')
            wav_dest = out_dir / routed / ('%08X.wav' % h)
            wav_dest_name = wav_dest.relative_to(out_dir).as_posix()

            if wav_src and Path(wav_src).is_file():
                wav_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wav_src, wav_dest)
                copiados += 1
            else:
                wav_dest_name = ''

            r = self.ref(tab, h)
            txt_cmp = decode_text(r) if r is not None else ''
            txt_cur = decode_text(t['cur'][h])

            linhas_csv.append({
                'seq': copiados, 'missao': tab, 'evento': ev,
                'audio_id': aid, 'jamcrc': '%08X' % h, 'duracao_seg': '%.3f' % dur,
                'texto_original_en': txt_cmp, 'texto_mod_ptbr': txt_cur,
                'arquivo_audio': wav_dest_name,
                'tipo_audio': route['family'] if route else 'desconhecido',
                'arquivo_origem': route['container'] if route else '',
                'destino_nativo': route['destination'] if route else ''
            })

        if not copiados and not linhas_csv:
            messagebox.showinfo('Extrair Áudios', f'Nenhum áudio vinculado foi encontrado para a tabela {tab}.')
            return

        csv_file = out_dir / 'dialogos.csv'
        with open(csv_file, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=[
                'seq', 'missao', 'evento', 'audio_id', 'jamcrc', 'duracao_seg',
                'texto_original_en', 'texto_mod_ptbr', 'arquivo_audio',
                'tipo_audio', 'arquivo_origem', 'destino_nativo'
            ], delimiter=';')
            w.writeheader()
            w.writerows(linhas_csv)

        json_file = out_dir / 'dialogos.json'
        json_file.write_text(json.dumps(linhas_csv, ensure_ascii=False, indent=2), encoding='utf-8')

        msg = f'Extração concluída para a missão {tab}!\n\n• {copiados} áudio(s) copiados\n• Planilha gerada em: dialogos.csv\n• Pasta: {out_dir}\n\nDeseja abrir a pasta agora?'
        if messagebox.askyesno('Extração Concluída', msg):
            abrir_no_sistema(out_dir)

    # ---------- Arquivos GXT
    def _detectar_mod_em_caminho(self, p):
        if not p:
            return None
        try:
            path_obj = Path(p).resolve()
            partes = path_obj.parts
            for i, part in enumerate(partes):
                if part.lower() == 'modloader' and i + 1 < len(partes):
                    mod_nome = partes[i + 1]
                    if mod_nome and not mod_nome.startswith('.'):
                        CFG['modloader_mod'] = mod_nome
                        cfg_save()
                        if getattr(self, 'jogo', None) is not None:
                            self.jogo.cfg['modloader_mod'] = mod_nome
                            self.jogo._ml_escaneado = False
                            self.jogo.escanear_modloader()
                        return mod_nome
        except Exception:
            pass
        return None

    def abrir(self, caminho=None):
        if not self.confirmar_descarte():
            return
        p = caminho or filedialog.askopenfilename(filetypes=[('Arquivos GXT', '*.gxt'), ('Todos', '*.*')])
        if not p:
            return
        try:
            mod_det = self._detectar_mod_em_caminho(p)
            replacement = Gxt(p)
            for library in getattr(self, 'library_windows', []):
                library.close()
            self.library_windows = []
            for voice in self.voice_windows:
                voice.close()
            self.voice_windows = []
            for native in self.native_windows:
                native.close()
            self.native_windows = []
            for window in self.cutscene_windows:
                window.close()
            self.cutscene_windows = []
            for review in getattr(self, 'review_windows', []):
                if not review.closed:
                    review.close()
            self.review_windows = []
            self.g = replacement
            self.limpar_cache_dublado()
            self.nomes = {}
            self.lembrar_recente()
            self.preencher_tabelas()
            self.atualizar_cabecalho()
            msg = 'Aberto: %s (%d tabelas, %d textos)' % (p, len(self.g.order), sum(len(t['hashes']) for t in self.g.t.values()))
            if mod_det:
                msg += f' [Mod: {mod_det}]'
            self.status(msg)
            self.atualizar_painel_inicio()
            # Se abriu o GXT de um mod do ModLoader e não há arquivo de comparação aberto, sugere o original limpo do jogo
            if mod_det and not self.c:
                gta = CFG.get('game_dir')
                if gta:
                    orig_gxt = Path(gta) / 'text' / 'american.gxt'
                    if orig_gxt.is_file() and str(orig_gxt.resolve()) != str(Path(p).resolve()):
                        self.abrir_cmp(str(orig_gxt))
        except Exception as e:
            messagebox.showerror('Erro ao abrir', str(e))

    def abrir_cmp(self, caminho=None):
        p = caminho or filedialog.askopenfilename(filetypes=[('Arquivos GXT', '*.gxt'), ('Todos', '*.*')])
        if not p:
            return
        try:
            self._detectar_mod_em_caminho(p)
            self.c = Gxt(p)
            self.recalcular_tudo()
            self.atualizar_cabecalho()
            self.status('Comparação: %s' % p)
        except Exception as e:
            messagebox.showerror('Erro ao abrir comparação', str(e))

    def atualizar_cabecalho(self):
        partes = []
        if self.g:
            tot_ed = self.g.total_edited()
            partes.append('Editando: %s%s' % (self.g.path, ' *' if tot_ed else ''))
        if self.c:
            partes.append('Comparando com: %s' % self.c.path)
        self.lbl_arq.config(text='  |  '.join(partes) if partes else 'Nenhum arquivo aberto')
        tit = 'GXT Editor & Mission Audio Studio'
        if self.g:
            tit = ('*%s - ' if self.g.total_edited() else '%s - ') % Path(self.g.path).name + tit
        self.root.title(tit)

    def confirmar_descarte(self):
        if not self.g or not self.g.total_edited():
            return True
        r = messagebox.askyesnocancel('Salvar alterações', 'Há alterações não salvas no arquivo aberto.\nSalvar antes de continuar?')
        if r is None:
            return False
        if r:
            return self.salvar()
        return True

    def fechar(self):
        if self.confirmar_descarte():
            if self.redub_ativo and not messagebox.askyesno('Dublagem em andamento', 'Há uma redublagem em andamento. Fechar mesmo assim?'):
                return
            self.closed = True
            self.redub_cancelado = True
            self.jobs.shutdown()
            if self.archive_cancel:
                self.archive_cancel.set()
            if getattr(self, 'jogo', None):
                self.jogo.cancelar()
            for window in self.library_windows:
                window.close()
            for window in self.voice_windows:
                window.close()
            for window in self.native_windows:
                window.close()
            for window in self.cutscene_windows:
                window.close()
            for window in self.review_windows:
                if not window.closed:
                    window.close()
            self.player.stop()
            self.salvar_sessao()
            self.root.destroy()

    def salvar(self):
        if not self.g:
            return False
        if not self.commit():
            return False
        return self._gravar(self.g.path)

    def salvar_como(self):
        if not self.g or not self.commit():
            return False
        p = filedialog.asksaveasfilename(defaultextension='.gxt', filetypes=[('Arquivos GXT', '*.gxt')])
        if not p:
            return False
        return self._gravar(p)

    def _gravar(self, path):
        try:
            self.g.save(path, backups_dir=PROJ_ROOT / 'backups')
            self.preencher_tabelas()
            self.atualizar_cabecalho()
            self.status('Texto GXT salvo: %s · áudio aprovado fica pendente no projeto até “Aplicar áudio ao mod”.' % path)
            return True
        except Exception as e:
            messagebox.showerror('Erro ao salvar', str(e))
            return False

    def nomes_tabela(self, tab, forcar=True):
        if not self.g or tab not in self.g.t:
            return {}
        if tab in self.nomes:
            return self.nomes[tab]
        hashes = self.g.t[tab]['hashes']
        achados = recuperar_nomes(tab, hashes) if forcar else {}
        salvou = False
        for h in hashes:
            if h not in achados and str(h) in self.cache_nomes:
                achados[h] = self.cache_nomes[str(h)]
            elif h in achados and str(h) not in self.cache_nomes:
                self.cache_nomes[str(h)] = achados[h]
                salvou = True
        if salvou:
            salvar_cache_nomes(self.cache_nomes)
        for h in hashes:
            key = self.nav_catalog.key_names.get('%s|%08x' % (tab, h))
            if key:
                achados.setdefault(h, key)
        self.nomes[tab] = achados
        return achados

    def ref(self, tab, h):
        if self.c and tab in self.c.t and h in self.c.t[tab]['cur']:
            return self.c.t[tab]['cur'][h]
        return None

    def estado(self, tab, h):
        if not self.c or tab not in self.c.t or h not in self.c.t[tab]['cur']:
            return 'só_aqui'
        bc = self.c.t[tab]['cur'][h]
        bg = self.g.t[tab]['cur'][h]
        if norm_cmp(bc, self.var_tags.get()) == norm_cmp(bg, self.var_tags.get()):
            return 'igual'
        return 'diferente'

    def n_dif(self, tab):
        if not self.c or tab not in self.c.t:
            return 0
        hc = set(self.c.t[tab]['hashes'])
        return sum(1 for h in self.g.t[tab]['hashes'] if (h not in hc) or
                   norm_cmp(self.g.t[tab]['cur'][h], self.var_tags.get()) != norm_cmp(self.c.t[tab]['cur'][h], self.var_tags.get()))

    def n_audios(self, tab):
        if not self.g or tab not in self.g.t or not self.script_db:
            return 0
        return sum(1 for h in self.g.t[tab]['hashes'] if (tab, h) in self.script_db or ('ANY', h) in self.script_db)

    def preencher_tabelas(self):
        sel_antes = self.tabela_atual()
        self.tv_tab.delete(*self.tv_tab.get_children())
        if not self.g:
            self.preencher_navegacao()
            return
        n_todas = sum(len(self.g.t[n]['hashes']) for n in self.g.order)
        aud_todas = sum(self.n_audios(n) for n in self.g.order)
        dif_todas = sum(self.n_dif(n) for n in self.g.order)
        ed_todas = self.g.total_edited()
        self.tv_tab.insert('', 'end', iid='*', text='* (todas as falas)', values=(n_todas, aud_todas or '', dif_todas, ed_todas or ''))
        for n in self.g.order:
            self.tv_tab.insert('', 'end', iid=n, text=n, values=self.vals_tab(n))
        alvo = sel_antes if (sel_antes and (sel_antes == '*' or sel_antes in self.g.order)) else (self.g.order[0] if self.g.order else None)
        if alvo and self.tv_tab.exists(alvo):
            self.tv_tab.selection_set(alvo)
        self.preencher_navegacao()

    def vals_tab(self, n):
        ed = self.g.n_edited(n)
        aud = self.n_audios(n)
        return (len(self.g.t[n]['hashes']), aud if aud else '', self.n_dif(n), ed if ed else '')

    def recalcular_tudo(self):
        if not self.g:
            return
        for n in self.g.order:
            if self.tv_tab.exists(n):
                self.tv_tab.item(n, values=self.vals_tab(n))
        self.atualizar_linhas()
        self.atualizar_stats()

    def ao_escolher_tabela(self):
        if self.navegacao_categorias():
            return
        if not self.commit():
            return
        self.limpar_editor()
        self.atualizar_linhas()
        self.atualizar_stats()

    def tabela_atual(self):
        if self.navegacao_categorias():
            category = self.categoria_atual()
            if not category:
                return None
            # A category is never authority to process an entire source table.
            # Table-oriented integrations (SAAT install, voice batch) must use
            # explicit table mode, even when every member happens to be MAIN.
            return '*'
        s = self.tv_tab.selection()
        return s[0] if s else None

    def info_script(self, tab, h, ev_nome=None):
        if not self.script_db:
            return None
        rec = consultar_script_info(self.script_db, tab, h, ev_nome)
        if rec:
            if not is_stream_record(rec):
                return self._speaker_info(tab, h, rec)
            from studio.audio.source_routing import audio_route
            rec = dict(rec)
            try:
                route = audio_route(rec)
                rec['audio_route'] = route
                if route and route['family'] == 'streams':
                    from studio.tts.stream_reference import resolve_stream_reference
                    reference = resolve_stream_reference(rec, PROJ_ROOT)
                    rec.update(duration_seconds=reference['duration_seconds'],
                               original_stream='%s/%s' % (route['container'], route['track']),
                               stream_reference_path=reference['path'],
                               stream_timing_status=reference.get('timing_status'))
            except (OSError, ValueError, KeyError) as exc:
                rec['stream_reference_error'] = str(exc)
            return self._speaker_info(tab, h, rec)
        return None

    def _speaker_info(self, tab, h, record):
        result = dict(record)
        manual = self.review_store.character('%s|%08x' % (tab, h)) if self.review_store else ''
        if manual:
            result.update(speaker_identity=manual, speaker_identity_status='MANUAL', speaker_kind='user_override')
            return result
        if record.get('speaker_identity_status') == 'VERIFIED' and record.get('speaker_identity'):
            return result
        voice = self.voice_identity_for(record)
        if voice and voice.get('identity'):
            result.update(speaker_identity=voice['identity'], speaker_identity_status=voice['status'],
                          speaker_kind='named_character', speaker_evidence=voice)
            return result
        from studio.audio.library_context import base_texts
        from studio.script.speakers import resolve_speaker
        try:
            original = base_texts(self).get((tab, h))
            speaker = resolve_speaker(tab, original) if original else {'status': 'unknown'}
        except (OSError, ValueError):
            speaker = {'status': 'unknown'}
        if speaker['status'] == 'matched':
            result.update(speaker_identity=speaker['identity'], speaker_identity_status='WIKI_EXACT_TEXT',
                          speaker_kind=speaker.get('speaker_kind'), speaker_evidence=speaker)
        elif speaker['status'] == 'ambiguous':
            result.update(speaker_identity=None, speaker_identity_status='WIKI_AMBIGUOUS', speaker_evidence=speaker)
        return result

    def dados_linha(self, tab, h, nome=None):
        ed = '✎' if self.g.t[tab]['cur'][h] != self.g.t[tab]['orig'][h] else ''
        r = self.ref(tab, h)
        est = self.estado(tab, h)
        rot_est = 'só aqui' if est == 'só_aqui' else '≠ dif.' if est == 'diferente' else ''
        # Row rendering needs only the authoritative SCRIPT mapping fields.
        # Speaker enrichment can lazily parse the full base-game GXT and wiki
        # index, which can otherwise block the first cold search row.
        ainfo = consultar_script_info(self.script_db, tab, h, nome) if self.script_db else None
        ev_nome = (ainfo.get('event_name') if ainfo else None) or nome
        rot_chave = ev_nome or ('0x%08X' % h)
        dub_path = self.achar_audio_dublado(tab, h, ev_nome)

        ck = (tab, int(h)) if h is not None else (tab, ev_nome)
        cached_orig = self.original_audio_cache.get(ck)
        orig_path = cached_orig[0] if (cached_orig and cached_orig[0]) else None
        if not orig_path and getattr(self, 'jogo', None) is not None and ainfo:
            p = self.jogo.original_para(ainfo)
            if p and Path(p).is_file():
                orig_path = p
                self.original_audio_cache[ck] = (p, 'SAAT (áudio original do jogo)', 100)

        mod_path = self.resolver_audio_mod(tab, h, ainfo=ainfo, chave=ev_nome)

        astring = ainfo.get('stable_audio_id', '') if ainfo else ''
        base_col = astring or (Path(orig_path).stem if orig_path else (ev_nome if ainfo else ''))
        from studio.audio.source_routing import audio_route
        try:
            route = audio_route(ainfo)
        except ValueError:
            route = None
        if route and route['family'] == 'streams':
            base_col = '%s/%s · %s' % (route['container'], route['track'], ev_nome or astring)
        tem_wav = bool(orig_path)
        if tem_wav:
            col_audio = f"🔊 {base_col}" if base_col else "🔊"
        elif ainfo:
            col_audio = f"○ {base_col}" if base_col else "○"
            if route and route['family'] == 'streams':
                col_audio += ' (trecho não extraído)'
        else:
            col_audio = base_col

        if mod_path:
            col_mod = f"📦 {Path(mod_path).name}"
        elif ainfo and getattr(self, 'jogo', None) and self.jogo.instalado_nosso(ainfo):
            col_mod = "📦 SCRIPT"
        elif ainfo:
            col_mod = "○ Mod"
        else:
            col_mod = ""

        dub_str = '🎙️ AI' if dub_path else ''
        txt_cmp = decode_text_c(r) if r is not None else ''
        txt_cur = decode_text_c(self.g.t[tab]['cur'][h])
        marcas = self.ws.get('%s|%08x' % (tab, h))
        fl_str = ('★' if marcas.get('fav') else '') + ('✓' if marcas.get('rev') else '') + ('📝' if marcas.get('note') else '')
        av = lint_avisos(txt_cur, txt_cmp if r is not None else None)
        fl = set()
        if ed:
            fl.add('edit')
        if est == 'diferente':
            fl.add('dif')
        elif est == 'igual':
            fl.add('igual')
        elif est == 'só_aqui':
            fl.add('solo')
        if tem_wav:
            fl.add('audio')
        if route and route['family'] == 'streams':
            # Linked stream dialogue exists even before its audition WAV is cached.
            # Playback remains gated on a real resolved file.
            fl.add('stream_link')
        if mod_path:
            fl.add('mod')
        if ainfo:
            fl.add('script')
        if dub_path:
            fl.add('dub')
        if marcas.get('fav'):
            fl.add('fav')
        if marcas.get('rev'):
            fl.add('rev')
        if marcas.get('note'):
            fl.add('note')
        if av:
            fl.add('lint')
        if getattr(self, 'jogo', None) is not None and ainfo:
            if self.jogo.instalado_nosso(ainfo):
                fl.add('inst')
        return (ed, fl_str, tab, rot_chave, col_audio, col_mod, dub_str, txt_cmp, txt_cur, rot_est), fl

    def tags_linha(self, fl):
        tags = []
        if 'edit' in fl:
            tags.append('edit')
        elif 'dif' in fl:
            tags.append('dif')
        elif 'solo' in fl:
            tags.append('solo')
        if 'dub' in fl:
            tags.append('has_dub')
        elif 'mod' in fl and 'audio' in fl:
            tags.append('has_both')
        elif 'audio' in fl:
            tags.append('has_audio')
        elif 'mod' in fl:
            tags.append('has_mod')
        return tags

    def agendar_atualizacao(self):
        self._cancelar_busca_pendente()
        self.busca_token += 1
        token = self.busca_token
        self.busca_contexto = None
        if self.lbl_stats.winfo_exists():
            self.lbl_stats.config(text='Preparando busca…')
        self.busca_job = self.root.after(160, lambda: self._executar_busca(token))

    def _cancelar_busca_pendente(self):
        if self.busca_job:
            try:
                self.root.after_cancel(self.busca_job)
            except tk.TclError:
                pass
            self.busca_job = None

    def _executar_busca(self, token=None):
        if token is None:
            token = self.busca_token
        if token != self.busca_token or self.closed:
            return
        self.busca_job = None
        tab = self.tabela_atual()
        if not self.g or (not tab and not self.var_todas.get()):
            self.tv.delete(*self.tv.get_children())
            self.row_info.clear()
            self.atualizar_stats()
            return

        tabs = list(self.g.order) if (tab == '*' or self.var_todas.get()) else [tab]
        filtro = self.var_filtro.get()
        busca = self.var_busca.get().strip().lower()
        pred = FILTRO_PRED.get(filtro, lambda f: True)
        pairs = self.pares_navegacao()
        linhas = iter(pairs)
        self.busca_contexto = {
            'token': token, 'tab': tab, 'tabs': tabs, 'busca': busca,
            'filtro': filtro, 'pred': pred, 'linhas': linhas,
            'total': len(pairs),
            'vistos': 0, 'itens': [], 'fase': 'scan',
        }
        if self.busca_cache_filtro != filtro:
            self.busca_cache_linhas.clear()
            self.busca_cache_filtro = filtro
        self.lbl_stats.config(text='Buscando…')
        self.busca_job = self.root.after(1, lambda: self._processar_busca(token))

    def _processar_busca(self, token):
        contexto = self.busca_contexto
        if self.closed or token != self.busca_token or not contexto or contexto['token'] != token:
            return
        self.busca_job = None
        limite_tempo = time.perf_counter() + 0.008
        if contexto['fase'] == 'scan':
            while time.perf_counter() < limite_tempo:
                try:
                    tab, h = next(contexto['linhas'])
                except StopIteration:
                    contexto['fase'] = 'render'
                    contexto['indice'] = 0
                    self.tv.delete(*self.tv.get_children())
                    self.row_info.clear()
                    break
                contexto['vistos'] += 1
                nomes = self.nomes_tabela(tab, forcar=False)
                row_key = (tab, h)
                cached = self.busca_cache_linhas.get(row_key)
                if cached is None:
                    vals, fl = self.dados_linha(tab, h, nomes.get(h))
                    nota = self.ws.get('%s|%08x' % (tab, h)).get('note', '').lower()
                    campos = tuple(str(vals[i]).lower() for i in (3, 4, 5, 7, 8))
                    cached = (vals, fl, nota, campos, ('%08x' % h).lower())
                    self.busca_cache_linhas[row_key] = cached
                vals, fl, nota, campos, h_hex = cached
                if contexto['pred'](fl) and (not contexto['busca'] or
                        any(contexto['busca'] in campo for campo in campos) or
                        contexto['busca'] in h_hex or contexto['busca'] in nota):
                    contexto['itens'].append((tab, h, vals, fl))
            if contexto['fase'] == 'scan':
                self.lbl_stats.config(text=f"Buscando… {contexto['vistos']}/{contexto['total']} falas")
                self.busca_job = self.root.after(1, lambda: self._processar_busca(token))
                return

        if contexto['fase'] == 'render':
            limite = min(len(contexto['itens']), self.MAX_LINHAS)
            while contexto['indice'] < limite and time.perf_counter() < limite_tempo:
                tab, h, vals, fl = contexto['itens'][contexto['indice']]
                iid = '%s|%08x' % (tab, h)
                self.row_info[iid] = fl
                self.tv.insert('', 'end', iid=iid, values=vals, tags=self.tags_linha(fl))
                contexto['indice'] += 1
            if contexto['indice'] < limite:
                self.lbl_stats.config(text=f"Exibindo… {contexto['indice']}/{limite} falas")
                self.busca_job = self.root.after(1, lambda: self._processar_busca(token))
                return
            if len(contexto['itens']) > limite:
                self.tv.insert('', 'end', iid='_mais_', values=(
                    '', '', '', '…', '', '', '',
                    f'(mais {len(contexto["itens"])-limite} falas ocultas; refine a busca ou aumente max_linhas)', '', ''))
            sel_antes = self.sel
            if sel_antes:
                iid_antes = '%s|%08x' % sel_antes
                if self.tv.exists(iid_antes):
                    self.ignorando_sel = True
                    try:
                        self.tv.selection_set(iid_antes)
                        self.tv.see(iid_antes)
                    finally:
                        self.ignorando_sel = False
            self.busca_contexto = None
            self.atualizar_stats()

    def limpar_busca(self):
        self.var_busca.set('')
        self.e_busca.focus()

    def focar_busca(self):
        self.e_busca.focus()
        self.e_busca.select_range(0, 'end')

    def atualizar_linhas(self):
        self._cancelar_busca_pendente()
        self.busca_token += 1
        token = self.busca_token
        self.busca_contexto = None
        self.busca_cache_linhas.clear()
        self.busca_cache_filtro = self.var_filtro.get()
        self.lbl_stats.config(text='Atualizando lista…')
        self.busca_job = self.root.after(0, lambda: self._executar_busca(token))

    def atualizar_linha(self, tab, h):
        if self.busca_contexto is not None or self.busca_job is not None:
            self.atualizar_linhas()
            return
        self.busca_cache_linhas.pop((tab, h), None)
        iid = '%s|%08x' % (tab, h)
        if not self.tv.exists(iid):
            return
        nomes = self.nomes_tabela(tab, forcar=False)
        vals, fl = self.dados_linha(tab, h, nomes.get(h))
        self.row_info[iid] = fl
        tags = self.tags_linha(fl)
        self.tv.item(iid, values=vals, tags=tags)

    def atualizar_stats(self):
        if not self.g:
            self.lbl_stats.config(text='')
            return
        tab = self.tabela_atual()
        if not tab and not self.var_todas.get():
            return
        if self.navegacao_categorias():
            pairs = self.pares_navegacao()
            tot = len(pairs)
            dif = sum(self.estado(t, h) != 'igual' for t, h in pairs) if self.c else 0
            ed = sum(self.g.t[t]['cur'][h] != self.g.t[t]['orig'][h] for t, h in pairs)
        elif tab == '*':
            tot = sum(len(self.g.t[n]['hashes']) for n in self.g.order)
            dif = sum(self.n_dif(n) for n in self.g.order)
            ed = self.g.total_edited()
        else:
            tot = len(self.g.t[tab]['hashes'])
            dif = self.n_dif(tab)
            ed = self.g.n_edited(tab)
        vis = len(self.tv.get_children()) - (1 if self.tv.exists('_mais_') else 0)
        partes = [f'{vis}/{tot} visíveis', f'{dif} diferentes' if self.c else 'sem comparação']
        if ed:
            partes.append(f'{ed} editadas (não salvas)')
        self.lbl_stats.config(text=' · '.join(partes))

    def limpar_editor(self):
        self.sel = None
        self.audio_atual = None
        self.audio_dublado_atual = None
        self.audio_instalado_atual = None
        self.t_cmp.config(state='normal')
        self.t_cmp.delete('1.0', 'end')
        self.t_cmp.config(state='disabled')
        self.t_cur.delete('1.0', 'end')
        self.lbl_info.config(text='')
        self.lbl_audio.config(text='')
        self.lbl_nota.config(text='')
        self.btn_fav.config(text='☆')
        self.btn_rev.config(text='✓ Revisada')
        self.btn_audio.config(state='disabled')
        self.btn_audio_dub.config(state='disabled')
        self.btn_ab.config(state='disabled')
        if hasattr(self, 'btn_audio_inst'):
            self.btn_audio_inst.config(state='disabled')
        self.btn_redub_fala.config(state='disabled')
        for w in self.f_fix.winfo_children():
            w.destroy()

    def ao_escolher_linha(self):
        if self.ignorando_sel:
            return
        s = self.tv.selection()
        if not s or s[0] == '_mais_':
            return
        tab, hex_str = s[0].split('|')
        h = int(hex_str, 16)
        if self.sel == (tab, h):
            return
        if not self.commit():
            if self.sel:
                self.ignorando_sel = True
                self.tv.selection_set('%s|%08x' % self.sel)
                self.ignorando_sel = False
            return
        self._carregar_linha(tab, h)
        modo_auto = self.var_autoplay.get()
        if modo_auto != 'nada':
            if self.autoplay_job:
                self.root.after_cancel(self.autoplay_job)
            self.autoplay_job = self.root.after(140, lambda: self._autoplay(modo_auto))

    def _autoplay(self, modo):
        self.autoplay_job = None
        if modo == 'original':
            self.tocar_audio()
        elif modo == 'dublado':
            self.tocar_audio_dublado()
        elif modo == 'ab':
            self.tocar_ab()

    def _carregar_linha(self, tab, h):
        self.sel = (tab, h)
        r = self.ref(tab, h)
        b = self.g.t[tab]['cur'][h]
        self.t_cmp.config(state='normal')
        self.t_cmp.delete('1.0', 'end')
        if r is not None:
            self.t_cmp.insert('1.0', decode_text_c(r))
        self.t_cmp.config(state='disabled')
        self.t_cur.delete('1.0', 'end')
        self.t_cur.insert('1.0', decode_text_c(b))
        self.t_cur.edit_reset()
        self.destacar()
        self.lint()
        nomes = self.nomes_tabela(tab, forcar=False)
        ev_nome = nomes.get(h)
        ainfo = self.info_script(tab, h, ev_nome)
        if not ev_nome and ainfo:
            ev_nome = ainfo.get('event_name')

        self.audio_atual = self.wav_original(ainfo=ainfo, tab=tab, h=h, chave=ev_nome)
        self.audio_dublado_atual = self.achar_audio_dublado(tab, h, ev_nome)
        self.audio_instalado_atual = self.resolver_audio_mod(tab, h, ainfo=ainfo, chave=ev_nome)
        self.atualizar_linha(tab, h)
        partes_audio = []
        mod_n = self.jogo.mod_ativo() if getattr(self, 'jogo', None) else 'ModLoader'
        caption_status = _intro_caption_audio_status(
            ainfo, self.audio_atual, self.audio_instalado_atual, mod_n
        )
        if caption_status:
            partes_audio.extend(caption_status)
        elif ainfo or self.audio_atual:
            dur = ainfo.get('duration_seconds', 0) if ainfo else (wav_duracao(self.audio_atual) or 0.0)
            aid = ainfo.get('stable_audio_id') if ainfo else (f'EVENT:{ev_nome}' if ev_nome else '0x%08X' % h)
            orig_txt = f"Original (SAAT Jogo Limpo): {aid} ({dur:.2f}s)" if dur else f"Original (SAAT Jogo Limpo): {aid}"
            if self.audio_atual:
                orig_txt += f" [WAV ✓ {Path(self.audio_atual).name}]"
            else:
                orig_txt += " (WAV não localizado)"
            partes_audio.append(orig_txt)
        if not caption_status:
            if self.audio_instalado_atual:
                d_inst = wav_duracao(self.audio_instalado_atual) or 0.0
                partes_audio.append(f"ModLoader ({mod_n}): [WAV ✓ {Path(self.audio_instalado_atual).name}] ({d_inst:.2f}s)")
            elif ainfo:
                partes_audio.append(f"ModLoader ({mod_n}): (não extraído)")
        if self.audio_dublado_atual:
            d_dub = wav_duracao(self.audio_dublado_atual) or 0.0
            d_orig = ainfo.get('duration_seconds', 0) if ainfo else (wav_duracao(self.audio_atual) or 0.0)
            delta = classe_delta(d_orig, d_dub) if d_orig else ''
            partes_audio.append(f"Dublado (Projeto): {Path(self.audio_dublado_atual).name} ({d_dub:.2f}s) {delta}".strip())
        elif ainfo or self.audio_atual:
            partes_audio.append("Dublado (Projeto): (não gerado)")
        else:
            partes_audio.append('SCRIPT: sem vínculo confirmado para esta chave GXT')
        if getattr(self, 'jogo', None) is not None and not caption_status:
            partes_audio += self.jogo.info_linha(ainfo, self.audio_dublado_atual)
        self.lbl_audio.config(text='  |  '.join(partes_audio))
        self.btn_audio.config(state='normal' if self.audio_atual else 'disabled')
        self.btn_audio_dub.config(state='normal' if self.audio_dublado_atual else 'disabled')
        self.btn_ab.config(state='normal' if (self.audio_atual and self.audio_dublado_atual) else ('normal' if (self.audio_atual or self.audio_dublado_atual) else 'disabled'))
        if hasattr(self, 'btn_audio_inst'):
            self.btn_audio_inst.config(state='normal' if self.audio_instalado_atual else 'disabled')
        self.btn_redub_fala.config(state='normal' if (ainfo or ev_nome or self.redub_ativo) else 'disabled')
        m = self.ws.get('%s|%08x' % (tab, h))
        self.lbl_nota.config(text=('Nota: ' + m['note']) if m.get('note') else '')
        self._atualizar_marcas_btn()

    def ao_digitar(self, e=None):
        if self.t_cur.edit_modified():
            self.t_cur.edit_modified(False)
            if self.diff_job:
                self.root.after_cancel(self.diff_job)
            self.diff_job = self.root.after(140, self._apos_digitar)

    def _apos_digitar(self):
        self.diff_job = None
        self.destacar()
        self.lint()

    def destacar(self):
        self.t_cmp.tag_remove('diff', '1.0', 'end')
        self.t_cur.tag_remove('diff', '1.0', 'end')
        tc = self.t_cmp.get('1.0', 'end-1c')
        tu = self.t_cur.get('1.0', 'end-1c')
        if not tc or not tu or tc == tu:
            return
        m = difflib.SequenceMatcher(None, tc, tu)
        for tag, i1, i2, j1, j2 in m.get_opcodes():
            if tag in ('replace', 'delete'):
                self.t_cmp.tag_add('diff', f'1.0+{i1}c', f'1.0+{i2}c')
            if tag in ('replace', 'insert'):
                self.t_cur.tag_add('diff', f'1.0+{j1}c', f'1.0+{j2}c')

    def lint(self):
        for w in self.f_fix.winfo_children():
            w.destroy()
        if not self.sel:
            return
        tab, h = self.sel
        tu = self.t_cur.get('1.0', 'end-1c')
        r = self.ref(tab, h)
        ref_txt = decode_text(r) if r is not None else None
        av = lint_avisos(tu, ref_txt)
        if not av:
            self.lbl_info.config(text='')
            return
        self.lbl_info.config(text='⚠ ' + '  ·  '.join(msg for msg, fx in av))
        for msg, fx in av:
            if fx and fx in FIX_LABELS:
                ttk.Button(self.f_fix, text=FIX_LABELS[fx],
                           command=lambda fxx=fx: self.aplicar_quick_fix(fxx)).pack(side='left', padx=(0, 4))

    def aplicar_quick_fix(self, fx):
        if not self.sel:
            return
        tu = self.t_cur.get('1.0', 'end-1c')
        r = self.ref(*self.sel)
        ref_txt = decode_text(r) if r is not None else None
        novo = aplicar_fix(tu, fx, ref_txt)
        if novo != tu:
            self.t_cur.delete('1.0', 'end')
            self.t_cur.insert('1.0', novo)
            self.destacar()
            self.lint()

    def commit(self, avisar=True):
        if not self.sel:
            return True
        tab, h = self.sel
        txt = self.t_cur.get('1.0', 'end-1c')
        b, bad = encode_text(txt)
        if bad and avisar:
            c = ' '.join(sorted(set(bad)))
            if not messagebox.askyesno('Caracteres incompatíveis', f'Caracteres não suportados pela fonte: {c}\nGravar assim mesmo?'):
                return False
        antigo = self.g.t[tab]['cur'][h]
        if b != antigo:
            self.g.t[tab]['cur'][h] = b
            self.atualizar_linha(tab, h)
            self.preencher_tabelas()
            self.atualizar_cabecalho()
        return True

    def commit_silencioso(self):
        return self.commit(avisar=False)

    def aplicar(self):
        if self.commit():
            self.status('Texto aplicado à memória (Ctrl+S para salvar no disco).')

    def copiar_cmp(self):
        tc = self.t_cmp.get('1.0', 'end-1c')
        if tc:
            self.t_cur.delete('1.0', 'end')
            self.t_cur.insert('1.0', tc)
            self.destacar()
            self.lint()

    def desfazer(self):
        if not self.sel:
            return
        tab, h = self.sel
        self.g.t[tab]['cur'][h] = bytes(self.g.t[tab]['orig'][h])
        self._carregar_linha(tab, h)
        self.atualizar_linha(tab, h)
        self.preencher_tabelas()
        self.atualizar_cabecalho()
        self.status('Restaurado ao estado salvo.')

    def _atualizar_marcas_btn(self):
        if not self.sel:
            return
        m = self.ws.get('%s|%08x' % self.sel)
        self.btn_fav.config(text='★' if m.get('fav') else '☆')
        self.btn_rev.config(text='✓ Revisada' if m.get('rev') else 'Revisar')

    def _alternar_marca(self, campo, rotulo_on, rotulo_off):
        if not self.sel:
            return
        k = '%s|%08x' % self.sel
        atual = bool(self.ws.get(k).get(campo))
        self.ws.set(k, **{campo: not atual})
        self._atualizar_marcas_btn()
        self.atualizar_linha(*self.sel)
        self.status(rotulo_off if atual else rotulo_on)

    def alternar_favorito(self):
        self._alternar_marca('fav', 'Marcada como favorita ★', 'Desmarcada de favorita')

    def alternar_revisada(self):
        self._alternar_marca('rev', 'Marcada como revisada ✓', 'Desmarcada de revisada')

    def editar_nota(self):
        if not self.sel:
            return
        k = '%s|%08x' % self.sel
        atual = self.ws.get(k).get('note', '')
        from tkinter import simpledialog
        n = simpledialog.askstring('Nota da fala', 'Anotação (salva no workspace do projeto):',
                                   initialvalue=atual, parent=self.root)
        if n is not None:
            self.ws.set(k, note=n.strip())
            self.lbl_nota.config(text=('Nota: ' + n.strip()) if n.strip() else '')
            self.atualizar_linha(*self.sel)

    def selecionar_iid(self, iid):
        if self.tv.exists(iid):
            self.tv.selection_set(iid)
            self.tv.see(iid)

    def ir_proxima(self, chave, passo):
        if not self.commit():
            return
        pred = PREDICADOS.get(chave, lambda f: True)
        itens = [i for i in self.tv.get_children() if i != '_mais_' and pred(self.row_info.get(i, set()))]
        if not itens:
            self.status(f'Nenhuma fala encontrada com o critério ({NOMES_NAV.get(chave, chave)}).')
            return
        cur_s = self.tv.selection()
        cur_id = cur_s[0] if (cur_s and cur_s[0] != '_mais_') else None
        try:
            idx = itens.index(cur_id) if cur_id in itens else -1
            prox = itens[(idx + passo) % len(itens)]
        except ValueError:
            prox = itens[0]
        self.selecionar_iid(prox)

    def proxima_dif(self, passo):
        self.ir_proxima('dif', passo)

    def mover_linha(self, passo):
        if not self.commit():
            return
        itens = [i for i in self.tv.get_children() if i != '_mais_']
        if not itens:
            return
        cur_s = self.tv.selection()
        cur = cur_s[0] if (cur_s and cur_s[0] != '_mais_') else itens[0]
        i = max(0, min(len(itens) - 1, (itens.index(cur) if cur in itens else 0) + passo))
        self.selecionar_iid(itens[i])

    # ---------- CSV
    def exportar_csv(self):
        if not self.g or not self.commit():
            return
        tab = self.tabela_atual()
        if self.navegacao_categorias():
            return self.exportar_csv_categoria()
        todas = messagebox.askyesno('Exportar CSV', 'Exportar TODAS as tabelas?\n(Não = só a selecionada)') \
            if tab not in (None, '*') else True
        tabs = self.g.order if todas else [tab]
        p = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')],
                                         initialfile='textos.csv')
        if not p:
            return
        with open(p, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['tabela', 'id', 'chave', 'audio_id', 'comparacao', 'atual'])
            for t in tabs:
                nm = self.nomes_tabela(t, forcar=False)
                for h in self.g.t[t]['hashes']:
                    r = self.ref(t, h)
                    ainfo = self.script_db.get((t, h)) or self.script_db.get(('ANY', h))
                    astring = ainfo['stable_audio_id'] if ainfo else ''
                    w.writerow([t, '0x%08X' % h, nm.get(h, ''), astring,
                                decode_text(r) if r is not None else '',
                                decode_text(self.g.t[t]['cur'][h])])
        self.status('CSV exportado: %s' % p)

    def importar_csv(self):
        if not self.g or not self.commit():
            return
        p = filedialog.askopenfilename(filetypes=[('CSV', '*.csv'), ('Todos', '*.*')])
        if not p:
            return
        try:
            with open(p, newline='', encoding='utf-8-sig') as f:
                rd = csv.DictReader(f, delimiter=';')
                campos = rd.fieldnames or []
                linhas = list(rd)
        except Exception as e:
            messagebox.showerror('Importar CSV', f'Não foi possível ler o arquivo:\n{e}')
            return
        if 'tabela' not in campos or 'id' not in campos or not ('atual' in campos or 'texto' in campos):
            messagebox.showerror('Importar CSV', 'Colunas esperadas (separador ";"): tabela, id e atual (ou texto).')
            return
        mudar, problemas = [], []
        iguais = invalidas = nao_achadas = caract = 0
        for n, row in enumerate(linhas, start=2):
            col = row.get('atual', row.get('texto'))
            try:
                tab, h = (row['tabela'] or '').strip(), int((row['id'] or '').strip(), 16)
            except Exception:
                invalidas += 1
                problemas.append(f'linha {n}: tabela/id inválidos')
                continue
            if col is None:
                invalidas += 1
                problemas.append(f'linha {n}: sem texto')
                continue
            if tab not in self.g.t or h not in self.g.t[tab]['cur']:
                nao_achadas += 1
                problemas.append(f'linha {n}: chave {tab}/{h:08X} não existe')
                continue
            b, bad = encode_text(col)
            if bad:
                caract += 1
                problemas.append(f'linha {n} ({tab}/{h:08X}): caracteres incompatíveis')
                continue
            if b == self.g.t[tab]['cur'][h]:
                iguais += 1
                continue
            mudar.append((tab, h, decode_text_c(self.g.t[tab]['cur'][h]), decode_text_c(b), b))

        win = tk.Toplevel(self.root)
        win.title('Importar CSV — Prévia')
        win.geometry('1000x560')
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, padding=8, text=f'{len(mudar)} alteração(ões)  ·  {iguais} iguais  ·  {invalidas} inválidas  ·  {nao_achadas} não achadas').pack(anchor='w')
        fr = ttk.Frame(win)
        fr.pack(fill='both', expand=True, padx=8)
        tv = ttk.Treeview(fr, columns=('tab', 'chave', 'antes', 'depois'), show='headings')
        for c, tt, w in (('tab', 'Tabela', 80), ('chave', 'Chave', 110), ('antes', 'Antes', 380), ('depois', 'Depois (CSV)', 380)):
            tv.heading(c, text=tt)
            tv.column(c, width=w, stretch=c in ('antes', 'depois'))
        sb = ttk.Scrollbar(fr, orient='vertical', command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        for tab, h, antes, depois, b in mudar:
            nome = self.nomes.get(tab, {}).get(h) or '0x%08X' % h
            tv.insert('', 'end', values=(tab, nome, antes[:140], depois[:140]))

        bts = ttk.Frame(win, padding=8)
        bts.pack(fill='x')

        def aplicar():
            self.undo_csv = {}
            for tab, h, antes, depois, b in mudar:
                self.undo_csv[(tab, h)] = self.g.t[tab]['cur'][h]
                self.g.t[tab]['cur'][h] = b
            win.destroy()
            if self.sel:
                self._carregar_linha(*self.sel)
            self.recalcular_tudo()
            self.status(f'CSV aplicado: {len(mudar)} linha(s) alterada(s).')

        ttk.Button(bts, text='Cancelar', command=win.destroy).pack(side='right')
        btn = ttk.Button(bts, text=f'Aplicar {len(mudar)} válida(s)', command=aplicar)
        btn.pack(side='right', padx=6)
        if not mudar:
            btn.config(state='disabled')

    def desfazer_csv(self):
        if not self.undo_csv or not self.g:
            self.status('Nada para desfazer da importação de CSV.')
            return
        if not messagebox.askyesno('Desfazer importação', f'Restaurar os {len(self.undo_csv)} textos alterados pelo CSV?'):
            return
        for (tab, h), b in self.undo_csv.items():
            if tab in self.g.t and h in self.g.t[tab]['cur']:
                self.g.t[tab]['cur'][h] = b
        self.undo_csv = {}
        if self.sel:
            self._carregar_linha(*self.sel)
        self.recalcular_tudo()

    def _copiar(self, texto, rotulo):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(texto)
            self.status(f'{rotulo} copiado para a área de transferência.')
        except Exception:
            pass

    def menu_contexto(self, event):
        iid = self.tv.identify_row(event.y)
        if iid and iid != '_mais_':
            self.tv.selection_set(iid)
            tab, hex_str = iid.split('|')
            h = int(hex_str, 16)
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label='🔊 Tocar áudio original (F5)', command=self.tocar_audio)
            m.add_command(label='🎙️ Tocar áudio dublado (F6)', command=self.tocar_audio_dublado)
            m.add_command(label='A/B (F7)', command=self.tocar_ab)
            m.add_command(label='Revisar voz, takes e efeitos…', command=self.abrir_revisao_audio)
            m.add_separator()
            m.add_command(label='⚡ Redublar fala (AI)', command=self.redublar_fala_selecionada)
            m.add_command(label='★ Alternar favorita (Ctrl+B)', command=self.alternar_favorito)
            m.add_command(label='✓ Alternar revisada', command=self.alternar_revisada)
            m.add_command(label='📝 Nota…', command=self.editar_nota)
            m.add_separator()
            m.add_command(label='Copiar JAMCRC hex', command=lambda: self._copiar('%08X' % h, 'JAMCRC'))
            m.tk_popup(event.x_root, event.y_root)

    def lista_comandos(self):
        cmds = [
            ('Abrir GXT…', self.abrir), ('Comparar com GXT…', self.abrir_cmp),
            ('Salvar', self.salvar), ('Salvar como…', self.salvar_como),
            ('Exportar CSV…', self.exportar_csv), ('Importar CSV…', self.importar_csv),
            ('Aplicar texto', self.aplicar), ('Restaurar texto salvo', self.desfazer),
            ('Copiar texto da comparação', self.copiar_cmp),
            ('★ Alternar favorita', self.alternar_favorito),
            ('✓ Alternar revisada', self.alternar_revisada),
            ('📝 Editar nota', self.editar_nota),
            ('Tocar original', self.tocar_audio), ('Tocar dublado', self.tocar_audio_dublado),
            ('A/B original → dublado', self.tocar_ab),
            ('Revisar voz, takes e efeitos…', self.abrir_revisao_audio),
            ('Áudios AI: confirmar e aplicar em lote…', self.abrir_revisao_audio_lote),
            ('Redublar fala selecionada (AI)', self.redublar_fala_selecionada),
            ('Redublar missão em lote…', self.popup_redublar_lote),
            ('🤖 Gerenciador de Modelos de IA & SAAT…', self.popup_gerenciador_modelos),
            ('📥 Baixar / Configurar SAAT…', self.baixar_saat_gui),
            ('🧭 Guia do projeto e fontes do jogo…', self.abrir_guia_projeto),
            ('Lote automático de cutscenes…', self.abrir_lote_cutscenes),
            ('Diagnóstico do sistema…', self.diagnostico),
            ('Atalhos de teclado (F1)', self.mostrar_atalhos),
        ] + getattr(self, 'comandos_extra', [])
        return cmds

    def paleta_comandos(self):
        win = tk.Toplevel(self.root)
        win.title('Paleta de Comandos')
        win.geometry('540x360')
        win.transient(self.root)
        win.grab_set()

        e = ttk.Entry(win, font=('Segoe UI', 11))
        e.pack(fill='x', padx=8, pady=8)
        lb = tk.Listbox(win, font=('Segoe UI', 10), activestyle='none')
        lb.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        todos = self.lista_comandos()

        def filtrar(*a):
            q = e.get().strip().lower()
            lb.delete(0, 'end')
            for rot, cmd in todos:
                if not q or q in rot.lower():
                    lb.insert('end', rot)
            if lb.size() > 0:
                lb.selection_set(0)

        e.bind('<KeyRelease>', filtrar)
        filtrar()

        def executar(evt=None):
            sel = lb.curselection()
            if sel:
                rot = lb.get(sel[0])
                for r, cmd in todos:
                    if r == rot:
                        win.destroy()
                        cmd()
                        return

        lb.bind('<Double-1>', executar)
        e.bind('<Return>', executar)
        e.focus()

    def mostrar_atalhos(self):
        messagebox.showinfo('Atalhos de Teclado', ATALHOS_TEXTO, parent=self.root)

    def lembrar_recente(self):
        if not self.g or not self.g.path:
            return
        rec = CFG.setdefault('recentes', [])
        p = str(Path(self.g.path).resolve())
        if p in rec:
            rec.remove(p)
        rec.insert(0, p)
        CFG['recentes'] = rec[:10]
        cfg_save()

    def montar_recentes(self):
        self.m_rec.delete(0, 'end')
        for r in CFG.get('recentes', []):
            self.m_rec.add_command(label=r, command=lambda p=r: self.abrir_recente(p))

    def abrir_recente(self, r):
        if Path(r).is_file():
            self.abrir(r)
        else:
            messagebox.showinfo('Arquivo não encontrado', f'O arquivo não existe mais:\n{r}')

    def ui(self, fn):
        if not self.closed:
            self.ui_q.put(fn)

    def _drenar_ui(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:
            escrever_log('app.log', 'erro em callback de UI: %r' % (e,))
        try:
            self.root.after(50, self._drenar_ui)
        except tk.TclError:
            pass

    def salvar_opcoes(self):
        CFG['autoplay'] = self.var_autoplay.get()
        CFG['pausa_ab_ms'] = self.var_pausa_ab.get()
        CFG['engine_padrao'] = self.var_engine.get()
        CFG['omnivoice_timing_policy_version'] = 2
        CFG['omnivoice_speed'] = self.var_omnivoice_speed.get()
        CFG['omnivoice_duration'] = self.var_omnivoice_duration.get()
        CFG['omnivoice_cj_reference'] = bool(self.var_omnivoice_cj_reference.get())
        CFG['omnivoice_gpu_guard'] = bool(self.var_omnivoice_gpu_guard.get())
        cfg_save()

    def set_omnivoice_keep_model_loaded(self):
        """Persist the resident-model preference and apply it to the job queue."""
        enabled = bool(self.var_omnivoice_keep_model_loaded.get())
        CFG['omnivoice_keep_model_loaded'] = enabled
        cfg_save()
        self.jobs.set_keep_model_loaded(enabled)

    def salvar_sessao(self):
        try:
            CFG['geometry'] = self.root.geometry()
            CFG['colunas'] = {c: int(self.tv.column(c, 'width')) for c in self.tv['columns']}
            CFG['sessao'] = {
                'gxt': self.g.path if self.g else '', 'cmp': self.c.path if self.c else '',
                'tab': self.tabela_atual() or '', 'iid': ('%s|%08x' % self.sel) if self.sel else '',
                'filtro': self.var_filtro.get(), 'busca': self.var_busca.get(),
                'todas': bool(self.var_todas.get()), 'tags': bool(self.var_tags.get()),
            }
            cfg_save()
        except Exception:
            pass

    def restaurar_sessao(self):
        s = CFG.get('sessao')
        if not s or not isinstance(s, dict):
            return
        if s.get('gxt') and Path(s['gxt']).is_file():
            self.abrir(s['gxt'])
        if s.get('cmp') and Path(s['cmp']).is_file():
            self.abrir_cmp(s['cmp'])
        if s.get('tab') and self.tv_tab.exists(s['tab']):
            self.tv_tab.selection_set(s['tab'])
            self.ao_escolher_tabela()

    def status(self, msg):
        self.lbl_st.config(text=' ' + msg)


def run_gui(arq_edit=None, arq_cmp=None):
    """Ponto de entrada para inicialização da interface gráfica Tkinter."""
    root = tk.Tk()
    app = App(root)
    if arq_edit:
        app.abrir(arq_edit)
    if arq_cmp:
        app.abrir_cmp(arq_cmp)
    if not arq_edit and not arq_cmp:
        app.restaurar_sessao()
    if not app.g:
        app.status('Siga o painel de início ou abra um GXT (Ctrl+O) — F1 mostra os atalhos, Ctrl+P a paleta de comandos.')
    root.lift()
    root.attributes('-topmost', True)
    root.after_idle(root.attributes, '-topmost', False)
    root.focus_force()
    root.mainloop()
