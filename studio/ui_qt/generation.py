"""Qt workers and explicit, cancellable OmniVoice generation controls."""
import json
import threading
import copy
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QDoubleSpinBox,
                               QLineEdit, QPushButton, QLabel, QPlainTextEdit)
from .model_settings import GenerationSettingsWidget


class Operation(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancelled = threading.Event()

    def run(self):
        try:
            self.succeeded.emit(self.operation(self.cancelled, self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc))

    def cancel(self):
        self.cancelled.set()


class GenerationWidget(QWidget):
    candidateReady = Signal(object)
    busyChanged = Signal(bool)
    logLine = Signal(str)

    def __init__(self, context, record, text_provider, parent=None):
        super().__init__(parent)
        self.context, self.record, self.text_provider = context, record, text_provider
        self.job = None
        self.preparation = None
        self.import_worker = None
        self._busy = False
        self.cancel_requested = threading.Event()
        layout = QVBoxLayout(self)
        self.settings = GenerationSettingsWidget(context, self)
        self.speed, self.duration, self.seed = self.settings.speed, self.settings.duration, self.settings.seed
        layout.addWidget(self.settings)
        self.reference = QLabel('Usa a referência original correspondente e sua transcrição. Cada tentativa fica preservada.')
        self.reference.setWordWrap(True)
        layout.addWidget(self.reference)
        self.start_button = QPushButton('Gerar take / nova tentativa')
        self.start_button.clicked.connect(self.start)
        layout.addWidget(self.start_button)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        self.logs.setMaximumHeight(65)
        layout.addWidget(self.logs)
        self.logLine.connect(self.logs.appendPlainText)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)

    @property
    def busy(self):
        return self._busy or self.job is not None

    def cancel(self):
        self.cancel_requested.set()
        if self.preparation:
            self.preparation.cancel()
        if self.import_worker:
            self.import_worker.cancel()
        if self.job:
            self.job.cancel()
        if self.busy:
            self.logs.appendPlainText('Cancelando; a pasta da tentativa será preservada. Aguarde a operação atual terminar.')

    def finish(self):
        self._busy = False
        self.start_button.setText('Gerar take / nova tentativa')
        self.speed.setEnabled(True)
        self.duration.setEnabled(True)
        self.seed.setEnabled(True)
        self.settings.setEnabled(True)
        self.busyChanged.emit(False)

    def start(self):
        if self.busy:
            self.cancel()
            return
        try:
            self.snapshot = self.text_provider()
            options = self.settings.request_options()
            if options.get('engine') == 'OmniVoice':
                options.pop('engine')
            record, snapshot = copy.deepcopy(self.record), self.snapshot
            self.cancel_requested = threading.Event()
            self._busy = True
            self.start_button.setText('Cancelar preparação')
            for control in (self.speed, self.duration, self.seed):
                control.setEnabled(False)
            self.settings.setEnabled(False)
            self.busyChanged.emit(True)
            self.logs.appendPlainText('Preparando referência original e conferindo as fontes…')
            def operation(cancel, progress):
                prepared = self.context.generation_request(record, snapshot, **options)
                request = prepared['request']
                if not isinstance(request, dict):
                    request = json.loads(Path(request).read_text(encoding='utf-8'))
                return prepared, request
            self.preparation = Operation(operation, self)
            self.preparation.succeeded.connect(self.prepared_ready)
            self.preparation.failed.connect(self.logs.appendPlainText)
            self.preparation.finished.connect(self.preparation_finished)
            self.preparation.start()
        except Exception as exc:
            self.logs.appendPlainText(str(exc))
            self.finish()

    def prepared_ready(self, result):
        if self.cancel_requested.is_set():
            self.logs.appendPlainText('Preparação cancelada; nenhum processo de geração foi iniciado.')
            return
        try:
            self.prepared, self.request = result
            request = self.request
            self.reference.setText('Referência: %s\nTranscrição original: %s\nVelocidade: %s · duração: %s · seed: %s' %
                (request.get('reference_path', '—'), request.get('reference_text', '—'),
                 request.get('speed', self.speed.value()), request.get('duration') or 'livre',
                 request.get('seed', '—')))
            self.job = self.context.jobs.submit_exclusive(self.prepared['argv'],
                cwd=self.context.project_root, on_line=self.logLine.emit,
                timeout=self.prepared.get('timeout', 600))
            self.start_button.setText('Cancelar geração')
            self.timer.start()
        except Exception as exc:
            self.logs.appendPlainText(str(exc))

    def preparation_finished(self):
        if not self.job:
            self.finish()

    def poll(self):
        if not self.job or not self.job.done:
            return
        result = self.job.wait()
        self.timer.stop()
        self.job = None
        try:
            if result.cancelled:
                raise ValueError('Geração cancelada; nenhum take foi importado.')
            if result.timed_out or result.returncode != 0:
                raise ValueError(result.error or 'A geração falhou. Consulte o registro; a tentativa foi preservada.')
            output = self.prepared.get('output') or self.request.get('output_path') or self.prepared.get('output_dir')
            for line in reversed(result.logs):
                try:
                    payload = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get('output_path'):
                    output = payload['output_path']
                    break
            if not output:
                raise ValueError('O worker não informou o WAV de saída.')
            # The worker preserves over-window WAVs in quarantine, along with
            # their receipt. Never pair that real output with the planned path.
            moved = self.prepared.get('output') and Path(output) != Path(self.prepared['output'])
            receipt = (Path(output).with_suffix('.omnivoice.json') if moved else
                       self.prepared.get('receipt') or Path(output).with_suffix('.omnivoice.json'))
            if self.cancel_requested.is_set():
                raise ValueError('Geração cancelada; nenhum take foi importado.')
            line_id, snapshot = self.record['line_id'], self.snapshot
            def operation(cancel, progress):
                if cancel.is_set() or self.cancel_requested.is_set():
                    raise ValueError('Importação cancelada; a saída foi preservada.')
                if self.prepared.get('engine', 'OmniVoice') == 'OmniVoice':
                    return self.context.review_store.import_omnivoice_candidate(line_id, output, snapshot, receipt)
                from studio.tts.results import import_generation_run
                result = import_generation_run(self.context.review_store, self.prepared['output_dir'],
                    self.record['table'], {self.record['hash']: snapshot}, self.prepared['engine'],
                    {self.record['hash']: self.prepared.get('window_seconds')})
                if not result['imported']:
                    raise ValueError('Nenhum WAV válido foi importado: ' + str(result['details']))
                cid = result['imported'][0]['candidate_id']
                return next(c for c in self.context.review_store.list_candidates(line_id, snapshot) if c['id'] == cid)
            self._busy = True
            self.start_button.setText('Cancelar importação')
            self.import_worker = Operation(operation, self)
            self.import_worker.succeeded.connect(self.imported)
            self.import_worker.failed.connect(self.logs.appendPlainText)
            self.import_worker.finished.connect(self.finish)
            self.import_worker.start()
        except Exception as exc:
            self.logs.appendPlainText(str(exc))
            self.finish()

    def imported(self, candidate):
        self.logs.appendPlainText('Take guardado para revisão; aplicação ao mod é separada.')
        self.candidateReady.emit(candidate)
