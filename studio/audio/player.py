from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

from studio.audio.wav import wav_duracao


class Player:
    """Reproduz WAVs usando winsound e o agendador Tk na thread principal."""

    def __init__(self, root):
        self.root = root
        self._job = None
        self._token = 0

    @property
    def disponivel(self):
        return winsound is not None

    def _tocar(self, path):
        if winsound and path:
            try:
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass

    def _parar_som(self):
        if winsound:
            try:
                winsound.PlaySound(None, 0)
            except Exception:
                pass

    def play(self, path):
        self.stop()
        self._tocar(path)

    def stop(self):
        self._token += 1
        if self._job is not None and self.root:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
        self._job = None
        self._parar_som()

    def _schedule(self, token, delay_ms, callback):
        def run():
            if token != self._token:
                return
            self._job = None
            callback()

        self._job = self.root.after(max(0, int(delay_ms)), run)

    def play_sequence(self, paths, pausa_ms=300, loop=False, on_step=None, on_done=None):
        """Play a sequence asynchronously; all timing and callbacks run on Tk's thread."""
        valid_paths = [str(p) for p in paths if p and Path(p).is_file()]
        if not valid_paths or not self.root:
            return
        self.stop()
        token = self._token
        pausa = max(0, int(float(pausa_ms)))
        index = 0

        def start_next():
            nonlocal index
            if token != self._token:
                return
            if index >= len(valid_paths):
                if loop:
                    index = 0
                    start_next()
                elif on_done:
                    on_done()
                return

            current = index
            path = valid_paths[current]
            index += 1
            if on_step:
                on_step(current, path)
            self._tocar(path)
            duration = wav_duracao(path) or 0.0
            delay = max(0, int(round(duration * 1000)))

            def after_clip():
                nonlocal index
                if token != self._token:
                    return
                if index < len(valid_paths):
                    self._schedule(token, pausa, start_next)
                elif loop:
                    index = 0
                    self._schedule(token, pausa, start_next)
                elif on_done:
                    on_done()

            self._schedule(token, delay, after_clip)

        start_next()
