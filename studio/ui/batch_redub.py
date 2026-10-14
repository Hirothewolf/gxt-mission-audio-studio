# -*- coding: utf-8 -*-
"""Isolated, review-first batch synthesis for a selected GXT table."""
import json
import os
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import uuid
from datetime import datetime

from studio.gxt.codec import decode_text_c
from studio import config as studio_config
from studio.tts.jobs import ProcessResult
from studio.tts.registry import select_engine
from studio.tts import omnivoice_batch


def create_run_workspace(gxt, project_root):
    """Validate the current in-memory GXT and write it into a unique run folder."""
    data = gxt.build()
    gxt.validar(data)
    root = Path(project_root) / 'workspace' / 'studio_runs'
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / uuid.uuid4().hex
    run_dir.mkdir()
    snapshot_dir = run_dir / 'snapshot'
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / 'american.gxt'
    temporary = snapshot.with_suffix('.gxt.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, snapshot)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return run_dir, snapshot


def snapshot_texts(gxt, mission):
    table = gxt.t[mission]
    return {int(h): decode_text_c(table['cur'][h]) for h in table['hashes']}


def prepare_batch_snapshot(app, mission, project_root):
    """Apply editor text before taking the immutable input snapshot."""
    if not app.commit():
        raise ValueError('A edição atual não foi aplicada; síntese cancelada.')
    run_dir, snapshot = create_run_workspace(app.g, project_root)
    return run_dir, snapshot, snapshot_texts(app.g, mission)


def select_current_engine(preference=None):
    """Resolve against the active project root each time a run is started."""
    return select_engine(preference, root=studio_config.PROJ_ROOT)


def abrir_popup_redublar_lote(app):
    """Open the compact batch-generation dialog and queue one isolated run."""
    if not app.g:
        messagebox.showwarning('Aviso', 'Abra um arquivo GXT primeiro.')
        return
    jobs = getattr(app, 'jobs', None)
    if jobs is None:
        messagebox.showerror('TTS indisponível', 'A fila de síntese ainda não foi inicializada.')
        return
    preference = app.var_engine.get() if hasattr(app, 'var_engine') else None
    try:
        engine = select_current_engine(preference)
    except RuntimeError as exc:
        messagebox.showerror('Engine indisponível', str(exc))
        return
    tab_atual = app.tabela_atual()
    if not tab_atual or tab_atual == '*':
        tab_atual = app.g.order[0] if app.g.order else None
    if not tab_atual or tab_atual not in app.g.t:
        messagebox.showwarning('Aviso', 'Selecione uma missão/tabela válida.')
        return

    win = tk.Toplevel(app.root)
    win.title(f'🎙️ Redublagem em lote ({engine.name})')
    win.geometry('860x350')
    win.minsize(800, 320)
    win.transient(app.root)
    state = {'closed': False, 'job': None, 'run_dir': None, 'run_output': None, 'imported': []}

    def alive():
        if state['closed']:
            return False
        try:
            return bool(win.winfo_exists())
        except tk.TclError:
            return False

    def on_ui(callback):
        def guarded():
            if alive():
                try:
                    callback()
                except tk.TclError:
                    pass
        app.ui(guarded)

    def close_window():
        job = state.get('job')
        if job is not None and not job.done:
            if not messagebox.askyesno('Síntese em andamento', 'Cancelar e fechar esta janela?', parent=win):
                return
            state['closed'] = True
            job.cancel()
        else:
            state['closed'] = True
        win.destroy()

    win.protocol('WM_DELETE_WINDOW', close_window)
    body = ttk.Frame(win, padding=10)
    body.pack(fill='both', expand=True)

    engine_label = tk.StringVar(value=f'Tabela: {tab_atual}   ·   Engine: {engine.name}')
    ttk.Label(body, textvariable=engine_label).pack(anchor='w')
    controls = ttk.Frame(body)
    controls.pack(fill='x', pady=(10, 4))
    ttk.Label(controls, text='Limite (0 = todas):').grid(row=0, column=0, sticky='w')
    limit_entry = ttk.Spinbox(controls, from_=0, to=9999, width=8)
    limit_entry.set(0)
    limit_entry.grid(row=0, column=1, sticky='w', padx=(6, 16))
    ttk.Label(controls, text='Aceleração máxima:').grid(row=0, column=2, sticky='w')
    speed_entry = ttk.Entry(controls, width=8)
    speed_entry.insert(0, '1.50')
    speed_entry.grid(row=0, column=3, sticky='w', padx=6)
    exaggeration_var = tk.DoubleVar(value=0.5)
    ttk.Label(controls, text='Expressão (Chatterbox):').grid(row=0, column=4, sticky='w', padx=(10, 0))
    exaggeration_scale = ttk.Scale(controls, from_=0.0, to=1.0, variable=exaggeration_var, orient='horizontal', length=90)
    exaggeration_scale.grid(row=0, column=5, padx=6)
    if engine.id != 'chatterbox':
        exaggeration_scale.config(state='disabled')
    if engine.id == 'omnivoice':
        speed_entry.config(state='disabled')
        ttk.Label(controls, text='OmniVoice usa velocidade/duração/seed de Ajustes e gera uma fala por vez, '
                                 'com a referência original verificada de cada linha.',
                  foreground='#555555', wraplength=780, justify='left').grid(
            row=1, column=0, columnspan=6, sticky='w', pady=(6, 0))
        if hasattr(app, 'var_omnivoice_gpu_guard'):
            ttk.Checkbutton(controls, text='Trava de GPU (mín. 5 GB livres, máx. 10% de uso)',
                            variable=app.var_omnivoice_gpu_guard, command=app.salvar_opcoes).grid(
                row=2, column=0, columnspan=6, sticky='w', pady=(4, 0))

    status = ttk.Label(body, text='Pronto para iniciar. Os resultados entram como candidatos pendentes de revisão.')
    status.pack(fill='x', pady=(10, 4))
    progress = ttk.Progressbar(body, mode='determinate', value=0, maximum=1)
    progress.pack(fill='x', pady=(0, 8))

    detail_toggle = ttk.Button(body, text='▶ Mostrar detalhes')
    detail_toggle.pack(anchor='w')
    detail_frame = ttk.Frame(body)
    log_text = tk.Text(detail_frame, height=8, wrap='word', state='disabled')
    log_scroll = ttk.Scrollbar(detail_frame, orient='vertical', command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side='left', fill='both', expand=True)
    log_scroll.pack(side='right', fill='y')
    state['details'] = False

    def toggle_details():
        state['details'] = not state['details']
        if state['details']:
            detail_frame.pack(fill='both', expand=True, pady=(4, 0))
            detail_toggle.config(text='▼ Ocultar detalhes')
            job = state.get('job')
            if job:
                log_text.config(state='normal')
                log_text.delete('1.0', 'end')
                log_text.insert('end', '\n'.join(job.logs[-200:]))
                log_text.see('end')
                log_text.config(state='disabled')
        else:
            detail_frame.pack_forget()
            detail_toggle.config(text='▶ Mostrar detalhes')

    detail_toggle.config(command=toggle_details)

    buttons = ttk.Frame(body)
    buttons.pack(fill='x', side='bottom', pady=(8, 0))
    start_button = ttk.Button(buttons, text='▶ Iniciar')
    start_button.pack(side='left')
    cancel_button = ttk.Button(buttons, text='⏹ Cancelar', state='disabled')
    cancel_button.pack(side='left', padx=6)
    folder_button = ttk.Button(buttons, text='📁 Abrir execução', state='disabled')
    folder_button.pack(side='left')
    def review_batch():
        from studio.ui.audition import BatchAuditionWindow
        if state['imported']:
            BatchAuditionWindow(app, state['imported'], parent=win)
    review_button = ttk.Button(buttons, text='Ouvir / confirmar lote', command=review_batch, state='disabled')
    review_button.pack(side='left', padx=6)
    close_button = ttk.Button(buttons, text='Fechar', command=close_window)
    close_button.pack(side='right')

    progress_total = re.compile(r'Total de falas a processar:\s*(\d+)')
    progress_current = re.compile(r'\[(\d+)/(\d+)\]')

    def add_log(line):
        m_total = progress_total.search(line)
        if m_total:
            total = max(1, int(m_total.group(1)))
            progress.configure(maximum=total, value=0)
            status.config(text=f'Preparando {total} fala(s)…')
        m_current = progress_current.search(line)
        if m_current:
            current, total = map(int, m_current.groups())
            progress.configure(maximum=max(1, total), value=current)
            status.config(text=f'Processando fala {current} de {total}…')
        elif any(key in line for key in ('[OK]', '[ERRO]', '[AVISO]', '[SPEEDUP]')):
            status.config(text=line[:220])
        if state['details']:
            log_text.config(state='normal')
            log_text.insert('end', line + '\n')
            last = int(log_text.index('end-1c').split('.')[0])
            if last > 500:
                log_text.delete('1.0', f'{last - 500}.0')
            log_text.see('end')
            log_text.config(state='disabled')

    def finalize_ui(result, import_counts=None, import_error=None):
        app.batch_ativo = False
        state['job'] = None
        start_button.config(state='normal')
        cancel_button.config(state='disabled')
        close_button.config(state='normal')
        folder_button.config(state='normal' if state['run_dir'] else 'disabled')
        state['imported'] = (import_counts or {}).get('imported', [])
        review_button.config(state='normal' if state['imported'] else 'disabled')
        if result.cancelled:
            status.config(text='Síntese cancelada. A execução isolada foi mantida para inspeção.')
        elif result.timed_out:
            status.config(text='A síntese atingiu o limite de tempo; a execução foi mantida.')
        elif result.error:
            status.config(text=f'Não foi possível executar o engine: {result.error}')
        elif result.returncode != 0:
            status.config(text=f'Engine terminou com código {result.returncode}; arquivos não importados.')
        elif import_error:
            status.config(text=f'Execução terminou, mas a validação/importação falhou: {import_error}')
        elif import_counts:
            valid = int(import_counts.get('valid', 0))
            quarantined = int(import_counts.get('quarantined', 0))
            missing = int(import_counts.get('missing', 0))
            if valid:
                status.config(text=f'{valid} candidato(s) pendente(s) de revisão; {quarantined} em quarentena; {missing} ausente(s).')
            else:
                status.config(text=f'Nenhum WAV válido foi importado; {quarantined} em quarentena; {missing} ausente(s).')
        else:
            status.config(text=f'Execução salva em {state["run_output"]}; importação para revisão pendente.')
        if state['details']:
            log_text.config(state='normal')
            tail = result.logs[-20:]
            if tail:
                log_text.insert('end', '\n' + '\n'.join(tail) + '\n')
            log_text.see('end')
            log_text.config(state='disabled')

    def start():
        nonlocal engine
        if jobs.busy:
            messagebox.showinfo('Síntese ocupada', 'A fila já está processando outra geração.', parent=win)
            return
        try:
            current_preference = app.var_engine.get() if hasattr(app, 'var_engine') else None
            engine = select_current_engine(current_preference)
            engine_label.set(f'Tabela: {tab_atual}   ·   Engine: {engine.name}')
            exaggeration_scale.config(state='normal' if engine.id == 'chatterbox' else 'disabled')
        except RuntimeError as exc:
            messagebox.showerror('Engine indisponível', str(exc), parent=win)
            return
        if engine.id == 'omnivoice':
            speed_entry.config(state='disabled')
            start_omnivoice()
            return
        try:
            app.batch_ativo = True
            limit = int(limit_entry.get())
            if limit < 0:
                raise ValueError
            speed = float(speed_entry.get().replace(',', '.'))
            if speed <= 0:
                raise ValueError
            run_dir, snapshot, texts = prepare_batch_snapshot(app, tab_atual, engine.root)
            out_root = run_dir / 'output'
            run_output = out_root / tab_atual
            command = engine.build_command(
                mission=tab_atual,
                gxt_path=snapshot,
                output_dir=out_root,
                limit=limit,
                max_speedup=speed,
                exaggeration=exaggeration_var.get() if engine.id == 'chatterbox' else None,
            )
        except Exception as exc:
            app.batch_ativo = False
            messagebox.showerror('Parâmetros inválidos', str(exc), parent=win)
            return

        state['run_dir'] = run_dir
        state['run_output'] = run_output
        state['imported'] = []
        review_button.config(state='disabled')
        run_log = run_dir / 'tts.log'
        folder_button.config(state='normal')
        start_button.config(state='disabled')
        cancel_button.config(state='normal')
        close_button.config(state='disabled')
        progress.configure(value=0, maximum=1)
        status.config(text=f'Executando {engine.name} para {tab_atual}…')
        try:
            def consume_line(line):
                try:
                    with run_log.open('a', encoding='utf-8') as stream:
                        stream.write('[%s] %s\n' % (datetime.now().isoformat(timespec='seconds'), line))
                except OSError:
                    pass
                on_ui(lambda l=line: add_log(l))

            job = jobs.submit(command, cwd=engine.root, on_line=consume_line, timeout=600)
        except Exception as exc:
            app.batch_ativo = False
            finalize_ui(ProcessResult(None, False, False, [], str(exc)))
            return
        state['job'] = job
        if state['details']:
            toggle_details()
            toggle_details()

        def wait_and_import():
            result = job.wait()
            counts = None
            import_error = None
            if not state['closed'] and not getattr(app, 'closed', False) and result.returncode == 0 and not result.cancelled and not result.timed_out and not result.error:
                importer = getattr(app, 'import_generation_run', None)
                if importer is None:
                    import_error = 'o fluxo de revisão ainda não está disponível'
                else:
                    try:
                        counts = importer(run_output, tab_atual, texts, engine.name)
                    except Exception as exc:
                        import_error = str(exc)
            app.batch_ativo = False
            on_ui(lambda: finalize_ui(result, counts, import_error))

        threading.Thread(target=wait_and_import, name='tts-batch-finalize', daemon=True).start()

    def start_omnivoice():
        """Lote OmniVoice: um take verificado por fala, em série, importado fala a fala."""
        try:
            app.batch_ativo = True
            limit = int(limit_entry.get())
            if limit < 0:
                raise ValueError('O limite não pode ser negativo.')
            run_dir, _snapshot, texts = prepare_batch_snapshot(app, tab_atual, engine.root)
            keys = app.nomes_tabela(tab_atual, forcar=True)
            hashes = list(app.g.t[tab_atual]['hashes'])
        except Exception as exc:
            app.batch_ativo = False
            messagebox.showerror('Parâmetros inválidos', str(exc), parent=win)
            return
        controller = omnivoice_batch.BatchController()
        state.update(job=controller, run_dir=run_dir, run_output=run_dir / 'lines', imported=[])
        run_log = run_dir / 'tts.log'
        review_button.config(state='disabled')
        folder_button.config(state='normal')
        start_button.config(state='disabled')
        cancel_button.config(state='normal')
        close_button.config(state='disabled')
        progress.configure(value=0, maximum=1)
        status.config(text='Verificando referência original de cada fala de %s…' % tab_atual)
        engine_ref = engine
        # Variáveis Tk são lidas aqui (thread de UI); o worker só vê valores simples.
        settings = {'use_cj_reference': bool(app.var_omnivoice_cj_reference.get()),
                    'speed': app.var_omnivoice_speed.get(),
                    'duration': app.var_omnivoice_duration.get()}
        timeout = float(studio_config.CFG.get('tts_timeout_seconds', 600))

        def consume_line(line):
            controller.logs.append(line)
            del controller.logs[:-500]
            try:
                with run_log.open('a', encoding='utf-8') as stream:
                    stream.write('[%s] %s\n' % (datetime.now().isoformat(timespec='seconds'), line))
            except OSError:
                pass
            on_ui(lambda l=line: add_log(l))

        def build(line_run, h, text):
            return app._omnivoice_command_for_selection(
                engine_ref, line_run, tab_atual, h, keys.get(h), text, settings=settings, announce=False)

        def import_line(item):
            from studio.audio.source_routing import command_output_directory
            folder = command_output_directory(item.command, item.output_dir / tab_atual)
            return app.import_generation_run(folder, tab_atual, {item.h: item.text}, engine_ref.name)

        def worker():
            summary = omnivoice_batch.BatchSummary()
            try:
                items, skipped = omnivoice_batch.plan_lines(
                    build, tab_atual, hashes, texts, keys, run_dir, limit, stop=controller.cancel_event.is_set)
                if controller.cancel_event.is_set():
                    summary = omnivoice_batch.BatchSummary(skipped=skipped, cancelled=True)
                else:
                    summary = omnivoice_batch.run_batch(
                        items, skipped, tab_atual, jobs.submit, import_line, controller, consume_line,
                        cwd=engine_ref.root, timeout=timeout)
            except Exception as exc:
                consume_line('[ERRO] Lote interrompido: %s' % exc)
                summary.failed.append(('lote', str(exc)))
            finally:
                controller.finish()
            try:
                (run_dir / 'batch_summary.json').write_text(json.dumps({
                    'table': tab_atual, 'engine': engine_ref.name, 'total': summary.total,
                    'valid': summary.valid, 'quarantined': summary.quarantined,
                    'failed': summary.failed, 'skipped': summary.skipped,
                    'cancelled': summary.cancelled, 'timed_out': summary.timed_out, 'aborted': summary.aborted,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError:
                pass
            counts = {'valid': summary.valid, 'quarantined': summary.quarantined,
                      'missing': len(summary.failed), 'imported': summary.imported}
            result = ProcessResult(0, summary.cancelled, False, list(controller.logs[-20:]))
            app.batch_ativo = False

            def done():
                finalize_ui(result, counts)
                if summary.aborted:
                    hint = (' Desmarque "Trava de GPU" se a leitura estiver errada.'
                            if 'GPU fora do limite' in summary.aborted else '')
                    status.config(text='Lote interrompido (mesmo erro em falas seguidas): %s%s  ·  %d candidato(s) '
                                       'já importado(s).' % (summary.aborted[:160], hint, summary.valid))
                elif not summary.cancelled:
                    status.config(text='%d candidato(s) pendente(s) de revisão; %d em quarentena; %d falha(s); '
                                       '%d pulada(s). Detalhes em batch_summary.json.' % (
                                           summary.valid, summary.quarantined, len(summary.failed),
                                           len(summary.skipped)))
            on_ui(done)

        threading.Thread(target=worker, name='omnivoice-batch', daemon=True).start()

    def cancel():
        job = state.get('job')
        if job is not None and not job.done:
            cancel_button.config(state='disabled')
            status.config(text='Cancelando processo…')
            job.cancel()

    def open_run():
        run_dir = state.get('run_dir')
        if run_dir and run_dir.is_dir():
            try:
                os.startfile(str(run_dir))
            except (AttributeError, OSError):
                pass

    start_button.config(command=start)
    cancel_button.config(command=cancel)
    folder_button.config(command=open_run)
