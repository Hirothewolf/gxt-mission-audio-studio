# -*- coding: utf-8 -*-
"""
Painel de início: primeiros passos, arquivos recentes e as fontes do jogo.

Aparece no lugar da área vazia enquanto nenhum GXT está aberto e também pode ser
aberto numa janela própria (menu Jogo / Ferramentas).
"""
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from studio.config import CFG
from studio.core.sources import inspecionar_fontes, proximos_passos
from studio.core.utils import abrir_no_sistema

VERDE, VERMELHO, CINZA = '#1a7f37', '#b00020', '#666666'


class StartPanel(ttk.Frame):
    """Visão geral do projeto com ação direta para cada pendência."""

    def __init__(self, master, app, embutido=True):
        super().__init__(master, padding=(16, 12, 4, 8))
        self.app = app
        self.embutido = embutido
        self._wrapped = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        cab = ttk.Frame(self)
        cab.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        cab.columnconfigure(0, weight=1)
        ttk.Label(cab, text='Comece por aqui', font=('Segoe UI', 15, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Button(cab, text='↻ Atualizar', command=self.atualizar).grid(row=0, column=1, sticky='e', padx=(0, 12))
        ttk.Label(cab, foreground=CINZA, text=(
            'O editor cruza quatro fontes do jogo: o script das missões, o texto (GXT), '
            'o áudio das falas e as cutscenes.')).grid(row=1, column=0, columnspan=2, sticky='w')
        # área rolável: em telas baixas o último card não pode ficar inacessível
        fundo = ttk.Style().lookup('TFrame', 'background') or '#d9d9d9'
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=fundo)
        sb = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        sb.grid(row=1, column=1, sticky='ns')
        self.corpo = ttk.Frame(self.canvas)
        self.corpo.columnconfigure(0, weight=1)
        self._janela = self.canvas.create_window((0, 0), window=self.corpo, anchor='nw')
        self.corpo.bind('<Configure>', lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._ao_redimensionar)
        self.canvas.bind('<Enter>', self._ligar_roda)
        self.canvas.bind('<Leave>', self._desligar_roda)
        self.atualizar()

    def _ao_redimensionar(self, e):
        self.canvas.itemconfigure(self._janela, width=e.width)
        self._ajustar_quebra()

    def _ligar_roda(self, _e=None):
        self.canvas.bind_all('<MouseWheel>', self._roda)
        self.canvas.bind_all('<Button-4>', lambda _e: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind_all('<Button-5>', lambda _e: self.canvas.yview_scroll(3, 'units'))

    def _desligar_roda(self, _e=None):
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.canvas.unbind_all(seq)

    def _roda(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units')

    # ---------- dados
    def _estado(self):
        app = self.app
        game = CFG.get('game_dir') or ''
        saat_ok = False
        try:
            saat_ok = bool(app.jogo and app.jogo.saat().get('sfx'))
        except Exception:
            saat_ok = bool(CFG.get('saat_sfx') and Path(CFG['saat_sfx']).is_file())
        return game, saat_ok

    def atualizar(self):
        """Reinspeciona o disco e redesenha o painel."""
        for w in self.corpo.winfo_children():
            w.destroy()
        self._wrapped = []
        game, saat_ok = self._estado()
        passos = proximos_passos(game, self.app.g, self.app.c, saat_ok)
        fontes = inspecionar_fontes(game, CFG.get('modloader_mod'))
        self._desenhar_passos(passos, game)
        self._desenhar_recentes()
        self._desenhar_fontes(fontes)
        self._ajustar_quebra()

    # ---------- desenho
    def _rotulo(self, pai, texto, **kw):
        lb = ttk.Label(pai, text=texto, justify='left', **kw)
        self._wrapped.append(lb)
        return lb

    def _desenhar_passos(self, passos, game):
        app = self.app
        box = ttk.LabelFrame(self.corpo, text='Primeiros passos', padding=8)
        box.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        box.columnconfigure(1, weight=1)
        acoes = [
            [('Detectar', self._detectar_jogo), ('Escolher…', self._escolher_jogo)],
            [('Abrir GXT…', lambda: self._depois(app.abrir))],
            [('Usar o do jogo', self._comparar_com_jogo), ('Escolher…', lambda: self._depois(app.abrir_cmp))],
            [('Configurar SAAT…', app.popup_gerenciador_modelos)],
        ]
        for i, ((texto, feito), btns) in enumerate(zip(passos, acoes)):
            ttk.Label(box, text='✓' if feito else '○', foreground=VERDE if feito else CINZA,
                      font=('Segoe UI', 11, 'bold'), width=2).grid(row=i, column=0, sticky='w')
            ttk.Label(box, text=texto, foreground='' if not feito else CINZA).grid(row=i, column=1, sticky='w')
            fr = ttk.Frame(box)
            fr.grid(row=i, column=2, sticky='e', pady=1)
            for t, cmd in btns:
                b = ttk.Button(fr, text=t, command=cmd)
                b.pack(side='left', padx=(4, 0))
                if t == 'Usar o do jogo' and not self._gxt_do_jogo(game):
                    b.state(['disabled'])

    def _desenhar_recentes(self):
        recentes = [r for r in (CFG.get('recentes') or []) if Path(r).is_file()][:4]
        if not recentes:
            return
        box = ttk.LabelFrame(self.corpo, text='Continuar de onde parou', padding=8)
        box.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        box.columnconfigure(0, weight=1)
        for i, r in enumerate(recentes):
            p = Path(r)
            nome = '%s  ›  %s' % (p.parent.parent.name or p.parent.name, p.name)
            ttk.Label(box, text=nome, foreground=VERDE if i == 0 else '').grid(row=i, column=0, sticky='w')
            ttk.Button(box, text='Abrir', command=lambda r=r: self._depois(lambda: self.app.abrir(r))
                       ).grid(row=i, column=1, sticky='e', pady=1)

    def _desenhar_fontes(self, fontes):
        app = self.app
        box = ttk.LabelFrame(self.corpo, text='Fontes do jogo', padding=8)
        box.grid(row=2, column=0, sticky='nsew')
        box.columnconfigure(1, weight=1)
        self.corpo.rowconfigure(2, weight=1)
        tem_g = app.g is not None
        sem_jogo = not any(f.ok or f.caminho for f in fontes) and all(
            'não configurada' in ' '.join(f.avisos) for f in fontes)
        if sem_jogo:
            self._rotulo(box, 'Defina a pasta do GTA San Andreas (passo 1) para verificar estas fontes.',
                         foreground=VERMELHO).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        extra = {
            'scm': ('Análise do SCM…', app.abrir_fontes_jogo, tem_g),
            'texto': ('Abrir no editor', lambda: self._abrir_fonte_texto(fontes), True),
            'audio': ('Biblioteca de áudio…', app.abrir_biblioteca_audio, tem_g),
            'cutscenes': ('Lote automático…', app.abrir_lote_cutscenes, tem_g),
        }
        for i, f in enumerate(fontes):
            ttk.Label(box, text='✓' if f.ok else '✗', foreground=VERDE if f.ok else VERMELHO,
                      font=('Segoe UI', 11, 'bold'), width=2).grid(row=1 + i * 3, column=0, sticky='nw')
            ttk.Label(box, text=f.titulo, font=('Segoe UI', 10, 'bold')).grid(row=1 + i * 3, column=1, sticky='w')
            fr = ttk.Frame(box)
            fr.grid(row=1 + i * 3, column=2, sticky='e')
            texto_extra, cmd_extra, habilitado = extra[f.chave]
            b = ttk.Button(fr, text=texto_extra, command=cmd_extra)
            if not (habilitado and f.ok):
                b.state(['disabled'])
            b.pack(side='left', padx=2)
            bp = ttk.Button(fr, text='Abrir pasta', command=lambda p=f.pasta: self._abrir_pasta(p))
            if not f.pasta:
                bp.state(['disabled'])
            bp.pack(side='left', padx=2)
            if f.ok:
                linha = '%s\n%s' % (f.caminho, f.detalhe) if f.detalhe else str(f.caminho)
                self._rotulo(box, linha, foreground=CINZA).grid(row=1 + i * 3 + 1, column=1, columnspan=2, sticky='w')
            elif sem_jogo:
                self._rotulo(box, 'Esperado: ' + f.relativo, foreground=CINZA
                             ).grid(row=1 + i * 3 + 1, column=1, columnspan=2, sticky='w')
            else:
                self._rotulo(box, '; '.join(f.avisos) + '\nEsperado: ' + f.relativo,
                             foreground=VERMELHO).grid(row=1 + i * 3 + 1, column=1, columnspan=2, sticky='w')
            self._rotulo(box, f.dica).grid(row=1 + i * 3 + 2, column=1, columnspan=2, sticky='w', pady=(0, 8))

    def _ajustar_quebra(self, _e=None):
        largura = max(self.winfo_width() - 330, 280)
        for lb in self._wrapped:
            try:
                lb.configure(wraplength=largura)
            except tk.TclError:
                pass

    # ---------- ações
    def _depois(self, fn):
        """Executa a ação e redesenha (o diálogo de arquivo é síncrono)."""
        try:
            fn()
        finally:
            self.after(100, self._seguro_atualizar)

    def _seguro_atualizar(self):
        try:
            if self.winfo_exists():
                self.atualizar()
        except tk.TclError:
            pass

    def _detectar_jogo(self):
        self._depois(lambda: self.app.detectar_gta_automaticamente(silencioso=False))

    def _escolher_jogo(self):
        self._depois(self.app.configurar_gta_root)

    @staticmethod
    def _gxt_do_jogo(game):
        if not game:
            return None
        p = Path(game) / 'text' / 'american.gxt'
        return p if p.is_file() else None

    def _comparar_com_jogo(self):
        p = self._gxt_do_jogo(CFG.get('game_dir'))
        if not p:
            messagebox.showinfo('Comparação', 'american.gxt não encontrado na pasta do jogo.', parent=self)
            return
        self._depois(lambda: self.app.abrir_cmp(str(p)))

    def _abrir_fonte_texto(self, fontes):
        f = next((x for x in fontes if x.chave == 'texto'), None)
        if f and f.caminho and f.caminho.is_file():
            self._depois(lambda: self.app.abrir(str(f.caminho)))

    def _abrir_pasta(self, p):
        try:
            abrir_no_sistema(p)
        except OSError as exc:
            messagebox.showerror('Abrir pasta', str(exc), parent=self)


def abrir_guia(app):
    """Abre (ou traz à frente) o painel de início numa janela própria."""
    atual = getattr(app, '_guia_win', None)
    try:
        if atual is not None and atual.winfo_exists():
            atual.lift()
            atual.painel.atualizar()
            return atual
    except tk.TclError:
        pass
    win = tk.Toplevel(app.root)
    win.title('Guia do projeto — fontes do jogo')
    win.geometry('820x620')
    win.transient(app.root)
    win.painel = StartPanel(win, app, embutido=False)
    win.painel.pack(fill='both', expand=True)
    app._guia_win = win
    return win
