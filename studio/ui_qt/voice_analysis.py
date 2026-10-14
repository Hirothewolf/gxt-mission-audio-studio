"""Explicit local voice analysis controls, reusing the maintained CLI."""
from PySide6.QtCore import QProcess, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QPlainTextEdit, QProgressBar)
from studio.audio import voice_runtime


class VoiceAnalysisWidget(QWidget):
    completed = Signal()

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.process = None
        root = QVBoxLayout(self)
        label = QLabel('Agrupa vozes do áudio original. O reconhecimento oferece sugestões; você confirma os personagens depois de ouvir.')
        label.setWordWrap(True)
        root.addWidget(label)
        bar = QHBoxLayout()
        self.scope = QComboBox()
        document = getattr(context, 'document', None)
        for table in getattr(getattr(document, 'gxt', None), 'order', []):
            self.scope.addItem(table, [table])
        self.scope.addItem('Todos os SCRIPT', None)
        bar.addWidget(self.scope, 1)
        self.analyze = QPushButton('Analisar vozes')
        self.analyze.clicked.connect(lambda: self.start(False))
        bar.addWidget(self.analyze)
        self.setup = QPushButton('Preparar reconhecimento local…')
        self.setup.setToolTip('Baixa/verifica o modelo somente ao clicar. Não gera dublagens.')
        self.setup.clicked.connect(lambda: self.start(True))
        bar.addWidget(self.setup)
        self.cancel_button = QPushButton('Cancelar')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        bar.addWidget(self.cancel_button)
        root.addLayout(bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        root.addWidget(self.logs, 1)
        self.refresh()

    def refresh(self):
        state = voice_runtime.runtime_status(self.context.project_root)
        self.status.setText(state['reason'])
        self.analyze.setEnabled(bool(state['ready']) and not self.busy())
        self.setup.setEnabled(bool(state['can_setup']) and not self.busy())

    def busy(self):
        return self.process is not None

    def start(self, setup=False):
        if self.busy():
            return
        try:
            command = (voice_runtime.build_setup_command(self.context.project_root) if setup else
                voice_runtime.build_analysis_command(self.context.project_root, tables=self.scope.currentData()))
        except (ValueError, RuntimeError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self.context.project_root))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.error)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.analyze.setEnabled(False)
        self.setup.setEnabled(False)
        self.scope.setEnabled(False)
        self.status.setText('Preparando reconhecimento…' if setup else 'Analisando vozes… os resultados serão salvos no projeto.')
        self.process.start(command[0], command[1:])

    def read_output(self):
        if self.process:
            self.logs.appendPlainText(bytes(self.process.readAllStandardOutput()).decode('utf-8', 'replace').strip())

    def error(self, reason):
        if self.process and self.process.state() == QProcess.ProcessState.NotRunning:
            self.logs.appendPlainText(self.process.errorString())
            self.finished(-1)

    def finished(self, code, *args):
        if not self.process:
            return
        self.read_output()
        process, self.process = self.process, None
        process.deleteLater()
        self.progress.hide()
        self.cancel_button.setEnabled(False)
        self.scope.setEnabled(True)
        self.refresh()
        self.status.setText('Concluído. As sugestões estão na aba de grupos.' if code == 0 else 'Análise interrompida. Confira os detalhes abaixo; resultados salvos foram preservados.')
        self.completed.emit()

    def cancel(self):
        if self.process:
            # Terminate only the process tree started by this widget.
            import os
            import subprocess
            import threading
            pid = int(self.process.processId())
            if pid and os.name == 'nt':
                from studio.core.utils import CREATE_FLAGS
                threading.Thread(target=lambda: subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True, creationflags=CREATE_FLAGS), daemon=True).start()
            else:
                self.process.kill()
            self.status.setText('Cancelando a análise…')
