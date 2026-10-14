import unittest
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch
from studio.ui.app import App
from studio.gxt.jamcrc import jamcrc

class NavigationUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.geometry('860x520')
        self.app=App.__new__(App)
        a=self.app
        a.root=self.root
        a.commit=lambda:True
        a.limpar_editor=lambda:None
        a.atualizar_linhas=lambda:None
        a.atualizar_stats=lambda:None
        a.lbl_stats=ttk.Label(self.root)
        a.var_todas=tk.BooleanVar(value=False)
        a.g=SimpleNamespace(order=['MAIN','SWEET1'],t={
          'MAIN':{'hashes':[jamcrc('LANDSTK'),jamcrc('SWEET_1'),123]},
          'SWEET1':{'hashes':[456]}})
        host=ttk.Frame(self.root);host.pack(fill='both',expand=True)
        notebook=ttk.Notebook(host)
        table=ttk.Frame(notebook)
        a.tv_tab=ttk.Treeview(table)
        a.tv_tab.insert('','end',iid='MAIN');a.tv_tab.selection_set('MAIN')
        a.montar_navegacao(host,table,notebook).pack(fill='both',expand=True)
        self.root.update()
        a.preencher_navegacao()
        a.nav_notebook.select(a.nav_page)
        self.root.update()
    def tearDown(self):self.root.destroy()
    def select(self,id):
        self.app.tv_nav.selection_set(id);self.root.update()
    def test_category_exact_pairs_global_override_and_table_return(self):
        a=self.app
        self.select('vehicles')
        self.assertEqual(a.pares_navegacao(),[('MAIN',jamcrc('LANDSTK'))])
        a.var_todas.set(True)
        self.assertEqual(len(a.pares_navegacao()),4)
        a.var_todas.set(False)
        a.nav_notebook.select(0);self.root.update()
        self.assertEqual(a.tabela_atual(),'MAIN')
        self.assertEqual(len(a.pares_navegacao()),3)
    def test_empty_search_and_header_are_empty(self):
        a=self.app
        a.var_nav_search.set('impossible no such mission');self.root.update()
        self.assertEqual(a.pares_navegacao(),[])
        self.assertIsNone(a.tabela_atual())
        a.var_nav_search.set('');self.root.update()
        self.select('section:Categorias')
        self.assertEqual(a.pares_navegacao(),[])
    def test_rejected_commit_restores_category_and_tab(self):
        a=self.app
        self.select('vehicles')
        a.commit=lambda:False
        self.select('other')
        self.assertEqual(a.tv_nav.selection(),('vehicles',))
        a.nav_notebook.select(0);self.root.update()
        self.assertTrue(a.navegacao_categorias())
    def test_bulk_operations_are_guarded_and_geometry_is_bounded(self):
        a=self.app
        self.select('vehicles')
        with patch('studio.ui.category_navigation.messagebox.showinfo') as info,patch('studio.ui.app.abrir_popup_redublar_lote') as batch:
            a.popup_redublar_lote()
            a.extrair_audios_missao()
            self.assertEqual(info.call_count,2)
            batch.assert_not_called()
        self.assertLessEqual(a.tv_nav.winfo_width(),self.root.winfo_width())
        self.assertTrue(a.tv_nav.winfo_ismapped())

if __name__=='__main__':unittest.main()
