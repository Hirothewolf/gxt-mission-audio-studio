# -*- coding: utf-8 -*-
"""
Diálogos interativos: Confirmação e progresso de download de modelos/SAAT, diagnóstico e lote.
"""
import time
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Dict, Optional, Callable

from studio.config import CFG, cfg_save, PROJ_ROOT, CONFIG_PATH, APP_DIR
from studio.core.utils import abrir_no_sistema, escrever_log, matar_arvore
from studio.saat.downloader import obter_info_instalacao_saat, executar_instalacao_saat
from studio.tts.downloader import (
    listar_modelos, obter_info_modelo, baixar_modelo,
    verificar_modelo_instalado, CATALOGO_MODELOS
)


class ConfirmarDownloadDialog:
    """
    Diálogo modal de confirmação prévia para qualquer download ou extração de binários/modelos.
    Garante que nada seja baixado sem o consentimento explícito do usuário.
    """

    def __init__(self, parent, info: Dict):
        self.parent = parent
        self.info = info
        self.confirmado = False

        self.win = tk.Toplevel(parent)
        self.win.title('Confirmação de Download / Instalação')
        self.win.geometry('620x460')
        self.win.minsize(540, 400)
        self.win.transient(parent)
        self.win.grab_set()

        self._montar()
        self.win.wait_window()

    def _montar(self):
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill='both', expand=True)

        # Cabeçalho
        lbl_titulo = ttk.Label(
            f, text=f"📥 {self.info.get('nome', 'Download de Componente')}",
            font=('Segoe UI', 12, 'bold')
        )
        lbl_titulo.pack(anchor='w', pady=(0, 6))

        lbl_desc = ttk.Label(
            f, text=self.info.get('descricao', ''),
            wraplength=580, font=('Segoe UI', 9)
        )
        lbl_desc.pack(anchor='w', pady=(0, 12))

        # Quadro de detalhes
        q = ttk.LabelFrame(f, text=' Detalhes da Operação ', padding=10)
        q.pack(fill='both', expand=True, pady=(0, 12))

        # Origem
        origem_txt = self.info.get('origem_local') or self.info.get('url_remota') or (
            self.info.get('urls')[0] if self.info.get('urls') else 'Download via repositório'
        )
        tipo_origem = "Arquivo Local no Disco" if self.info.get('origem_local') else "Download Online"

        r = 0
        ttk.Label(q, text='Tipo de Fonte:', font=('Segoe UI', 9, 'bold')).grid(row=r, column=0, sticky='w', pady=3)
        ttk.Label(q, text=tipo_origem, foreground='#007acc' if not self.info.get('origem_local') else '#2e7d32').grid(row=r, column=1, sticky='w', padx=8, pady=3)

        r += 1
        ttk.Label(q, text='Tamanho Estimado:', font=('Segoe UI', 9, 'bold')).grid(row=r, column=0, sticky='w', pady=3)
        ttk.Label(q, text=self.info.get('tamanho_estimado', 'Desconhecido')).grid(row=r, column=1, sticky='w', padx=8, pady=3)

        r += 1
        ttk.Label(q, text='Pasta de Destino:', font=('Segoe UI', 9, 'bold')).grid(row=r, column=0, sticky='w', pady=3)
        lbl_dest = ttk.Label(q, text=self.info.get('destino', ''), wraplength=400)
        lbl_dest.grid(row=r, column=1, sticky='w', padx=8, pady=3)

        r += 1
        arqs = self.info.get('arquivos') or self.info.get('arquivos_esperados') or []
        if arqs:
            ttk.Label(q, text='Arquivos:', font=('Segoe UI', 9, 'bold')).grid(row=r, column=0, sticky='nw', pady=3)
            ttk.Label(q, text=', '.join(arqs), wraplength=400).grid(row=r, column=1, sticky='w', padx=8, pady=3)

        # Aviso
        aviso_txt = "Deseja autorizar o download/instalação deste componente agora?"
        if not self.info.get('origem_local'):
            aviso_txt += "\n(Uma conexão ativa com a internet será utilizada)."
        lbl_aviso = ttk.Label(f, text=aviso_txt, font=('Segoe UI', 9, 'italic'), foreground='#555')
        lbl_aviso.pack(anchor='w', pady=(0, 12))

        # Botões
        bts = ttk.Frame(f)
        bts.pack(fill='x')

        btn_ok = ttk.Button(bts, text='✔ Confirmar e Instalar', command=self._confirmar)
        btn_ok.pack(side='right', padx=(6, 0))

        btn_cancel = ttk.Button(bts, text='✖ Cancelar', command=self.win.destroy)
        btn_cancel.pack(side='right')

    def _confirmar(self):
        self.confirmado = True
        self.win.destroy()


