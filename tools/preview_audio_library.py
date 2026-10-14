"""Read-only visual preview of the library and extraction selector; no export or synthesis."""
import sys
from pathlib import Path
import tkinter as tk
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.ui.app import App
root = tk.Tk()
app = App(root)
app.restaurar_sessao()
root.title('GXT Studio — verificação da biblioteca')
root.geometry('1180x780+60+60')
if '--archives' in sys.argv:
    app.popup_extrair_arquivos()
else:
    app.abrir_biblioteca_audio()
root.mainloop()
