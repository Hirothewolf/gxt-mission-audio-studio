"""Human audition; ASR scores are advisory, never an acceptance gate."""
import math
import tkinter as tk
from tkinter import ttk, messagebox

from studio.audio.wav import wav_duracao
from studio.gxt.codec import decode_text_c


def wer_warning(candidate):
    metadata = candidate.get('metadata') or {}
    value = metadata.get('wer', metadata.get('blind_asr_wer'))
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ''
    if not math.isfinite(value) or value <= .15:
        return ''
    return 'WER %.1f%% — aviso; você decide pela escuta.' % (value * 100)


def collective_wer_candidates(candidates):
    return [c for c in candidates if c.get('status') == 'pending'
            and not c.get('stale') and wer_warning(c)
            and not (c.get('metadata') or {}).get('duration_override_required')]


class BatchAuditionWindow:
    """Review only the candidates imported by this run, on Tk's main thread."""
    def __init__(self, app, imported, parent=None):
        self.app, self.context = app, app.g
        self.items = {str(i): item for i, item in enumerate(imported)}
        self.original = None
        self.win = tk.Toplevel(parent or app.root)
        self.win.title('Ouvir e confirmar lote')
        self.win.geometry('900x520')
        self.win.transient(parent or app.root)
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(self.win, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='WER é apenas um aviso. Ouça e escolha os takes; os arquivos do jogo não são alterados.',
                  wraplength=850).pack(anchor='w')
        self.tree = ttk.Treeview(frame, columns=('line', 'state', 'wer'), show='headings', height=10)
        for col, label in [('line', 'Fala'), ('state', 'Revisão'), ('wer', 'Aviso WER')]:
            self.tree.heading(col, text=label)
        self.tree.pack(fill='both', expand=True, pady=8)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.select())
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail, wraplength=850).pack(fill='x', pady=6)
        controls = ttk.Frame(frame)
        controls.pack(fill='x')
        for label, action in [('Ouvir original', self.play_original), ('Ouvir gerado', self.play_generated),
                              ('Comparar A/B', self.compare), ('Parar', app.parar_audio)]:
            ttk.Button(controls, text=label, command=action).pack(side='left', padx=3)
        self.override = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='Aceitar diferença de duração desta fala, sem cortar ou acelerar',
                        variable=self.override).pack(anchor='w', pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x')
        for label, action in [('Aceitar', lambda: self.decide('approved')),
                              ('Recusar', lambda: self.decide('rejected')),
                              ('Confirmar para todos — WER', self.accept_wer), ('Fechar', self.close)]:
            ttk.Button(buttons, text=label, command=action).pack(side='left', padx=3)
        self.refresh()

    def candidate(self, item):
        if self.app.g is not self.context:
            raise ValueError('O GXT mudou. Reabra a revisão com o arquivo correto.')
        tab, key = item['line_id'].split('|')
        h = int(key, 16)
        if self.app.sel == (tab, h):
            self.app.commit_silencioso()
        text = decode_text_c(self.context.t[tab]['cur'][h])
        candidates = self.app.review_store.list_candidates(item['line_id'], text)
        c = next(c for c in candidates if c['id'] == item['candidate_id'])
        return c, tab, h, text

    def refresh(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for iid, item in self.items.items():
            try:
                c, _, _, _ = self.candidate(item)
                state = 'Texto alterado' if c.get('stale') else {
                    'pending': 'Pendente', 'approved': 'Aceito', 'rejected': 'Recusado'}[c['status']]
                warning = wer_warning(c)
            except (ValueError, KeyError, OSError, StopIteration) as exc:
                state, warning = 'Indisponível', str(exc)
            self.tree.insert('', 'end', iid=iid, values=(item['line_id'], state, warning))
        if self.items:
            self.tree.selection_set(selected[0] if selected else next(iter(self.items)))
        self.select()

    def selected(self):
        ids = self.tree.selection()
        return self.items[ids[0]] if ids else None

    def select(self):
        self.app.parar_audio()
        self.override.set(False)
        try:
            item = self.selected()
            if not item:
                return
            c, tab, h, text = self.candidate(item)
            self.original = self.app.wav_original(tab=tab, h=h)
            generated = self.app.review_store.resolve_path(c['path'])
            original_duration = wav_duracao(self.original) if self.original else None
            self.detail.set('%s\nOriginal: %s · gerado: %.2fs · %s' % (
                text, ('%.2fs' % original_duration) if original_duration else 'indisponível',
                wav_duracao(generated) or 0, wer_warning(c) or 'Sem aviso WER disponível'))
        except (ValueError, KeyError, OSError, StopIteration) as exc:
            self.detail.set(str(exc))

    def generated(self):
        c, _, _, _ = self.candidate(self.selected())
        return self.app.review_store.resolve_path(c['path'])

    def play_original(self):
        self.app._tocar(self.original, 'original')

    def play_generated(self):
        try:
            self.app._tocar(self.generated(), 'gerado')
        except (ValueError, KeyError, OSError, StopIteration) as exc:
            self.detail.set(str(exc))

    def compare(self):
        try:
            self.app.player.play_sequence([self.original, self.generated()], pausa_ms=300)
        except (ValueError, KeyError, OSError, StopIteration) as exc:
            self.detail.set(str(exc))

    def approve_item(self, item, status, override=False):
        c, tab, h, text = self.candidate(item)
        self.app.review_store.set_status(item['line_id'], c['id'], status, text,
                                        duration_override=override)
        self.app.limpar_cache_dublado(tab)
        self.app.atualizar_linha(tab, h)
        if self.app.sel == (tab, h):
            self.app._carregar_linha(tab, h)

    def decide(self, status):
        try:
            item = self.selected()
            if item:
                self.approve_item(item, status, status == 'approved' and self.override.get())
                self.refresh()
        except (ValueError, KeyError, OSError, StopIteration) as exc:
            messagebox.showerror('Revisão', str(exc), parent=self.win)

    def accept_wer(self):
        accepted, errors = 0, []
        for item in self.items.values():
            try:
                c, _, _, _ = self.candidate(item)
                if collective_wer_candidates([c]):
                    self.approve_item(item, 'approved')
                    accepted += 1
            except (ValueError, KeyError, OSError, StopIteration) as exc:
                errors.append(str(exc))
        self.refresh()
        self.detail.set('%d take(s) aceitos por confirmação coletiva de WER. '
                        'Duração e outros casos continuam para revisão individual.%s' % (
                            accepted, (' Falhas: ' + '; '.join(errors[:3])) if errors else ''))

    def close(self):
        self.app.parar_audio()
        self.win.destroy()
