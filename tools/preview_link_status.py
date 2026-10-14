"""Read-only preview of unresolved/caption-only audio status; never extracts audio."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tkinter as tk
import studio.ui.app as ui
from studio.config import CFG
from studio.script.mapping import ref_do_registro
ui.cfg_save=lambda:None
root=tk.Tk();app=ui.App(root)
app.abrir(CFG['sessao']['gxt'])
app.abrir_cmp(str(Path(CFG['game_dir'])/'text'/'american.gxt'))
table='INTRO1' if '--intro' in sys.argv else 'CASIN10'
app.tv_tab.selection_set(table);app.ao_escolher_tabela()
for key in app.g.t[table]['hashes']:
 info=app.info_script(table,key)
 if (table=='INTRO1' and info and not ref_do_registro(info)) or (table=='CASIN10' and not info):
  app.selecionar_iid('%s|%08x'%(table,key));break
root.title('GXT Editor — verificação de vínculos')
root.geometry('1180x760+30+30')
root.protocol('WM_DELETE_WINDOW',root.destroy)
root.mainloop()
