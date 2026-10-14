"""Threaded operations whose results cross Qt signals before touching widgets."""
import threading
import traceback
from PySide6.QtCore import QThread, Signal


class Task(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancel_event = threading.Event()
        self.details = ''

    def run(self):
        try:
            result = self.function(self.progress.emit, self.cancel_event)
            self.succeeded.emit(result)
        except Exception as exc:
            self.details = traceback.format_exc()
            self.failed.emit(str(exc) or type(exc).__name__)

    def cancel(self):
        self.cancel_event.set()
