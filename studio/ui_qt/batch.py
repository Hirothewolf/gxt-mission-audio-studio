"""Explicit finite local generation queue; approved takes are never regenerated."""
import json
import uuid
from pathlib import Path
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDoubleSpinBox, QLineEdit, QCheckBox, QPlainTextEdit, QProgressBar, QFormLayout)
from .tasks import Task
from studio.core.utils import json_atomico


class BatchGenerationDialog(QDialog):
    # Emits generated candidates as {record, candidate} pairs so the main
    # window can select them in PendingAudio and route them through Save.
    candidatesReady = Signal(list)

    def __init__(self, context, records, parent=None):
        super().__init__(parent)
        self.context = context
        self.records = [dict(row, line_id=row.get('line_id') or '%s|%08x' % (row['table'], row['hash']))
                        for row in records if row.get('info')]
        self.job = None
        self.child_job = None
        self.setWindowTitle('Redublar lote — OmniVoice local')
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('%d falas com vínculo de áudio na seleção. Takes aprovados serão preservados.' % len(self.records)))
        form = QFormLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 2.)
        self.speed.setSingleStep(.05)
        self.speed.setValue(1.)
        self.duration = QLineEdit('livre')
        self.seed = QLineEdit()
        self.seed.setPlaceholderText('Vazio = aleatória por fala')
        form.addRow('Velocidade', self.speed)
        form.addRow('Duração: original / livre / segundos', self.duration)
        form.addRow('Seed', self.seed)
        layout.addLayout(form)
        self.regenerate_approved = QCheckBox(
            'Regerar também falas com take aprovado (preserva os takes anteriores)')
        layout.addWidget(self.regenerate_approved)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.records))
        layout.addWidget(self.progress)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1000)
        layout.addWidget(self.logs)
        bar = QHBoxLayout()
        self.start_button = QPushButton('Iniciar lote')
        self.start_button.clicked.connect(self.start)
        bar.addWidget(self.start_button)
        self.cancel_button = QPushButton('Cancelar após preservar o progresso')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        bar.addWidget(self.cancel_button)
        layout.addLayout(bar)

    def start(self):
        if self.job:
            return
        try:
            speed, duration = self.speed.value(), self.duration.text().strip() or 'livre'
            seed = int(self.seed.text()) if self.seed.text().strip() else None
            if seed is not None and not 0 <= seed <= 2**31 - 1:
                raise ValueError('Seed deve ficar entre 0 e 2147483647.')
            from studio.tts.timing import resolve_duration
            resolve_duration(duration, 1.0)
            records = [dict(row, text=self.context.current_text(row)) for row in self.records]
            regenerate_approved = self.regenerate_approved.isChecked()
        except Exception as exc:
            self.logs.appendPlainText(str(exc))
            return
        report = self.context.project_root / 'workspace/qt_batches' / (uuid.uuid4().hex + '.json')
        def generate(progress, cancel):
            ledger = {'records': [], 'settings': dict(speed=speed, duration=duration, seed=seed),
                      'complete': False, 'cancelled': False, 'counts': {}}
            generated_candidates = []
            json_atomico(report, ledger)
            for i, record in enumerate(records, 1):
                if cancel.is_set():
                    ledger['cancelled'] = True
                    break
                item = {'line_id': record['line_id'], 'text': record['text']}
                try:
                    if self.context.current_text(record) != record['text']:
                        item['status'] = 'skipped_text_changed'
                    elif (not regenerate_approved and
                          self.context.approved_path(record['line_id'], record['text'])):
                        item['status'] = 'preserved_approved'
                    else:
                        prepared = self.context.generation_request(record, record['text'], speed, duration, seed)
                        item['run'] = str(prepared['run'])
                        request_path = prepared.get('request')
                        if request_path is not None:
                            request_path = Path(request_path)
                            request = json.loads(request_path.read_text(encoding='utf-8'))
                            item.update(request=str(request_path), seed=request['seed'],
                                        duration_parameter=request.get('duration'),
                                        reference_path=request.get('reference_path'),
                                        reference_sha256=request.get('reference_sha256'))
                        self.child_job = self.context.jobs.submit_exclusive(prepared['argv'], cwd=self.context.project_root,
                            on_line=progress, timeout=None)
                        if cancel.is_set():
                            self.child_job.cancel()
                        result = self.child_job.wait()
                        self.child_job = None
                        if result.cancelled:
                            item['status'] = 'cancelled'
                        elif result.returncode != 0:
                            raise RuntimeError(result.error or '\n'.join(result.logs[-8:]) or 'O worker terminou sem gerar áudio.')
                        else:
                            output, receipt = Path(prepared['output']), Path(prepared['receipt'])
                            if not output.is_file():
                                # The worker preserves longer takes in its adjacent review directory.
                                quarantined = output.parent / 'quarantine' / output.name
                                if quarantined.is_file():
                                    output = quarantined
                                    receipt = receipt.parent / 'quarantine' / receipt.name
                            candidate = self.context.review_store.import_omnivoice_candidate(record['line_id'],
                                output, record['text'], receipt)
                            item.update(output=str(output), receipt=str(receipt))
                            item.update(status='generated_for_review', candidate_id=candidate['id'])
                            generated_candidates.append({'record': record, 'candidate': candidate})
                except Exception as exc:
                    item.update(status='failed', error=str(exc))
                ledger['records'].append(item)
                if cancel.is_set() and item.get('status') == 'cancelled':
                    ledger['cancelled'] = True
                json_atomico(report, ledger)
                progress('%d/%d · %s · %s' % (i, len(records), record['line_id'], item['status']))
            ledger['cancelled'] = ledger['cancelled'] or cancel.is_set()
            ledger['complete'] = len(ledger['records']) == len(records) and not ledger['cancelled']
            statuses = [item['status'] for item in ledger['records']]
            ledger['counts'] = {
                'generated': statuses.count('generated_for_review'),
                'preserved_approved': statuses.count('preserved_approved'),
                'failed': statuses.count('failed'),
                'skipped_text_changed': statuses.count('skipped_text_changed'),
                'cancelled': statuses.count('cancelled'),
                'not_started': len(records) - len(statuses),
            }
            json_atomico(report, ledger)
            return dict(ledger, candidates=generated_candidates)
        self.job = Task(generate, self)
        self.job.progress.connect(self.report_progress)
        self.report_path = report
        self.job.succeeded.connect(self.batch_succeeded)
        self.job.failed.connect(self.logs.appendPlainText)
        self.job.finished.connect(self.finished_job)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        for field in (self.speed, self.duration, self.seed, self.regenerate_approved):
            field.setEnabled(False)
        self.job.start()

    @Slot(object)
    def batch_succeeded(self, ledger):
        counts = ledger.get('counts', {})
        self.logs.appendPlainText(
            'Lote %s: %d gerado(s), %d preservado(s), %d falha(s), %d texto(s) alterado(s), '
            '%d cancelado(s), %d não iniciado(s). Registro: %s' % (
                'cancelado' if ledger.get('cancelled') else 'concluído',
                counts.get('generated', 0), counts.get('preserved_approved', 0),
                counts.get('failed', 0), counts.get('skipped_text_changed', 0),
                counts.get('cancelled', 0), counts.get('not_started', 0), self.report_path))
        candidates = ledger.get('candidates') or []
        if candidates:
            self.candidatesReady.emit(candidates)

    @Slot(str)
    def report_progress(self, text):
        self.logs.appendPlainText(text)
        prefix = text.split(' · ', 1)[0]
        try:
            done, total = (int(value) for value in prefix.split('/'))
        except ValueError:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def cancel(self):
        if self.job:
            self.job.cancel()
        if self.child_job:
            self.child_job.cancel()

    @Slot()
    def finished_job(self):
        finished = self.job
        # The QThread has finished, but its native object is deleted on the next
        # event turn. Detach it so closing this dialog cannot delete it twice
        # through parent ownership while that deferred deletion is pending.
        finished.wait()
        finished.setParent(None)
        finished.deleteLater()
        self.job = None
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        for field in (self.speed, self.duration, self.seed, self.regenerate_approved):
            field.setEnabled(True)

    def reject(self):
        if self.job:
            self.cancel()
            self.logs.appendPlainText('Cancelando. Aguarde o worker encerrar antes de fechar.')
            return
        super().reject()

    def closeEvent(self, event):
        if self.job:
            self.cancel()
            event.ignore()
        else:
            super().closeEvent(event)
