"""One Qt-owned player shared by the classic-layout windows."""
from pathlib import Path
from PySide6.QtCore import QObject, QUrl, Signal, QTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput


class QtAudioPlayer(QObject):
    status = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = QAudioOutput(self)
        self.media = QMediaPlayer(self)
        self.media.setAudioOutput(self.output)
        self.output.setVolume(0.85)
        self._queue = []
        self._generation = 0
        self._loop_paths = []
        self._pause_ms = 300
        self.media.positionChanged.connect(lambda value: self.positionChanged.emit(int(value)))
        self.media.durationChanged.connect(lambda value: self.durationChanged.emit(int(value)))
        self.media.errorOccurred.connect(lambda error, message: self.status.emit('Não foi possível reproduzir: ' + message))
        self.media.mediaStatusChanged.connect(self._media_status)

    def play(self, path):
        self.stop()
        self._start(path)

    def _start(self, path):
        if not path or not Path(path).is_file():
            self.status.emit('Áudio indisponível. Extraia ou gere a fala primeiro.')
            return False
        self.media.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self.media.play()
        self.status.emit('Reproduzindo: ' + Path(path).name)
        return True

    def stop(self):
        self._generation += 1
        self._queue.clear()
        self._loop_paths.clear()
        self.media.stop()

    def pause(self):
        self.media.pause()

    def seek(self, milliseconds):
        self.media.setPosition(max(0, int(milliseconds)))

    def compare(self, paths, loop=False, pause_ms=300):
        selected = list(paths)
        if not selected or any(not path or not Path(path).is_file() for path in selected):
            self.status.emit('A comparação precisa de todos os áudios disponíveis.')
            return
        self.stop()
        self._loop_paths = selected[:] if loop else []
        self._pause_ms = max(0, int(pause_ms))
        self._queue = selected[1:]
        self._start(selected[0])

    def _media_status(self, state):
        if state == QMediaPlayer.MediaStatus.EndOfMedia and not self._queue and self._loop_paths:
            self._queue = self._loop_paths[:]
        if state == QMediaPlayer.MediaStatus.EndOfMedia and self._queue:
            path = self._queue.pop(0)
            generation = self._generation
            QTimer.singleShot(self._pause_ms, lambda: self._start(path) if generation == self._generation else None)
