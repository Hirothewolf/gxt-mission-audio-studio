"""Pick and export native SFX archives without requiring subtitle mappings."""
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from studio.script.mapping import PAKS
from studio.ui.dialogs import ProgressoOperacaoDialog
from studio.config import get_project_root
from studio.core.utils import escrever_log, json_atomico


MISSION_PAKS = ('SCRIPT',)
NPC_PAKS = tuple(pak for pak in PAKS if pak.startswith('SPC_'))
EFFECT_PAKS = ('FEET', 'GENRL', 'PAIN_A')
PRESETS = {
    'missoes': MISSION_PAKS,
    'npcs': NPC_PAKS,
    'dialogos': MISSION_PAKS + NPC_PAKS,
}


def archives_for_preset(name):
    """Return the exact archive set for a user-facing selection preset."""
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError('Predefinição desconhecida: %s' % name)


def selection_for_preset(name):
    """Build a complete PAK selection map; effects always require an explicit choice."""
    selected = set(archives_for_preset(name))
    return {pak: pak in selected for pak in PAKS}


def open_archive_extraction(app):
    game = getattr(app, 'jogo', None)
    if not game or not game.info['ok']:
        messagebox.showinfo('Extração de áudio', 'Configure a pasta do GTA San Andreas primeiro.', parent=app.root)
        return
    if getattr(app, 'archive_busy', False):
        app.status('Uma extração já está em andamento.')
        return

    win = tk.Toplevel(app.root)
    win.title('Extrair bancos de áudio')
    win.geometry('720x700')
    win.minsize(650, 620)
    win.transient(app.root)
    frame = ttk.Frame(win, padding=16)
    frame.pack(fill='both', expand=True)

    ttk.Label(frame, text='Escolha os áudios que deseja indexar',
              font=('Segoe UI', 13, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='A extração independe das legendas e preserva os bancos que já foram extraídos. '
              'Streams de rádio e CUTSCEN não são suportados pelo extrator SFX atual.',
              wraplength=670, justify='left').pack(fill='x', pady=(5, 12))

    originals = set(game.available_archives('original'))
    mods = set(game.available_archives('mod'))
    available = originals | mods
    variables = {pak: tk.BooleanVar(value=(pak == 'SCRIPT' and pak in available)) for pak in PAKS}

    presets = ttk.LabelFrame(frame, text=' Atalhos de seleção ', padding=(10, 7))
    presets.pack(fill='x', pady=(0, 10))
    preset_buttons = ttk.Frame(presets)
    preset_buttons.pack(fill='x')

    def apply_preset(name):
        for pak, selected in selection_for_preset(name).items():
            variables[pak].set(selected and pak in available)

    for label, name in (('Missões · SCRIPT', 'missoes'),
                        ('NPCs · SPC (~50 mil slots)', 'npcs'),
                        ('Diálogos · SCRIPT + SPC', 'dialogos')):
        ttk.Button(preset_buttons, text=label, command=lambda n=name: apply_preset(n)).pack(
            side='left', expand=True, fill='x', padx=3)

    ttk.Label(presets, text='SCRIPT reúne falas ligadas às missões e aos scripts. Os SPCs reúnem vozes de NPCs '
              'e falas ambientes (lote grande, cerca de 50 mil slots). NPCs e bancos de efeitos só entram '
              'quando você escolhe um atalho ou os marca.',
              wraplength=650, justify='left').pack(fill='x', pady=(7, 0))

    mission_box = ttk.LabelFrame(frame, text=' Missões ', padding=(10, 5))
    mission_box.pack(fill='x', pady=(0, 8))
    ttk.Checkbutton(mission_box, text='SCRIPT — falas associadas às missões e scripts',
                    variable=variables['SCRIPT'], state='normal' if 'SCRIPT' in available else 'disabled').pack(
                        anchor='w', fill='x')
    ttk.Label(mission_box, text=_availability_label('SCRIPT', originals, mods),
              foreground='#666666').pack(anchor='w', padx=(26, 0))

    npc_box = ttk.LabelFrame(frame, text=' Vozes de NPCs e falas ambientes ', padding=(10, 5))
    npc_box.pack(fill='x', pady=(0, 8))
    npc_descriptions = {
        'SPC_EA': 'SPC_EA — falas e vozes de ambiente',
        'SPC_FA': 'SPC_FA — falas e vozes de ambiente',
        'SPC_GA': 'SPC_GA — falas e vozes de ambiente',
        'SPC_NA': 'SPC_NA — falas e vozes de ambiente',
        'SPC_PA': 'SPC_PA — falas e vozes de ambiente',
    }
    for pak in NPC_PAKS:
        row = ttk.Frame(npc_box)
        row.pack(fill='x', pady=1)
        ttk.Checkbutton(row, text=npc_descriptions.get(pak, pak), variable=variables[pak],
                        state='normal' if pak in available else 'disabled').pack(side='left', anchor='w')
        ttk.Label(row, text=_availability_label(pak, originals, mods),
                  foreground='#666666').pack(side='right', padx=(8, 2))

    effects_panel = ttk.LabelFrame(frame, text=' Bancos de efeitos — seleção opcional ', padding=(10, 6))
    effect_vars = {}
    for pak, description in (
            ('FEET', 'FEET — passos, movimentos e sons corporais'),
            ('GENRL', 'GENRL — efeitos gerais do jogo'),
            ('PAIN_A', 'PAIN_A — gritos de dor e reações')):
        effect_vars[pak] = variables[pak]
        ttk.Checkbutton(effects_panel, text=description, variable=variables[pak],
                        state='normal' if pak in available else 'disabled').pack(anchor='w', fill='x', pady=1)
    ttk.Label(effects_panel, text='Streams de rádio e CUTSCEN não são arquivos SFX e continuam indisponíveis '
              'nesta ferramenta.', wraplength=650, justify='left').pack(fill='x', pady=(5, 0))

    expanded = tk.BooleanVar(value=False)
    def toggle_effects():
        if expanded.get():
            effects_panel.pack(fill='x', pady=(0, 8), after=effects_toggle)
            effects_toggle.configure(text='▾ Ocultar bancos de efeitos avançados')
        else:
            effects_panel.pack_forget()
            effects_toggle.configure(text='▸ Mostrar bancos de efeitos avançados')
    effects_toggle = ttk.Button(frame, text='▸ Mostrar bancos de efeitos avançados',
                                command=lambda: (expanded.set(not expanded.get()), toggle_effects()))
    effects_toggle.pack(anchor='w', pady=(0, 8))

    target_row = ttk.Frame(frame)
    target_row.pack(fill='x', pady=(2, 5))
    ttk.Label(target_row, text='Origem:').pack(side='left')
    target = tk.StringVar(value='Original e mod')
    ttk.Combobox(target_row, textvariable=target, state='readonly', width=24,
                 values=['Original e mod', 'Somente original', 'Somente mod']).pack(side='left', padx=(7, 0))
    force = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text='Refazer mesmo se o cache estiver atualizado', variable=force).pack(anchor='w')
    ttk.Label(frame, text='Arquivos ausentes no mod são ignorados. Cada banco é salvo separadamente; '
              'cancelar ou falhar preserva a extração anterior. Depois, atualize a Biblioteca de áudio.',
              wraplength=670, justify='left').pack(fill='x', pady=(7, 10))

    def start():
        if getattr(app, 'archive_busy', False):
            return
        selection = [pak for pak in PAKS if variables[pak].get() and pak in available]
        if not selection:
            messagebox.showinfo('Extração', 'Selecione um banco disponível.', parent=win)
            return
        mode = {'Original e mod': 'ambos', 'Somente original': 'jogo', 'Somente mod': 'mod'}[target.get()]
        reextract = force.get()
        win.destroy()
        run = get_project_root() / 'workspace' / 'studio_runs' / ('extraction-' + uuid.uuid4().hex)
        run.mkdir(parents=True, exist_ok=True)
        progress = ProgressoOperacaoDialog(app.root, 'Extraindo bancos de áudio')
        app.archive_busy = True
        app.archive_cancel = progress.cancel_event

        def line(text):
            escrever_log('extraction.log', text, base_dir=run)
            def display():
                if progress.win.winfo_exists():
                    progress.atualizar(0, 0, text)
            app.ui(display)

        def work():
            try:
                result = game.indexar_arquivos(selection, alvo=mode, on_line=line,
                                               cancel=progress.cancel_event, forcar=reextract)
                json_atomico(run / 'extraction_result.json', result)
                error = None
            except Exception as exc:
                result, error = None, str(exc)
                escrever_log('extraction.log', error, base_dir=run)

            def finish():
                app.archive_busy = False
                progress.fechar()
                if error:
                    messagebox.showerror('Extração', error, parent=app.root)
                elif progress.cancel_event.is_set():
                    app.status('Extração cancelada; bancos anteriormente concluídos preservados.')
                else:
                    app.status('Extração finalizada. Atualize a Biblioteca de áudio para conferir todos os slots.')
                    if result and not result.get('ok'):
                        messagebox.showwarning('Extração parcial', 'Alguns bancos não foram extraídos. '
                                               'Log e resultado: %s' % run, parent=app.root)
                app.original_audio_cache.clear()

            app.ui(finish)

        progress.atualizar(0, 0, 'Preparando %d banco(s). Expanda o terminal para detalhes.' % len(selection))
        threading.Thread(target=work, daemon=True).start()

    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', side='bottom', pady=(4, 0))
    ttk.Button(buttons, text='Cancelar', command=win.destroy).pack(side='right', padx=(6, 0))
    ttk.Button(buttons, text='Extrair selecionados', command=start).pack(side='right')


def _availability_label(pak, originals, mods):
    sources = []
    if pak in originals:
        sources.append('jogo')
    if pak in mods:
        sources.append('mod')
    return 'Disponível: ' + ' + '.join(sources) if sources else 'Não encontrado'
