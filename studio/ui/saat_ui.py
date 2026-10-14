# -*- coding: utf-8 -*-
"""
Integração de menus e interface para o GTA San Andreas, ModLoader e SAAT.
"""
import re
import threading
import subprocess
from pathlib import Path
from studio.saat.detection import detectar_pastas_jogo, achar_saat
from studio.saat.audio import GameAudio, MOD_PADRAO
from studio.ui.dialogs import (
    ConfirmarDownloadDialog, ProgressoOperacaoDialog, GerenciadorModelosDialog
)
from studio.saat.downloader import executar_instalacao_saat


def texto_status(ga):
    """Gera o relatório textual do status do jogo, SAAT, ModLoader e cache."""
    L = []
    ok = lambda b: '✓' if b else '✗'
    i = ga.info
    L.append('%s Pasta do jogo: %s' % (ok(i['ok']), i['pasta'] or '—'))
    if i['pasta'] and not i['ok']:
        L.append('   faltando: ' + ', '.join(i['faltando']))
    if i['ok']:
        L.append('   %s gta_sa.exe: %s' % (ok(i['exe']), i['exe'] or 'não encontrado'))
        L.append('   %s american.gxt do jogo base: %s' % (ok(i['gxt']), i['gxt'] or 'não encontrado'))
        if i.get('script'):
            sz = Path(i['script']).stat().st_size / (1024 * 1024) if Path(i['script']).is_file() else 0
            L.append('   %s audio\\SFX\\SCRIPT do jogo base (ORIGEM LIMPA): %s (%.1f MB)' % (ok(i['script']), i['script'], sz))
        L.append('   %s modloader: %s' % (ok(i['modloader']), i['modloader'] or 'pasta não existe'))
    ex = ga.saat() if i['ok'] else achar_saat(None)
    L.append('%s saat_sfx: %s' % (ok(ex['sfx']), ex['sfx'] or 'não encontrado'))
    L.append('%s saat_stream: %s' % (ok(ex['stream']), ex['stream'] or 'não encontrado'))
    if i['ok']:
        e = ga.estado_cache()
        L.append('')
        L.append('─── Extração do SAAT (Áudio Original Limpo) ───')
        L.append('%s Áudio original extraído: %d WAV(s) indexados%s' % (
            ok(e['indexado'] and ga.indice), len(ga.indice), '   ⚠ o banco do jogo mudou desde a extração — reindexar' if e['desatualizado'] else ''))
        L.append('   Pasta de cache: %s' % ga.cache_dir())
        if ga.indice:
            L.append('   Status: Extraído do jogo original e pronto para reprodução (F5)')

        L.append('')
        L.append('─── ModLoader & Mods ───')
        mods = ga.mods()
        L.append('Mods encontrados no ModLoader: %s' % (', '.join(mods) or '(nenhum)'))
        mod_ativo = ga.mod_ativo() if hasattr(ga, 'mod_ativo') else (ga.cfg.get('modloader_mod') or MOD_PADRAO)
        L.append('Mod ativo / destino: %s' % mod_ativo)
        mod_dir = ga.modloader_dir() / mod_ativo if ga.modloader_dir() else None
        if mod_dir and mod_dir.is_dir():
            sfx_pak = mod_dir / 'audio' / 'SFX' / 'SCRIPT'
            if sfx_pak.is_file():
                sz_mod = sfx_pak.stat().st_size / (1024 * 1024)
                L.append('   Arquivo SCRIPT do mod: %s (%.1f MB, áudio do mod)' % (sfx_pak, sz_mod))
            wavs_soltos = len(ga.instalados_para_mod(mod_ativo)) if hasattr(ga, 'instalados_para_mod') else 0
            if wavs_soltos:
                L.append('   WAVs soltos instalados no mod: %d arquivo(s)' % wavs_soltos)
            gxt_mod = mod_dir / 'text' / 'american.gxt'
            if gxt_mod.is_file():
                L.append('   GXT do mod: %s' % gxt_mod)
        else:
            L.append('   (Pasta do mod ainda não criada no ModLoader)')
    return '\n'.join(L)


