"""Local UI preview: runs a tiny Python job; no downloads or TTS inference."""
import queue
import sys
from pathlib import Path
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.ui.dialogs import ProgressoOperacaoDialog, GerenciadorModelosDialog
from studio.tts.jobs import JobQueue


def main():
    root = tk.Tk()
    root.title('GXT Studio — prévia local do progresso')
    root.geometry('550x110+450+170')
    tk.Label(root, text='Verificação local da barra e do terminal expansível. Nenhum download.',
             wraplength=510).pack(padx=15, pady=12)
    if '--models' in sys.argv:
        root.title('GXT Studio — prévia local do catálogo')
        manager = GerenciadorModelosDialog(root)
        manager.win.geometry('780x620+400+250')
        root.mainloop()
        return
    progress = ProgressoOperacaoDialog(root, 'Prévia local — tarefa de verificação')
    progress.win.geometry('540x205+450+280')
    events = queue.Queue()
    jobs = JobQueue()
    command = [sys.executable, '-u', '-c',
               'import time\nfor i in range(1, 21):\n print(f"Etapa {i}/20: verificação local", flush=True)\n time.sleep(.2)']
    job = jobs.submit(command, Path(__file__).resolve().parents[1], on_line=events.put, timeout=20)

    def close():
        jobs.shutdown()
        root.destroy()

    def cancel():
        progress._cancelar()
        job.cancel()

    progress.btn_cancelar.configure(command=cancel)
    progress.win.protocol('WM_DELETE_WINDOW', close)
    root.protocol('WM_DELETE_WINDOW', close)

    def poll():
        while not events.empty():
            line = events.get_nowait()
            progress.atualizar(0, 0, line)
        if job.done:
            result = job.wait()
            progress.pb.stop()
            progress.pb.configure(mode='determinate', value=100 if result.returncode == 0 else 0)
            progress.lbl_status.configure(text='Prévia concluída.' if result.returncode == 0 else 'Prévia cancelada.')
            progress.lbl_detalhes.configure(text='Abra a seta para consultar as etapas da tarefa local.')
            progress.btn_cancelar.configure(text='Fechar prévia', state='normal', command=close)
            jobs.shutdown()
        else:
            root.after(60, poll)

    root.after(60, poll)
    root.mainloop()


if __name__ == '__main__':
    main()
