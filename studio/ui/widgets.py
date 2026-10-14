# -*- coding: utf-8 -*-
"""
Widgets reutilizáveis de interface gráfica (Tooltip, janelas de texto e helpers Tkinter).
"""
import tkinter as tk
from tkinter import ttk


class Tooltip:
    """Tooltip informativo que surge ao passar o cursor sobre um widget."""

    def __init__(self, widget, texto, atraso=550):
        self.w, self.texto, self.atraso, self.tip, self.job = widget, texto, atraso, None, None
        widget.bind('<Enter>', self._agendar, add='+')
        widget.bind('<Leave>', self._esconder, add='+')
        widget.bind('<ButtonPress>', self._esconder, add='+')

    def _agendar(self, e=None):
        self._esconder()
        self.job = self.w.after(self.atraso, self._mostrar)

    def _mostrar(self):
        try:
            x = self.w.winfo_rootx() + 12
            y = self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry('+%d+%d' % (x, y))
            tk.Label(
                self.tip, text=self.texto, justify='left', background='#ffffe1', relief='solid',
                borderwidth=1, padx=6, pady=4, wraplength=420, font=('Segoe UI', 9)
            ).pack()
        except Exception:
            pass

    def _esconder(self, e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def janela_texto(parent, titulo, texto, botoes=(), largura=780, altura=440):
    """Exibe uma janela com área de texto monoespacada e botões de ação na base."""
    win = tk.Toplevel(parent)
    win.title(titulo)
    win.geometry(f'{largura}x{altura}')
    win.transient(parent)

    txt = tk.Text(win, font=('Consolas', 10), wrap='none')
    txt.insert('1.0', texto)
    txt.config(state='disabled')
    txt.pack(fill='both', expand=True, padx=8, pady=8)

    bt = ttk.Frame(win, padding=(8, 0, 8, 8))
    bt.pack(fill='x')
    for rot, cmd in botoes:
        ttk.Button(bt, text=rot, command=lambda c=cmd: c(win)).pack(side='left', padx=(0, 6))
    ttk.Button(bt, text='Fechar', command=win.destroy).pack(side='right')
    win.bind('<Escape>', lambda e: win.destroy())
    return win