def install_ui(app, tk, ttk, messagebox, filedialog, cfg, cfg_save, proj_root, log=None):
    """Liga os recursos do jogo/SAAT/ModLoader aos menus da aplicação."""
    ga = GameAudio(cfg, cfg_save, Path(proj_root) / '.cache' / 'saat', Path(proj_root) / 'backups' / 'modloader',
                   Path(proj_root) / 'workspace' / 'modloader_manifest.json', log=log)
    app.jogo = ga
    estado = {'cancel': None}

    def janela_texto(titulo, texto, botoes=()):
        win = tk.Toplevel(app.root)
        win.title(titulo)
        win.geometry('900x540')
        win.transient(app.root)
        t = tk.Text(win, font=('Consolas', 10), wrap='none')
        t.insert('1.0', texto)
        t.config(state='disabled')
        t.pack(fill='both', expand=True, padx=8, pady=8)
        bt = ttk.Frame(win, padding=(8, 0, 8, 8))
        bt.pack(fill='x')
        for rot, cmd in botoes:
            ttk.Button(bt, text=rot, command=lambda c=cmd: c(win)).pack(side='left', padx=(0, 6))
        ttk.Button(bt, text='Fechar', command=win.destroy).pack(side='right')
        win.bind('<Escape>', lambda e: win.destroy())
        return win

    def refresh():
        app.original_audio_cache.clear()
        ga.carregar_indice()
        ga._ml_escaneado = False
        ga.escanear_modloader()
        app.limpar_cache_dublado()
        app.reindexar_audios_base(silencioso=True)
        app.atualizar_linhas()
        if app.sel:
            app._carregar_linha(*app.sel)

    def baixar_saat_com_confirmacao():
        def pedir_confirmacao(info):
            dialog = ConfirmarDownloadDialog(app.root, info)
            return dialog.confirmado

        prog_dlg = ProgressoOperacaoDialog(app.root, titulo="Instalação do SAAT")

        def worker():
            def on_prog(b, t, msg=None):
                app.ui(lambda: prog_dlg.atualizar(b, t, msg))

            res = executar_instalacao_saat(
                confirmar_callback=pedir_confirmacao,
                on_progress=on_prog,
                cancel_event=prog_dlg.cancel_event
            )

            def fim():
                prog_dlg.fechar()
                if res.get('cancelado'):
                    return
                if res.get('ok'):
                    messagebox.showinfo('SAAT Configurado', res['motivo'], parent=app.root)
                    app.status("SAAT instalado e configurado!")
                else:
                    messagebox.showerror('Erro SAAT', res.get('motivo', 'Falha ao instalar SAAT.'), parent=app.root)

            app.ui(fim)

        threading.Thread(target=worker, daemon=True).start()

    def escolher_pasta():
        cands = detectar_pastas_jogo([cfg.get('game_dir')])
        pasta = None
        if len(cands) == 1:
            if messagebox.askyesno('Pasta do jogo', 'Encontrei o GTA San Andreas em:\n\n%s\n\nUsar esta pasta?' % cands[0]):
                pasta = cands[0]
        elif len(cands) > 1:
            lista = '\n'.join('%d) %s' % (i + 1, c) for i, c in enumerate(cands))
            if messagebox.askyesno('Pasta do jogo', 'Encontrei %d instalações:\n\n%s\n\nUsar a primeira?\n(Não = escolher manualmente)' % (len(cands), lista)):
                pasta = cands[0]
        if pasta is None:
            p = filedialog.askdirectory(title='Pasta do GTA San Andreas (a que tem gta_sa.exe e audio\\)')
            if not p:
                return
            pasta = p
        info = ga.definir_pasta(pasta)
        if not info['ok']:
            messagebox.showerror('Pasta do jogo', 'Essa pasta não parece ser a raiz do GTA SA.\nFaltando: %s' % ', '.join(info['faltando']))
            return
        app.status('Pasta do jogo: %s' % info['pasta'])

        # Se SAAT não estiver instalado, oferece o download/configuração automática com confirmação
        ex = ga.saat()
        if not ex['sfx']:
            if messagebox.askyesno(
                'SAAT Necessário',
                'O executável saat_sfx.exe não foi encontrado.\n\nDeseja que o programa localize/baixe e configure o SAAT automaticamente?'
            ):
                baixar_saat_com_confirmacao()
                return

        if not ga.indice and messagebox.askyesno(
                'Áudio do Jogo / Mod', 'Deseja extrair os áudios SCRIPT com o SAAT agora?\n\n'
                'O SAAT pode extrair tanto o áudio do jogo original limpo quanto o áudio do mod no ModLoader.\n'
                'É seguro e somente leitura: tudo é exportado para o cache do projeto.'):
            indexar('ambos', False)
        else:
            refresh()

    def indexar(alvo='ambos', forcar=False):
        if getattr(app, 'archive_busy', False):
            app.status('Uma extração já está em andamento.')
            return
        if not ga.info['ok']:
            escolher_pasta()
            if not ga.info['ok']:
                return

        ex = ga.saat()
        if not ex['sfx']:
            if messagebox.askyesno(
                'SAAT Não Encontrado',
                'O executável saat_sfx.exe não foi encontrado para extrair o áudio.\n\nDeseja baixar/instalar o SAAT automaticamente com confirmação?'
            ):
                baixar_saat_com_confirmacao()
            return

        win = tk.Toplevel(app.root)
        titulos = {
            'ambos': 'SAAT — extraindo áudios: Jogo Base + Mod no ModLoader',
            'jogo': 'SAAT — extraindo áudio original do jogo base (somente leitura)',
            'mod': f'SAAT — extraindo áudio do mod ativo ({ga.mod_ativo()})'
        }
        win.title(titulos.get(alvo, 'SAAT — extração de áudio'))
        win.geometry('900x500')
        win.transient(app.root)
        txt = tk.Text(win, font=('Consolas', 9), bg='#181818', fg='#00e676')
        txt.pack(fill='both', expand=True, padx=8, pady=8)
        bt = ttk.Frame(win, padding=(8, 0, 8, 8))
        bt.pack(fill='x')
        cancel = threading.Event()
        app.archive_busy = True
        app.archive_cancel = cancel
        estado['cancel'] = cancel

        script_orig = ga.info.get('script')
        sz_mb = Path(script_orig).stat().st_size / (1024 * 1024) if script_orig and Path(script_orig).is_file() else 0
        mod_ativo = ga.mod_ativo()
        mod_dir = ga.modloader_dir() / mod_ativo if ga.modloader_dir() else None
        mod_script = mod_dir / 'audio' / 'SFX' / 'SCRIPT' if mod_dir else None
        sz_mod = mod_script.stat().st_size / (1024 * 1024) if mod_script and mod_script.is_file() else 0

        txt.insert('end', '=======================================================================\n')
        txt.insert('end', ' EXTRAÇÃO DE ÁUDIO VIA SAAT (SAN ANDREAS AUDIO TOOLKIT)\n')
        txt.insert('end', '=======================================================================\n')
        txt.insert('end', f' • Executável SAAT: {ex.get("sfx")}\n')
        if alvo in ('ambos', 'jogo'):
            txt.insert('end', f' • Jogo Base (Origem Limpa): {script_orig} ({sz_mb:.1f} MB)\n')
            txt.insert('end', f'   Destino do cache original: {ga.cache_dir() / "export" / "SCRIPT"}\n')
        if alvo in ('ambos', 'mod'):
            txt.insert('end', f' • Mod no ModLoader ({mod_ativo}): {mod_script if mod_script and mod_script.is_file() else "(WAVs soltos no mod)"} ({sz_mod:.1f} MB)\n')
            txt.insert('end', f'   Destino do cache do mod: {ga.cache_dir_mod(mod_ativo) / "export"}\n')
        txt.insert('end', '-----------------------------------------------------------------------\n\n')

        def fechar():
            if t.is_alive():
                if not messagebox.askyesno('SAAT', 'Cancelar a extração e fechar?', parent=win):
                    return
                cancel.set()
                ga.cancelar()
            win.destroy()
        win.protocol('WM_DELETE_WINDOW', fechar)
        ttk.Button(bt, text='⏹ Cancelar', command=lambda: (cancel.set(), ga.cancelar())).pack(side='left')
        ttk.Button(bt, text='Fechar', command=fechar).pack(side='right')

        def linha(l):
            app.ui(lambda: (txt.insert('end', l + '\n'), txt.see('end')) if win.winfo_exists() else None)

        def trabalho():
            try:
                if alvo == 'ambos':
                    r = ga.indexar_ambos(on_line=linha, cancel=cancel, forcar=forcar)
                elif alvo == 'mod':
                    r = ga.indexar_mod(ga.mod_ativo(), 'SCRIPT', on_line=linha, cancel=cancel, forcar=forcar)
                else:
                    r = ga.indexar('SCRIPT', on_line=linha, cancel=cancel, forcar=forcar)
            except Exception as ex:
                r = {'ok': False, 'motivo': str(ex)}

            def fim():
                app.archive_busy = False
                if not win.winfo_exists():
                    return
                if r.get('ok'):
                    txt.insert('end', '\n[OK] Extração e indexação concluídas com sucesso!\n')
                    txt.insert('end', f'  • WAVs do Jogo Base indexados: {len(ga.indice)}\n')
                    txt.insert('end', f'  • WAVs do Mod ({mod_ativo}) indexados: {len(ga.indice_mod)}\n')
                    txt.insert('end', 'Arquivos físicos extraídos. O vínculo com legendas depende do catálogo; consulte a Biblioteca de áudio para os slots sem vínculo.\n')
                    app.status(f'SAAT: {len(ga.indice)} originais e {len(ga.indice_mod)} do mod indexados com sucesso.')
                    refresh()
                else:
                    txt.insert('end', '\n[ERRO / AVISO] %s\n%s\n' % (r.get('motivo') or ('código %s' % r.get('rc')), '\n'.join(r.get('log') or [])))
                txt.see('end')
            app.ui(fim)
        t = threading.Thread(target=trabalho, daemon=True)
        t.start()

    def status():
        janela_texto('Jogo / SAAT / ModLoader', texto_status(ga), [
            ('Escolher pasta do jogo…', lambda w: (w.destroy(), escolher_pasta())),
            ('Baixar / Reinstalar SAAT…', lambda w: (w.destroy(), baixar_saat_com_confirmacao())),
            ('Copiar', lambda w: app._copiar(texto_status(ga), 'Status')),
        ])

    def abrir_gxt_do_jogo():
        g = ga.info.get('gxt')
        if not g:
            messagebox.showinfo('american.gxt', 'Configure a pasta do jogo primeiro (Jogo > Detectar / escolher pasta).')
            return
        app.abrir_cmp(str(g))

    def nome_mod():
        from tkinter import simpledialog
        n = simpledialog.askstring('Mod do ModLoader', 'Nome da pasta do mod (modloader\\<nome>\\…):',
                                   initialvalue=cfg.get('modloader_mod') or MOD_PADRAO, parent=app.root)
        if n and re.fullmatch(r'[\w .\-]{1,60}', n.strip()) and not n.strip().startswith('.'):
            cfg['modloader_mod'] = n.strip()
            cfg_save()
            ga._ml_escaneado = False
            app.status('Mod do ModLoader: %s' % n.strip())
        elif n:
            messagebox.showerror('Nome inválido', 'Use letras, números, espaço, ponto, hífen ou _ (sem começar com ponto).')

    def coletar(escopo):
        itens, ignoradas = [], 0
        if escopo == 'fala':
            alvo = [app.sel] if app.sel else []
        else:
            tab = app.tabela_atual()
            if not tab or tab == '*':
                return None, 0
            alvo = [(tab, h) for h in app.g.t[tab]['hashes']]
        for tab, h in alvo:
            ainfo = app.info_script(tab, h)
            if not ainfo:
                continue
            nome = app.nomes.get(tab, {}).get(h) or ainfo.get('event_name') or '%08X' % h
            if escopo == 'revisadas' and not app.ws.get('%s|%08x' % (tab, h)).get('rev'):
                continue
            dub = app.achar_audio_dublado(tab, h, nome)
            if not dub:
                ignoradas += 1
                continue
            itens.append({'rotulo': '%s/%s' % (tab, nome), 'ref': ga.ref_para(ainfo), 'dub': dub})
        return itens, ignoradas

    def instalar(escopo):
        if not ga.info['ok']:
            escolher_pasta()
            if not ga.info['ok']:
                return
        itens, ignoradas = coletar(escopo)
        if itens is None:
            messagebox.showinfo('Instalar', 'Selecione uma missão (tabela) específica na lista da esquerda.')
            return
        mod = cfg.get('modloader_mod') or MOD_PADRAO
        plano = ga.planejar(itens, mod)
        ok = [e for e in plano if not e['problemas']]
        ruins = [e for e in plano if e['problemas']]
        L = ['SIMULAÇÃO — nada foi gravado ainda.', 'Mod: %s   →   %s' % (mod, ga.modloader_dir() / mod), '',
             '%d fala(s) a instalar · %d com problema · %d sem dublagem (ignoradas)' % (len(ok), len(ruins), ignoradas),
             'Os arquivos do jogo NÃO são alterados: tudo vai para a pasta do mod (o ModLoader aplica ao iniciar o jogo).', '']
        for e in ok:
            extra = []
            if e.get('precisa_converter'):
                extra.append('converte p/ PCM16 mono%s' % (' %d Hz' % e['taxa_orig'] if e.get('taxa_orig') else ''))
            extra += e.get('avisos', [])
            L.append('  [%s] %s  →  %s%s' % (e['acao'], e['rotulo'], Path(e['destino']).relative_to(ga.modloader_dir() / mod),
                                             ('   (' + '; '.join(extra) + ')') if extra else ''))
        if ruins:
            L += ['', 'COM PROBLEMA (não serão instaladas):']
            L += ['  %s: %s' % (e['rotulo'], '; '.join(e['problemas'])) for e in ruins[:40]]

        def aplicar(win):
            if not ok:
                return
            if not messagebox.askyesno('Instalar no ModLoader', 'Gravar %d arquivo(s) em modloader\\%s?\nArquivos já existentes do mod serão copiados para backups\\modloader antes.' % (len(ok), mod), parent=win):
                return
            r = ga.instalar(plano, mod)
            win.destroy()
            if r['ok']:
                app.status('ModLoader: %d arquivo(s) instalados em %s (backup: %s)' % (r['instalados'], mod, r['backup']))
                messagebox.showinfo('ModLoader', '%d arquivo(s) instalados no mod "%s".\nAbra o jogo (ou recarregue pelo menu do ModLoader) para testar.' % (r['instalados'], mod))
                refresh()
            else:
                messagebox.showerror('ModLoader', r['motivo'])
        janela_texto('Instalar no ModLoader — simulação', '\n'.join(L), [('Instalar agora', aplicar)] if ok else [])

    def desinstalar():
        if not ga.info['ok']:
            return
        mod = cfg.get('modloader_mod') or MOD_PADRAO
        if not messagebox.askyesno('Desinstalar', 'Remover do mod "%s" só os arquivos que este programa instalou\n(e restaurar os backups)?' % mod):
            return
        r = ga.desinstalar(mod)
        app.status('ModLoader: %d removido(s), %d restaurado(s), %d mexido(s) por fora (mantidos).' % (r['removidos'], r['restaurados'], len(r['pulados'])))
        refresh()

    def abrir_pasta_mod():
        if not ga.game_dir:
            return
        p = ga.modloader_dir() / (cfg.get('modloader_mod') or MOD_PADRAO)
        if not p.is_dir():
            p = ga.modloader_dir() if ga.modloader_dir().is_dir() else ga.game_dir
        app.abrir_pasta_sistema(p)

    def abrir_jogo():
        exe = ga.info.get('exe')
        if exe and messagebox.askyesno('Abrir GTA', 'Abrir %s?' % exe):
            subprocess.Popen([str(exe)], cwd=str(ga.game_dir))

    m = tk.Menu(app.menubar, tearoff=0)
    m.add_command(label='🧭 Guia do projeto e fontes do jogo…', command=app.abrir_guia_projeto)
    m.add_separator()
    m.add_command(label='Detectar / escolher pasta do jogo…', command=escolher_pasta)
    m.add_command(label='Status (jogo, SAAT, ModLoader)…', command=status)
    m.add_separator()
    m.add_command(label='📥 Baixar / Configurar SAAT automaticamente…', command=baixar_saat_com_confirmacao)
    m.add_command(label='🎵 Extrair áudios: Jogo Base + Mod no ModLoader (SAAT)…', command=lambda: indexar('ambos', False))
    m.add_command(label='Extrair áudio original do jogo base (SAAT, só leitura)…', command=lambda: indexar('jogo', False))
    m.add_command(label='Extrair áudio do mod ativo no ModLoader (SAAT)…', command=lambda: indexar('mod', False))
    m.add_command(label='Reindexar tudo (forçar nova extração SAAT)…', command=lambda: indexar('ambos', True))
    m.add_command(label='Extrair outros bancos (SCRIPT / SPC / efeitos)…', command=app.popup_extrair_arquivos)
    m.add_command(label='Comparar com o american.gxt do jogo', command=abrir_gxt_do_jogo)
    m.add_separator()
    m.add_command(label='Nome do mod no ModLoader…', command=nome_mod)
    m.add_command(label='Instalar no ModLoader: fala selecionada…', command=lambda: instalar('fala'))
    m.add_command(label='Instalar no ModLoader: missão atual (todas dubladas)…', command=lambda: instalar('missao'))
    m.add_command(label='Instalar no ModLoader: missão atual (só revisadas ✓)…', command=lambda: instalar('revisadas'))
    m.add_command(label='Desinstalar nosso mod (restaura backups)…', command=desinstalar)
    m.add_command(label='Abrir pasta do mod', command=abrir_pasta_mod)
    m.add_separator()
    m.add_command(label='▶ Abrir GTA', command=abrir_jogo)
    app.menubar.add_cascade(label='🎮 Jogo (SAAT/ModLoader)', menu=m)

    app.comandos_extra = [
        ('Jogo: detectar / escolher pasta', escolher_pasta),
        ('Jogo: status SAAT/ModLoader', status),
        ('Jogo: baixar / configurar SAAT', baixar_saat_com_confirmacao),
        ('Jogo: extrair áudio original (SAAT)', lambda: indexar(False)),
        ('Jogo: comparar com american.gxt do jogo', abrir_gxt_do_jogo),
        ('ModLoader: instalar fala selecionada', lambda: instalar('fala')),
        ('ModLoader: instalar missão atual', lambda: instalar('missao')),
        ('ModLoader: instalar missão (só revisadas)', lambda: instalar('revisadas')),
        ('ModLoader: abrir pasta do mod', abrir_pasta_mod)
    ]
    app.jogo_api = {
        'escolher_pasta': escolher_pasta,
        'indexar': indexar,
        'instalar': instalar,
        'status': status,
        'baixar_saat': baixar_saat_com_confirmacao
    }
    return ga
