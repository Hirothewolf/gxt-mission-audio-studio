"""Read-only classic-window smoke preview; no synthesis or automatic analysis."""
import sys
from pathlib import Path
import tkinter as tk
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio.ui.app as ui
ui.cfg_save = lambda: None
root = tk.Tk()
app = ui.App(root)
app.salvar_sessao = lambda: None
app.restaurar_sessao()
root.title('GXT Studio — verificação da identificação de voz')
app.abrir_identidade_voz()
root.mainloop()