class ProgressoOperacaoDialog:
    """Janela flutuante de progresso para downloads e extrações."""

    def __init__(self, parent, titulo="Processando Download"):
        self.parent = parent
        self.cancel_event = threading.Event()

        self.win = tk.Toplevel(parent)
        self.win.title(titulo)
        self.win.geometry('540x205')
        self.win.minsize(460, 185)
        self.win.transient(parent)
        self.win.grab_set()

        self._montar()

    def _montar(self):
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill='both', expand=True)

        self.lbl_status = ttk.Label(f, text='Iniciando...', font=('Segoe UI', 10), wraplength=490)
        self.lbl_status.pack(anchor='w', pady=(0, 8))

        self.pb = ttk.Progressbar(f, mode='determinate', maximum=100)
        self.pb.pack(fill='x', pady=(0, 8))

        self.lbl_detalhes = ttk.Label(f, text='', font=('Segoe UI', 8), foreground='#666')
        self.lbl_detalhes.pack(anchor='w', pady=(0, 8))

        self._details_open = False
        self._indeterminate = False
        self._last_log_message = None
        self._last_log_time = 0.0
        self.btn_details = ttk.Button(f, text='▸ Mostrar terminal / log', command=self._alternar_detalhes)
        self.btn_details.pack(anchor='w', pady=(0, 8))

        self.frame_log = ttk.Frame(f)
        self.log_text = tk.Text(
            self.frame_log, height=8, wrap='word', state='disabled',
            background='#171a1f', foreground='#d2e7d2', insertbackground='white',
            font=('Consolas', 9), relief='flat'
        )
        log_scroll = ttk.Scrollbar(self.frame_log, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        log_scroll.pack(side='right', fill='y')

        footer = ttk.Frame(f)
        footer.pack(side='bottom', fill='x')
        self.btn_cancelar = ttk.Button(footer, text='Cancelar', command=self._cancelar)
        self.btn_cancelar.pack(side='right')

        self.win.protocol('WM_DELETE_WINDOW', self._cancelar)

    def _alternar_detalhes(self):
        self._details_open = not self._details_open
        if self._details_open:
            self.btn_details.configure(text='▾ Ocultar terminal / log')
            self.frame_log.pack(fill='both', expand=True, pady=(0, 8))
            self.win.geometry('640x440')
        else:
            self.btn_details.configure(text='▸ Mostrar terminal / log')
            self.frame_log.pack_forget()
            self.win.geometry('540x205')

    def _registrar_log(self, text):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f"[{time.strftime('%H:%M:%S')}] {text}\n")
        if int(self.log_text.index('end-1c').split('.')[0]) > 1000:
            self.log_text.delete('1.0', '2.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def _cancelar(self):
        if self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.lbl_status.config(text='Cancelando operação...')
        self.btn_cancelar.config(state='disabled')

    def atualizar(self, baixado, total, msg=None):
        if msg:
            self.lbl_status.config(text=msg)
        if total and total > 0:
            if self._indeterminate:
                self.pb.stop()
                self._indeterminate = False
            pct = min(100.0, (baixado / total) * 100.0)
            self.pb.configure(mode='determinate', value=pct)
            mb_b = baixado / (1024 * 1024)
            mb_t = total / (1024 * 1024)
            self.lbl_detalhes.config(text=f"{mb_b:.1f} MB / {mb_t:.1f} MB  ({pct:.1f}%)")
        else:
            if not self._indeterminate:
                self.pb.configure(mode='indeterminate')
                self.pb.start(12)
                self._indeterminate = True
            mb_b = baixado / (1024 * 1024)
            self.lbl_detalhes.config(text=f"{mb_b:.1f} MB transferidos..." if baixado else 'Em execução · total ainda não informado')
        now = time.monotonic()
        if msg and (msg != self._last_log_message or now - self._last_log_time >= 1.0):
            progress = ''
            if total and total > 0:
                progress = f" — {baixado / (1024 * 1024):.1f}/{total / (1024 * 1024):.1f} MB"
            elif baixado:
                progress = f" — {baixado / (1024 * 1024):.1f} MB transferidos"
            self._registrar_log(msg + progress)
            self._last_log_message = msg
            self._last_log_time = now

    def fechar(self):
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass


class GerenciadorModelosDialog:
    """Painel central de gerenciamento e download de Modelos de IA e ferramentas SAAT."""

    def __init__(self, parent, on_refresh_callback=None):
        self.parent = parent
        self.on_refresh = on_refresh_callback

        self.win = tk.Toplevel(parent)
        self.win.title('🤖 Gerenciador de Modelos de IA & SAAT')
        self.win.geometry('780x560')
        self.win.minsize(700, 480)
        self.win.transient(parent)
        self._wheel_tag = f'ModelCatalogScroll{id(self)}'
        self.win.bind_class(self._wheel_tag, '<MouseWheel>', self._rolar_lista)
        self.win.bind_class(self._wheel_tag, '<Button-4>', self._rolar_lista)
        self.win.bind_class(self._wheel_tag, '<Button-5>', self._rolar_lista)
        self.win.protocol('WM_DELETE_WINDOW', self._fechar)

        self._montar()

    def _montar(self):
        f = ttk.Frame(self.win, padding=12)
        f.pack(fill='both', expand=True)

        lbl = ttk.Label(
            f, text='Modelos de Síntese de Voz (TTS) e Ferramentas do Jogo',
            font=('Segoe UI', 11, 'bold')
        )
        lbl.pack(anchor='w', pady=(0, 4))

        sub = ttk.Label(
            f, text=('Baixe modelos neurais para dublagem ou o toolkit SAAT para extração de áudio. '
                     'A presença dos arquivos não confirma que o runtime Python esteja pronto. '
                     'O catálogo não altera o checkpoint Qwen 1.7B usado pelo engine legado nem seleciona o 0.6B para inferência.'),
            font=('Segoe UI', 9), foreground='#555', wraplength=650
        )
        sub.pack(anchor='w', pady=(0, 10))

        # Frame de lista com scrollbar
        f_list = ttk.Frame(f)
        f_list.pack(fill='both', expand=True, pady=(0, 10))

        canvas = tk.Canvas(f_list, borderwidth=0, highlightthickness=0)
        sb = ttk.Scrollbar(f_list, orient='vertical', command=canvas.yview)
        self.f_scroll = ttk.Frame(canvas)
        self.canvas = canvas
        self._description_labels = []

        self.f_scroll.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
        )
        self._canvas_window = canvas.create_window((0, 0), window=self.f_scroll, anchor='nw')
        canvas.bind('<Configure>', self._redimensionar_conteudo)
        canvas.configure(yscrollcommand=sb.set)

        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self.renderizar_itens()

        # Rodapé
        f_bot = ttk.Frame(f)
        f_bot.pack(fill='x')

        ttk.Button(f_bot, text='Abrir Pasta de Modelos', command=self._abrir_pasta_modelos).pack(side='left')
        ttk.Button(f_bot, text='Recarregar Lista', command=self.renderizar_itens).pack(side='left', padx=6)
        ttk.Button(f_bot, text='Fechar', command=self._fechar).pack(side='right')

    def _redimensionar_conteudo(self, event):
        width = max(1, getattr(event, 'width', self.canvas.winfo_width()))
        self.canvas.itemconfigure(self._canvas_window, width=width)
        wraplength = max(280, width - 52)
        for label in self._description_labels:
            if label.winfo_exists():
                label.configure(wraplength=wraplength)

    def _rolar_lista(self, event):
        if getattr(event, 'num', None) == 4:
            units = -1
        elif getattr(event, 'num', None) == 5:
            units = 1
        else:
            delta = getattr(event, 'delta', 0)
            units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(units, 'units')
        return 'break'

    def _bind_scroll_widgets(self, widget):
        tags = widget.bindtags()
        if self._wheel_tag not in tags:
            widget.bindtags((self._wheel_tag,) + tags)
        for child in widget.winfo_children():
            self._bind_scroll_widgets(child)

    def _fechar(self):
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            try:
                self.win.unbind_class(self._wheel_tag, sequence)
            except tk.TclError:
                pass
        self.win.destroy()

    def _abrir_pasta_modelos(self):
        p = PROJ_ROOT / 'tools' / 'models'
        p.mkdir(parents=True, exist_ok=True)
        abrir_no_sistema(p)

    def renderizar_itens(self):
        for widget in self.f_scroll.winfo_children():
            widget.destroy()
        self._description_labels.clear()

        # 1. Seção SAAT
        saat_info = obter_info_instalacao_saat()
        sfx_cfg = CFG.get('saat_sfx')
        saat_instalado = sfx_cfg and Path(sfx_cfg).is_file()

        q_saat = ttk.LabelFrame(self.f_scroll, text=' 🎮 Ferramenta Original: SAAT (San Andreas Audio Toolkit) ', padding=8)
        q_saat.pack(fill='x', expand=True, pady=4, padx=4)

        status_saat = "✓ Instalado e Configurado" if saat_instalado else "✗ Não Instalado"
        cor_saat = '#2e7d32' if saat_instalado else '#c62828'

        l_top = ttk.Frame(q_saat)
        l_top.pack(fill='x')
        ttk.Label(l_top, text="SAAT SFX & Stream Extractor", font=('Segoe UI', 10, 'bold')).pack(side='left')
        ttk.Label(l_top, text=status_saat, foreground=cor_saat, font=('Segoe UI', 9, 'bold')).pack(side='right')

        desc_s = "Extrai os áudios originais de missão (SCRIPT) do GTA San Andreas para reprodução e comparação."
        if saat_info.get('origem_local'):
            desc_s += f"\n💡 Arquivo local detectado em: {Path(saat_info['origem_local']).name}"
        ttk.Label(q_saat, text=desc_s, font=('Segoe UI', 9), wraplength=640).pack(anchor='w', pady=4)

        f_bts_saat = ttk.Frame(q_saat)
        f_bts_saat.pack(fill='x')

        btn_txt_saat = "Reinstalar SAAT…" if saat_instalado else "📥 Baixar / Instalar SAAT…"
        ttk.Button(f_bts_saat, text=btn_txt_saat, command=self._disparar_download_saat).pack(side='right')
        if saat_instalado:
            ttk.Button(f_bts_saat, text="Abrir Pasta SAAT", command=lambda: abrir_no_sistema(Path(sfx_cfg).parent)).pack(side='right', padx=6)

        # 2. Seção Modelos de IA
        ttk.Separator(self.f_scroll, orient='horizontal').pack(fill='x', pady=8)
        ttk.Label(self.f_scroll, text='Catálogo de Modelos de Síntese de Voz (TTS)', font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=4)

        modelos = listar_modelos()
        for mod in modelos:
            q_mod = ttk.LabelFrame(self.f_scroll, text=f' {mod.categoria}: {mod.nome} ', padding=8)
            q_mod.pack(fill='x', expand=True, pady=4, padx=4)

            top_f = ttk.Frame(q_mod)
            top_f.pack(fill='x')

            st_txt = "✓ Arquivos presentes · runtime à parte" if mod.instalado else "⬇ Arquivos ausentes"
            st_cor = '#2e7d32' if mod.instalado else '#007acc'
            ttk.Label(top_f, text=f"Tamanho: {mod.tamanho_estimado}  |  Requisitos: {mod.requisitos}", font=('Segoe UI', 8), foreground='#666').pack(side='left')
            ttk.Label(top_f, text=st_txt, foreground=st_cor, font=('Segoe UI', 9, 'bold')).pack(side='right')

            description = ttk.Label(q_mod, text=mod.descricao, font=('Segoe UI', 9), wraplength=640)
            description.pack(anchor='w', pady=4)
            self._description_labels.append(description)

            b_f = ttk.Frame(q_mod)
            b_f.pack(fill='x')

            btn_txt = "Substituir arquivos…" if mod.instalado else "📥 Baixar arquivos…"
            ttk.Button(b_f, text=btn_txt, command=lambda m=mod.id: self._disparar_download_modelo(m)).pack(side='right')
            if mod.instalado and mod.caminho_local:
                ttk.Button(b_f, text="Abrir Pasta", command=lambda p=mod.caminho_local: abrir_no_sistema(p)).pack(side='right', padx=6)

        self._redimensionar_conteudo(None)
        self._bind_scroll_widgets(self.f_scroll)

    def _iniciar_operacao(self, titulo, operacao, erro_padrao):
        """Run blocking I/O on a worker; the UI thread alone touches Tk widgets."""
        prog_dlg = ProgressoOperacaoDialog(self.win, titulo=titulo)
        eventos = queue.Queue()

        def pedir_confirmacao(info):
            if prog_dlg.cancel_event.is_set():
                return False
            pronto = threading.Event()
            resposta = {'confirmado': False}
            eventos.put(('confirmacao', info, pronto, resposta))
            while not pronto.wait(0.05):
                if prog_dlg.cancel_event.is_set():
                    return False
            return resposta['confirmado']

        def on_progress(baixado, total, msg=None):
            eventos.put(('progresso', baixado, total, msg))

        def worker():
            try:
                result = operacao(pedir_confirmacao, on_progress, prog_dlg.cancel_event)
            except Exception as exc:
                result = {'ok': False, 'motivo': str(exc) or erro_padrao}
            eventos.put(('fim', result))

        def processar_fila():
            # Schedule the next poll first because a confirmation uses Tk's
            # nested modal loop, which can continue servicing these updates.
            if not prog_dlg.win.winfo_exists():
                return
            prog_dlg.win.after(60, processar_fila)
            while True:
                try:
                    evento = eventos.get_nowait()
                except queue.Empty:
                    break
                if evento[0] == 'progresso':
                    if prog_dlg.win.winfo_exists():
                        prog_dlg.atualizar(evento[1], evento[2], evento[3])
                elif evento[0] == 'confirmacao':
                    _kind, info, pronto, resposta = evento
                    if prog_dlg.cancel_event.is_set() or not self.win.winfo_exists():
                        resposta['confirmado'] = False
                    else:
                        dialog = ConfirmarDownloadDialog(self.win, info)
                        resposta['confirmado'] = dialog.confirmado
                    pronto.set()
                elif evento[0] == 'fim':
                    result = evento[1] if isinstance(evento[1], dict) else {'ok': False, 'motivo': erro_padrao}
                    prog_dlg.fechar()
                    if prog_dlg.cancel_event.is_set() or result.get('cancelado'):
                        return
                    if result.get('ok'):
                        messagebox.showinfo('Sucesso', result.get('motivo', 'Operação concluída.'), parent=self.win)
                        self.renderizar_itens()
                        if self.on_refresh:
                            self.on_refresh()
                    else:
                        messagebox.showerror('Erro', result.get('motivo', erro_padrao), parent=self.win)
                    return

        threading.Thread(target=worker, daemon=True).start()
        # Scheduled from this Tk callback: workers never call root.after or any
        # other Tcl/Tk method directly.
        self.win.after(60, processar_fila)

    def _disparar_download_saat(self):
        def operacao(confirmar, progresso, cancelamento):
            return executar_instalacao_saat(
                confirmar_callback=confirmar,
                on_progress=progresso,
                cancel_event=cancelamento
            )
        self._iniciar_operacao('Instalação do SAAT', operacao, 'Falha na instalação do SAAT.')

    def _disparar_download_modelo(self, model_id):
        def operacao(confirmar, progresso, cancelamento):
            return baixar_modelo(
                model_id,
                confirmar_callback=confirmar,
                on_progress=progresso,
                cancel_event=cancelamento
            )
        self._iniciar_operacao(
            f'Download de Modelo: {model_id}', operacao, 'Falha ao baixar modelo.'
        )
