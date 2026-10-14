"""Alternative navigation UI; preserves existing table-oriented editing and audio flows."""
import tkinter as tk
from tkinter import ttk, messagebox
from studio.ui.navigation_catalog import NavigationCatalog

class CategoryNavigationMixin:
    def montar_navegacao(self, parent, table_frame, notebook):
        self.nav_catalog = NavigationCatalog()
        self.nav_groups = {}
        self._nav_previous = None
        self._nav_previous_tab = None
        self._nav_restoring = False
        self.nav_notebook = notebook
        self.nav_notebook.configure(width=330)
        self.nav_notebook.add(table_frame, text='Tabelas GXT')
        self.nav_page = ttk.Frame(self.nav_notebook)
        self.nav_notebook.add(self.nav_page, text='Missões e categorias')
        self.var_nav_search = tk.StringVar()
        ttk.Label(self.nav_page, text='Encontrar missão / categoria:').pack(anchor='w', padx=5, pady=(5,0))
        ttk.Entry(self.nav_page, textvariable=self.var_nav_search).pack(fill='x',padx=5,pady=4)
        frame=ttk.Frame(self.nav_page)
        frame.pack(fill='both',expand=True)
        self.tv_nav=ttk.Treeview(frame,columns=('n',),selectmode='browse')
        self.tv_nav.heading('#0',text='Missão / categoria')
        self.tv_nav.heading('n',text='Linhas')
        self.tv_nav.column('#0',width=245,minwidth=120)
        self.tv_nav.column('n',width=55,minwidth=45,stretch=False,anchor='e')
        sb=ttk.Scrollbar(frame,orient='vertical',command=self.tv_nav.yview)
        self.tv_nav.configure(yscrollcommand=sb.set)
        self.tv_nav.pack(side='left',fill='both',expand=True)
        sb.pack(side='right',fill='y')
        self.nav_reason=ttk.Label(self.nav_page,text='Abra um GXT para navegar.',wraplength=300,justify='left')
        self.nav_reason.pack(fill='x',padx=5,pady=5)
        ttk.Label(self.nav_page,text='Sobreposição permitida · chaves originais preservadas',wraplength=300,foreground='#555').pack(fill='x',padx=5,pady=(0,5))
        self.tv_nav.bind('<<TreeviewSelect>>',self.ao_escolher_categoria)
        self.nav_notebook.bind('<<NotebookTabChanged>>',self.ao_mudar_navegacao)
        self.var_nav_search.trace_add('write',lambda *a:self.filtrar_navegacao())
        return self.nav_notebook

    def navegacao_categorias(self):
        return hasattr(self,'nav_notebook') and self.nav_notebook.select()==str(self.nav_page)

    def categoria_atual(self):
        if not self.navegacao_categorias():return None
        selection=self.tv_nav.selection()
        return self.nav_groups.get(selection[0]) if selection else None

    def pares_navegacao(self):
        category=self.categoria_atual()
        if self.navegacao_categorias() and not self.var_todas.get():
            return category['members'] if category is not None else []
        tab=self.tabela_atual()
        tabs=self.g.order if tab=='*' or self.var_todas.get() else [tab] if tab else []
        return [(t,h) for t in tabs for h in self.g.t[t]['hashes']]

    def preencher_navegacao(self):
        self.nav_groups=self.nav_catalog.for_gxt(self.g) if self.g else {}
        self.filtrar_navegacao()

    def filtrar_navegacao(self):
        previous=self.tv_nav.selection()
        query=self.var_nav_search.get().strip().casefold()
        self.tv_nav.delete(*self.tv_nav.get_children())
        for id,group in self.nav_groups.items():
            table_names=' '.join(group.get('tables',[]))
            if query and query not in (group['label']+' '+table_names).casefold():continue
            section=group['section']
            if section and not self.tv_nav.exists('section:'+section):
                self.tv_nav.insert('','end',iid='section:'+section,text=section,open=True)
            label=group['label']
            if group.get('tables'):label+=' · '+', '.join(group['tables'])
            self.tv_nav.insert('section:'+section if section else '','end',iid=id,text=label,values=(len(group['members']),))
        target=previous[0] if previous and self.tv_nav.exists(previous[0]) else 'all'
        if self.tv_nav.exists(target):self.tv_nav.selection_set(target)
        else:
            available=[id for id in self.nav_groups if self.tv_nav.exists(id)]
            if available:self.tv_nav.selection_set(available[0])
            elif self.navegacao_categorias():
                self.nav_reason.config(text='Nenhum resultado para este nome.')
                self.atualizar_linhas()

    def ao_escolher_categoria(self,event=None):
        if not self.navegacao_categorias() or self._nav_restoring:return
        category=self.categoria_atual()
        if not self.commit():
            self._nav_restoring=True
            if self._nav_previous and self.tv_nav.exists(self._nav_previous):
                self.tv_nav.selection_set(self._nav_previous)
            else:self.tv_nav.selection_remove(*self.tv_nav.selection())
            self.root.after_idle(lambda:setattr(self,'_nav_restoring',False))
            return
        selection=self.tv_nav.selection()
        self._nav_previous=selection[0] if selection else None
        self.limpar_editor()
        self.nav_reason.config(text=category['evidence'] if category else 'Selecione uma missão ou categoria abaixo do cabeçalho.')
        self.atualizar_linhas()

    def ao_mudar_navegacao(self,event=None):
        if not hasattr(self,'lbl_stats') or self._nav_restoring:return
        if not self.commit():
            if self._nav_previous_tab:
                self._nav_restoring=True
                self.nav_notebook.select(self._nav_previous_tab)
                self.root.after_idle(lambda:setattr(self,'_nav_restoring',False))
            return
        self._nav_previous_tab=self.nav_notebook.select()
        self.limpar_editor()
        self.atualizar_linhas()

    def exigir_modo_tabela(self):
        if not self.navegacao_categorias():return True
        messagebox.showinfo('Escolha uma tabela GXT','Esta ação processa uma tabela inteira. Abra a aba Tabelas GXT e escolha a tabela desejada. Para uma fala, selecione a linha nesta lista e use as ações de revisão ou dublagem individual.')
        return False

    def exportar_csv_categoria(self):
        import csv
        from tkinter import filedialog
        from studio.gxt.codec import decode_text
        category=self.categoria_atual()
        if category is None:return
        path=filedialog.asksaveasfilename(defaultextension='.csv',filetypes=[('CSV','*.csv')],initialfile='categoria.csv')
        if not path:return
        with open(path,'w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.writer(stream,delimiter=';')
            writer.writerow(['tabela','id','chave','audio_id','comparacao','atual'])
            for tab,h in category['members']:
                reference=self.ref(tab,h)
                audio=self.script_db.get((tab,h)) or self.script_db.get(('ANY',h)) or {}
                writer.writerow([tab,'0x%08X'%h,self.nomes_tabela(tab,forcar=False).get(h,''),audio.get('stable_audio_id',''),
                    decode_text(reference) if reference is not None else '',decode_text(self.g.t[tab]['cur'][h])])
        self.status('CSV exportado: '+category['label'])
