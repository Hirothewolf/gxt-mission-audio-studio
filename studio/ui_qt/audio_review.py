"""Classic review workflows using the current backend and Qt widgets."""
import uuid
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QAction, QKeySequence, QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QWidget, QTableWidget, QTableWidgetItem, QCheckBox, QFileDialog,
    QComboBox, QPlainTextEdit, QProgressBar, QMessageBox, QAbstractItemView, QDoubleSpinBox, QScrollArea)

from studio.projects.bulk_audio import collect_ai_rows, approve_rows
from studio.audio.wav import wav_duracao
from studio.audio.analyzer import analyze_wav
from studio.audio.effects import render_effect
from .generation import GenerationWidget, Operation


def normalize_record(record):
    row = dict(record)
    row.setdefault('line_id', '%s|%08x' % (row['table'], row['hash']))
    row.setdefault('tab', row['table'])
    row.setdefault('key', row['hash'])
    row.setdefault('label', row.get('name') or row['line_id'])
    return row


def buttons(layout, actions):
    bar = QHBoxLayout()
    result = []
    for label, callback in actions:
        button = QPushButton(label)
        button.clicked.connect(callback)
        bar.addWidget(button)
        result.append(button)
    layout.addLayout(bar)
    return result


def result_summary(result):
    text = '%d aprovados · %d instalados · %d pulados · %d erros' % (
        len(result.get('approved', [])), len(result.get('installed', [])),
        len(result.get('skipped', [])), len(result.get('errors', [])))
    if result.get('cancelled'):
        text += ' · cancelado'
    details = result.get('errors', []) + result.get('skipped', [])
    if details:
        text += '\n' + '\n'.join(str(item.get('reason', item)) if isinstance(item, dict)
                                 else str(item) for item in details[:8])
    return text


def review_status_label(code):
    return {'approved': 'Aprovado', 'pending': 'Para ouvir', 'rejected': 'Recusado',
            'stale': 'Texto desatualizado', 'legacy': 'AI sem vínculo'}.get(str(code).lower(), code)


class Waveform(QWidget):
    """Bounded analyzer peaks; this view never changes source samples."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks = []
        self.setMinimumHeight(60)
        self.setMaximumHeight(88)
        self.setAccessibleName('Forma de onda do take selecionado')

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#19222d'))
        center = self.height() / 2
        painter.setPen(QPen(QColor('#455a70'), 1))
        painter.drawLine(0, int(center), self.width(), int(center))
        painter.setPen(QPen(QColor('#62c8bb'), 1))
        for index, (low, high) in enumerate(self.peaks):
            x = int((index + .5) * self.width() / len(self.peaks))
            painter.drawLine(x, int(center - min(1., high) * center),
                             x, int(center - max(-1., low) * center))


class AudioReviewDialog(QDialog):
    selectionChanged = Signal(object, object)
    textChanged = Signal(object, str)
    sceneRequested = Signal(object)

    def __init__(self, context, record, text, parent=None):
        super().__init__(parent)
        self.context, self.record, self.text = context, normalize_record(record), text
        self.store = context.review_store
        self.worker = None
        self.analysis_worker = None
        self.analysis_pending = None
        self.analysis_token = 0
        self.worker_kind = None
        self.probes = {}
        self.probe_inflight = set()
        self.probe_callbacks = {}
        self.original_ready = False
        self.original = None
        self.installed_ready = False
        self.installed_audio = None
        self.source_durations = {}
        self.source_errors = {}
        self.entries = []
        self.pending_choice = None
        self._closing = False
        self._syncing_text = False
        self._last_external_text = text
        self._text_pending = False
        self.text_timer = QTimer(self)
        self.text_timer.setSingleShot(True)
        self.text_timer.setInterval(300)
        self.text_timer.timeout.connect(self.flush_text)
        self.audio_info = {}
        self.candidates = []
        self.setWindowTitle('Revisão de voz — ' + self.record['line_id'])
        self.resize(900, 780)
        shell = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        shell.addWidget(scroll, 1)
        self.context_label = QLabel()
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        self.scene_button = QPushButton('Abrir cena completa…')
        self.scene_button.clicked.connect(lambda: self.sceneRequested.emit(dict(self.record, text=self.current_text())))
        layout.addWidget(self.scene_button)
        layout.addWidget(QLabel('Texto da fala'))
        self.text_editor = QPlainTextEdit(text)
        self.text_editor.setMaximumHeight(64)
        self.text_editor.textChanged.connect(self.edited_text)
        layout.addWidget(self.text_editor)
        # The editable text, all audition sources, player and generator share one page.
        self.text_label = self.text_editor
        self.duration_label = QLabel()
        layout.addWidget(self.duration_label)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Origem', 'Duração', 'Revisão', 'Texto'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 75)
        self.table.setColumnWidth(2, 115)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        locate = QAction('Abrir pasta do áudio selecionado', self.table)
        locate.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.selected_path().parent))) if self.selected_path() else None)
        self.table.addAction(locate)
        self.table.setMinimumHeight(140)
        self.table.setMaximumHeight(180)
        layout.addWidget(self.table)
        self.waveform = Waveform()
        layout.addWidget(self.waveform)
        self.analysis_label = QLabel('Selecione um áudio para ouvir e ver suas medições.')
        self.analysis_label.setWordWrap(True)
        layout.addWidget(self.analysis_label)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.take_buttons = buttons(layout, [('Ouvir selecionado', self.play_take),
            ('Comparar A/B', self.compare), ('Parar', context.player.stop),
            ('Importar WAV', self.import_wav), ('Usar áudio selecionado', self.choose_selected)])
        self.use_button = self.take_buttons[-1]
        self.use_button.setToolTip('Takes válidos já ficam escolhidos ao selecionar. Para original e instalado, este botão confirma a escolha.')
        self.generation = GenerationWidget(context, self.record, self.current_text, self)
        if self.record.get('reference_override'):
            layout.addWidget(QLabel('Palavras em inglês do trecho de referência (opcional)'))
            reference_text = QPlainTextEdit(self.record['reference_override'].get('transcript', ''))
            reference_text.setMaximumHeight(55)
            reference_text.setPlaceholderText('Se cortou parte da fala, informe só as palavras desse trecho. Vazio usa a legenda original da chave.')
            reference_text.textChanged.connect(lambda: self.record['reference_override'].update(transcript=reference_text.toPlainText().strip()))
            layout.addWidget(reference_text)
        layout.addWidget(self.generation)
        self.generation.candidateReady.connect(self.generated)
        self.generation.busyChanged.connect(self.set_busy)
        self.details_button = QPushButton('Referências e efeitos ▸')
        self.details_button.setCheckable(True)
        layout.addWidget(self.details_button)
        self.tabs = QTabWidget()
        self.tabs.hide()
        self.details_button.toggled.connect(self.tabs.setVisible)
        self.details_button.toggled.connect(lambda expanded: self.details_button.setText(
            'Referências e efeitos ▾' if expanded else 'Referências e efeitos ▸'))
        layout.addWidget(self.tabs)
        takes = QWidget()
        takes_layout = QVBoxLayout(takes)
        self.tabs.addTab(takes, 'Efeitos e revisão')
        self.override = QCheckBox('Usar mesmo além da tolerância de 2 s (decisão manual)')
        self.override.toggled.connect(self.choose_selected)
        takes_layout.addWidget(self.override)
        buttons(takes_layout, [('Recusar take', lambda: self.set_status('rejected'))])
        effect_controls = QHBoxLayout()
        self.effect_speed = QDoubleSpinBox()
        self.effect_speed.setRange(.5, 2.)
        self.effect_speed.setValue(1.)
        self.effect_speed.setSingleStep(.05)
        self.effect_pitch = QDoubleSpinBox()
        self.effect_pitch.setRange(-12., 12.)
        self.effect_pitch.setSingleStep(.5)
        self.effect_reverb = QDoubleSpinBox()
        self.effect_reverb.setRange(0., 1.)
        self.effect_reverb.setSingleStep(.05)
        for name, widget in [('Velocidade WAV', self.effect_speed), ('Tom (semitons)', self.effect_pitch),
                             ('Reverberação', self.effect_reverb)]:
            effect_controls.addWidget(QLabel(name))
            effect_controls.addWidget(widget)
        self.effect_button = QPushButton('Criar take com efeito')
        self.effect_button.clicked.connect(self.effect)
        effect_controls.addWidget(self.effect_button)
        takes_layout.addLayout(effect_controls)
        references = QWidget()
        references_layout = QVBoxLayout(references)
        self.tabs.addTab(references, 'Referências')
        self.character = QComboBox()
        self.character.setEditable(True)
        self.character.addItems(self.store.characters())
        self.character.setCurrentText(self.store.character(self.record['line_id']))
        references_layout.addWidget(QLabel('Personagem confirmado manualmente'))
        references_layout.addWidget(self.character)
        self.reference_list = QComboBox()
        references_layout.addWidget(self.reference_list)
        self.transcript = QPlainTextEdit()
        self.transcript.setPlaceholderText('Transcrição no idioma falado da referência')
        references_layout.addWidget(self.transcript)
        self.character.currentTextChanged.connect(self.refresh_references)
        self.reference_list.currentIndexChanged.connect(self.select_reference)
        buttons(references_layout, [('Guardar personagem', self.save_character),
            ('Guardar original', self.add_original_reference), ('Importar referência', self.import_reference),
            ('Ouvir', self.play_reference), ('Remover', self.remove_reference)])
        # Kept as an API alias; the primary flow stages the choice for the editor's Save.
        self.install_button = self.use_button
        self.status_label = QLabel('Escolha um take e use Salvar no editor para guardar texto e áudio juntos. Original e instalado são apenas para ouvir até clicar em Usar áudio selecionado.')
        self.status_label.setWordWrap(True)
        shell.addWidget(self.status_label)
        self.save_button = QPushButton('Salvar texto e áudio (Ctrl+S)')
        callback = getattr(parent, 'save_file', None)
        self.save_button.setEnabled(bool(callback))
        if callback:
            self.save_button.clicked.connect(lambda: callback())
            shortcut = QAction(self)
            shortcut.setShortcut(QKeySequence('Ctrl+S'))
            shortcut.triggered.connect(lambda: callback())
            self.addAction(shortcut)
        footer = QHBoxLayout()
        self.generation.layout().removeWidget(self.generation.start_button)
        footer.addWidget(self.generation.start_button)
        footer.addWidget(self.save_button)
        shell.addLayout(footer)
        self.operation_progress = QProgressBar()
        self.operation_progress.hide()
        shell.addWidget(self.operation_progress)
        self.update_context()
        self.refresh()

    def update_context(self):
        info = self.audio_info or self.record.get('info') or {}
        stable = str(info.get('stable_audio_id') or '')
        family = next((str(info.get(key) or '').upper() for key in
                       ('container', 'archive', 'pak', 'package', 'audio_type', 'source_type')
                       if str(info.get(key) or '').upper() in {'CUTSCENE', 'BEATS'}), '')
        if not family and stable.upper().startswith(('CUTSCENE:', 'BEATS:')):
            family = stable.split(':', 1)[0].upper()
        route = info.get('audio_route') or {}
        family = family or (str(route.get('container') or '').upper()
                            if route.get('family') == 'streams' else '')
        from studio.audio.source_routing import audio_route
        try:
            native_route = audio_route(info)
        except ValueError:
            native_route = None
        if native_route and native_route['family'] == 'streams':
            family = native_route['container']
            route = native_route
        is_scene = family in {'CUTSCENE', 'BEATS'}
        track = info.get('track') or info.get('stream_track') or (info.get('stream') or {}).get('track') or route.get('track') or info.get('scene')
        if not track and is_scene and ':' in stable:
            track = stable.split(':')[1]
        label = '%s · %s' % (family, track or 'cena vinculada') if is_scene else self.record['line_id']
        self.context_label.setText('%s · %s' % (self.record.get('name') or self.record['table'], label))
        self.scene_button.setVisible(is_scene)
        self.setWindowTitle('Texto e voz — ' + label)

    def edited_text(self):
        if self._syncing_text:
            return
        self.pending_choice = None
        self._text_pending = True
        self.text_timer.start()
        self.status_label.setText('Texto alterado. Selecione um take correspondente ou gere uma nova tentativa antes de salvar o áudio.')

    def flush_text(self):
        if not self._text_pending:
            return
        self.text_timer.stop()
        self._text_pending = False
        text = self.text_editor.toPlainText()
        self.textChanged.emit(dict(self.record), text)
        self.selectionChanged.emit(dict(self.record, text=text), None)
        self.refresh()

    def selection_changed(self):
        entry = self.selected_entry()
        self.use_button.setVisible(bool(entry and entry.get('kind') == 'source'))
        if self.selected():
            self.pending_choice = None
            self.selectionChanged.emit(dict(self.record, text=self.current_text()), None)
        self.request_analysis(stage=bool(self.selected()))

    def choose_selected(self, *args):
        self.request_analysis(stage=True)

    def request_analysis(self, stage=False):
        if self._closing:
            return
        self.analysis_token += 1
        entry = self.selected_entry()
        self.waveform.set_peaks([])
        if not entry:
            self.analysis_pending = None
            return
        candidate = self.selected()
        if stage and candidate and (candidate.get('stale') or candidate.get('text') != self.current_text()):
            stage = False
            self.status_label.setText('Texto alterado: este take pode ser ouvido, mas precisa corresponder ao texto atual para ser escolhido.')
        if stage and candidate and candidate.get('status') == 'rejected':
            stage = False
            self.status_label.setText('Take recusado. Gere ou importe uma nova tentativa.')
        path = self.entry_path(entry)
        if not path:
            self.analysis_pending = None
            self.analysis_label.setText('Áudio indisponível ou ainda carregando.')
            if stage:
                self.status_label.setText('Aguarde o carregamento do áudio antes de escolhê-lo.')
            return
        choice = dict(candidate) if stage and candidate else (
            dict(entry, path=str(path)) if stage else None)
        self.analysis_pending = (self.analysis_token, path, choice, self.current_text())
        self.analysis_label.setText('Conferindo o áudio selecionado…')
        self.start_pending_analysis()

    def start_pending_analysis(self):
        if self._closing or not self.analysis_pending or (self.analysis_worker and self.analysis_worker.isRunning()):
            return
        token, path, choice, snapshot = self.analysis_pending
        self.analysis_pending = None
        line_id = self.record['line_id']
        def operation(cancel, progress):
            if choice and choice.get('kind') != 'source':
                self.store.validate_candidate_source(line_id, choice['id'])
            return token, analyze_wav(path), choice, snapshot, cancel.is_set()
        self.analysis_worker = Operation(operation, self)
        self.analysis_worker.succeeded.connect(self.analysis_ready)
        self.analysis_worker.failed.connect(self.analysis_failed)
        self.analysis_worker.finished.connect(self.start_pending_analysis)
        self.analysis_worker.start()

    def analysis_ready(self, result):
        token, analysis, choice, snapshot, cancelled = result
        if self._closing or cancelled or token != self.analysis_token:
            return
        if not analysis.get('valid'):
            self.analysis_label.setText('WAV inválido: ' + str(analysis.get('error')))
            return
        self.waveform.set_peaks(analysis['waveform'])
        self.analysis_label.setText('%.2fs · %s Hz · pico %.1f%% · silêncio %.1f%% · clipping %.2f%%' % (
            analysis['duration'], analysis['sample_rate'], 100 * analysis['peak'],
            100 * analysis['silence_ratio'], 100 * analysis['clipping_ratio']))
        if not choice or snapshot != self.current_text():
            return
        if choice.get('kind') != 'source':
            if analysis.get('silence_ratio', 1.) > .98 or analysis.get('clipping_ratio', 1.) > .01:
                self.status_label.setText('Take silencioso ou com clipping excessivo. Gere ou importe uma nova tentativa.')
                return
            metadata = choice.get('metadata') or {}
            window = metadata.get('window_seconds') or self.audio_info.get('duration_seconds') or self.source_durations.get('original')
            if window:
                difference = abs(analysis['duration'] - float(window))
                if difference > 2. and not self.override.isChecked():
                    self.status_label.setText('Diferença de duração de %.2f s. Escolha outro take ou autorize a diferença nos detalhes.' % difference)
                    return
                choice['duration_override'] = bool(metadata.get('duration_override_required'))
            elif metadata.get('duration_override_required') and not self.override.isChecked():
                self.status_label.setText('Falta a duração original para conferir a tolerância. Aguarde o original ou autorize a diferença nos detalhes.')
                return
            else:
                choice['duration_override'] = bool(metadata.get('duration_override_required') and self.override.isChecked())
        choice['text'] = snapshot
        self.pending_choice = choice
        self.selectionChanged.emit(dict(self.record, text=snapshot), choice)
        label = {'original': 'Original', 'installed': 'Instalado'}.get(choice.get('source'), 'Take')
        self.status_label.setText('%s escolhido. Salvar no editor guardará o texto e este áudio juntos.' % label)

    def analysis_failed(self, error):
        if not self._closing and not self.analysis_pending:
            self.analysis_label.setText('Áudio não pode ser escolhido: ' + error)

    def effect(self):
        if self.worker and self.worker.isRunning():
            if self.worker_kind == 'effect':
                self.worker.cancel()
                self.status_label.setText('Cancelando efeito; o take fonte será preservado.')
            return
        if self.generation.busy:
            return
        candidate = self.selected()
        if not candidate:
            self.status_label.setText('Selecione um take importado ou gerado como fonte do efeito.')
            return
        candidate = dict(candidate)
        source = self.store.resolve_path(candidate['path'])
        destination = self.store.root / 'workspace' / 'studio_runs' / 'effects' / (uuid.uuid4().hex + '.wav')
        parameters = {'speed': self.effect_speed.value(), 'pitch_semitones': self.effect_pitch.value(),
                      'reverb': self.effect_reverb.value()}
        def operation(cancel, progress):
            info = self.context.info(self.record)
            window = candidate.get('metadata', {}).get('window_seconds') or info.get('duration_seconds')
            if not window:
                original = self.context.original_path(self.record)
                window = wav_duracao(original) if original else None
            if cancel.is_set():
                raise ValueError('Efeito cancelado antes de renderizar.')
            verified = self.store.validate_candidate_source(self.record['line_id'], candidate['id'])
            progress(0, 2, 'Criando novo WAV; o take fonte permanece intacto…')
            analysis = render_effect(source, destination, cancel_event=cancel, **parameters)
            self.store.validate_candidate_source(self.record['line_id'], candidate['id'])
            if cancel.is_set():
                raise ValueError('Efeito cancelado; nenhum take foi importado.')
            metadata = {'source': str(source), 'effects': dict(parameters, source_candidate_id=candidate['id'],
                source_sha256=candidate['sha256'])}
            if window:
                metadata.update(window_seconds=float(window),
                    duration_override_required=analysis['duration'] > float(window) + (1 / analysis['sample_rate']))
            progress(1, 2, 'Guardando derivação para revisão…')
            return self.store.import_candidate(self.record['line_id'], destination, verified['text'],
                'efeito', metadata, analysis)
        self.worker_kind = 'effect'
        self.worker = Operation(operation, self)
        self.set_busy(True)
        self.generation.setEnabled(False)
        self.effect_button.setEnabled(True)
        self.effect_button.setText('Cancelar efeito')
        self.worker.progress.connect(self.show_operation_progress)
        self.worker.succeeded.connect(self.effect_ready)
        self.worker.failed.connect(self.status_label.setText)
        self.worker.finished.connect(self.operation_finished)
        self.worker.start()

    def show_operation_progress(self, done, total, label):
        self.operation_progress.setRange(0, total)
        self.operation_progress.setValue(done)
        self.status_label.setText(label)

    def effect_ready(self, candidate):
        self.generated(candidate)
        self.status_label.setText('Novo take com efeito guardado para revisão. O take fonte foi preservado.')

    def current_text(self):
        self.flush_text()
        callback = getattr(self.context, 'current_text', None)
        external = callback(self.record) if callback else self._last_external_text
        if external != self._last_external_text:
            self._last_external_text = external
            self._syncing_text = True
            try:
                self.text_editor.setPlainText(external)
            finally:
                self._syncing_text = False
        return self.text_editor.toPlainText()

    def report(self, callback):
        try:
            return callback()
        except Exception as exc:
            self.status_label.setText(str(exc))

    def refresh(self, candidate_id=None):
        if self._closing:
            return
        previous = self.selected_entry()
        previous_key = self.entry_key(previous)
        self.candidates = self.store.list_candidates(self.record['line_id'], self.current_text())
        ids = {candidate['id'] for candidate in self.candidates}
        if candidate_id not in ids:
            candidate_id = previous.get('id') if previous and previous.get('id') in ids else None
        source = previous.get('source') if previous and previous.get('kind') == 'source' else None
        if candidate_id is None and not source:
            preferred = next((candidate for candidate in self.candidates
                              if candidate['status'] == 'approved' and not candidate['stale']), None)
            preferred = preferred or (self.candidates[0] if self.candidates else None)
            candidate_id = preferred['id'] if preferred else None
        self.entries = [dict(kind='source', source=kind, path=path)
                        for kind, path in [('original', self.original), ('installed', self.installed_audio)]]
        self.entries.extend(self.candidates)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.entries))
        for index, entry in enumerate(self.entries):
            if entry.get('kind') == 'source':
                kind = entry['source']
                duration = self.source_durations.get(kind)
                ready = self.original_ready if kind == 'original' else self.installed_ready
                state = 'Para ouvir' if entry.get('path') else ('Indisponível' if ready else 'Carregando…')
                values = ({'original': 'Original do jogo', 'installed': 'Instalado no mod'}[kind],
                          '%.2fs' % duration if duration else '—', state, 'Fonte')
                selected = (kind == source or (not candidate_id and not source and kind == 'original'))
            else:
                duration = (entry.get('analysis') or {}).get('duration') or (entry.get('metadata') or {}).get('duration_seconds')
                if not duration:
                    duration = wav_duracao(self.store.resolve_path(entry['path']))
                values = (entry['origin'], '%.2fs' % duration if duration else '—',
                          review_status_label(entry['status']), 'Desatualizado' if entry['stale'] else 'Atual')
                selected = entry['id'] == candidate_id
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.table.setItem(index, column, cell)
            if selected:
                self.table.selectRow(index)
        self.table.blockSignals(False)
        entry = self.selected_entry()
        self.use_button.setVisible(bool(entry and entry.get('kind') == 'source'))
        current_key = self.entry_key(self.selected_entry())
        if current_key != previous_key:
            self.request_analysis()
        if not self.original_ready:
            self.duration_label.setText('Conferindo o áudio original em segundo plano…')
            self.resolve_audio('original')
        if not self.installed_ready:
            self.resolve_audio('installed')
        self.refresh_references()

    def resolve_audio(self, kind, callback=None):
        """Native extraction/hash probes never run on the Qt event thread."""
        if self._closing:
            return
        if ((kind == 'original' and self.original_ready) or
                (kind == 'installed' and self.installed_ready)):
            if callback:
                callback(self.original if kind == 'original' else self.installed_audio)
            return
        if callback:
            self.probe_callbacks.setdefault(kind, []).append(callback)
        active = self.probes.get(kind)
        if kind in self.probe_inflight or (active and active.isRunning()):
            return
        record = dict(self.record)
        snapshot = self.current_text() if kind == 'approved' else None
        def operation(cancel, progress):
            if kind == 'original':
                path = self.context.original_path(record)
                info = self.context.info(record)
                duration = wav_duracao(path) if path else None
            elif kind == 'installed':
                path, info = self.context.installed_path(record), {}
                duration = wav_duracao(path) if path else None
            else:
                path, info, duration = self.context.approved_path(record['line_id'], snapshot), {}, None
            return kind, path, info, duration, cancel.is_set()
        worker = Operation(operation, self)
        self.probes[kind] = worker
        self.probe_inflight.add(kind)
        worker.succeeded.connect(self.probe_ready)
        worker.failed.connect(self.probe_failed)
        worker.start()

    def reload_sources(self):
        if self.operation_active() or self._closing:
            return
        self.original_ready = self.installed_ready = False
        self.original = self.installed_audio = None
        self.source_durations.clear()
        self.refresh()

    def probe_ready(self, result):
        kind, path, info, duration, cancelled = result
        self.probe_inflight.discard(kind)
        if self._closing or cancelled:
            self.probe_callbacks.pop(kind, None)
            return
        if kind == 'original':
            self.original_ready, self.original, self.audio_info = True, path, info
            self.update_context()
            self.duration_label.setText('Original: %s · janela mapeada: %s s' % (
                '%.2fs' % duration if duration else 'indisponível',
                '%.2f' % (info.get('duration_seconds') or duration) if info.get('duration_seconds') or duration else 'não disponível'))
        elif kind == 'installed':
            self.installed_ready, self.installed_audio = True, path
        self.source_durations[kind] = duration
        if kind in {'original', 'installed'}:
            self.refresh()
        callbacks = self.probe_callbacks.pop(kind, [])
        if not cancelled:
            for callback in callbacks:
                self.report(lambda callback=callback: callback(path))

    def probe_failed(self, error):
        if self._closing:
            return
        worker = self.sender()
        for kind, probe in self.probes.items():
            if probe is worker:
                self.probe_inflight.discard(kind)
                self.probe_callbacks.pop(kind, None)
                self.source_errors[kind] = error
                if kind == 'original':
                    self.original_ready = True
                    self.duration_label.setText('Original indisponível: ' + error)
                elif kind == 'installed':
                    self.installed_ready = True
                break
        self.refresh()
        self.status_label.setText(error)

    def play_approved(self):
        self.resolve_audio('approved', self.play_path)

    @staticmethod
    def entry_key(entry):
        if not entry:
            return None
        return (entry.get('source'), str(entry.get('path'))) if entry.get('kind') == 'source' else (entry['id'], entry['sha256'])

    def selected_entry(self):
        index = self.table.currentRow()
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def selected(self):
        entry = self.selected_entry()
        return entry if entry and entry.get('kind') != 'source' else None

    def entry_path(self, entry):
        if not entry or not entry.get('path'):
            return None
        return Path(entry['path']) if entry.get('kind') == 'source' else self.store.resolve_path(entry['path'])

    def selected_path(self):
        path = self.entry_path(self.selected_entry())
        if not path:
            raise ValueError('Áudio selecionado indisponível ou ainda carregando.')
        return path

    def play_path(self, path):
        self.report(lambda: self.context.player.play(path) if path else self.status_label.setText('Áudio indisponível.'))

    def play_take(self):
        self.report(lambda: self.context.player.play(self.selected_path()))

    def compare(self):
        def resolved(original):
            if not original:
                raise ValueError('Original indisponível para comparação.')
            self.context.player.compare([original, self.selected_path()])
        self.report(lambda: self.resolve_audio('original', resolved))

    def import_wav(self):
        source, _ = QFileDialog.getOpenFileName(self, 'Importar take', '', 'WAV (*.wav)')
        if source:
            snapshot = self.current_text()
            self.worker_kind = 'import'
            self.worker = Operation(lambda cancel, progress: self.store.import_candidate(
                self.record['line_id'], source, snapshot, 'importado'), self)
            self.set_busy(True)
            self.generation.setEnabled(False)
            self.worker.succeeded.connect(self.generated)
            self.worker.failed.connect(self.status_label.setText)
            self.worker.finished.connect(self.operation_finished)
            self.worker.start()

    def import_source(self, source):
        def operation():
            candidate = self.store.import_candidate(self.record['line_id'], source, self.current_text(), 'importado')
            self.generated(candidate)
            self.status_label.setText('Take importado e selecionado. Conferindo para salvar com o texto…')
            return candidate
        return self.report(operation)

    def set_status(self, status):
        def operation():
            candidate = self.selected()
            if not candidate:
                raise ValueError('Selecione um take.')
            override = status == 'approved' and self.override.isChecked() and bool(
                candidate.get('metadata', {}).get('duration_override_required'))
            self.store.set_status(self.record['line_id'], candidate['id'], status, self.current_text(), duration_override=override)
            self.refresh(candidate['id'])
            self.status_label.setText('Revisão salva no projeto.')
        self.report(operation)

    def save_character(self):
        self.report(lambda: self.store.assign_character(self.record['line_id'], self.character.currentText()))

    def refresh_references(self, *args):
        self.reference_list.blockSignals(True)
        self.reference_list.clear()
        for reference in self.store.references(self.character.currentText()):
            self.reference_list.addItem('%s · %.2fs' % (reference['name'], reference['duration']), reference)
        self.reference_list.blockSignals(False)
        self.select_reference()

    def select_reference(self, *args):
        reference = self.reference_list.currentData()
        if reference:
            self.transcript.setPlainText(reference['transcript'])

    def add_reference(self, source, provenance, character=None, transcript=None):
        character = self.character.currentText() if character is None else character
        transcript = self.transcript.toPlainText() if transcript is None else transcript
        def operation():
            if not transcript.strip():
                raise ValueError('Informe a transcrição original da referência antes de guardar.')
            self.store.add_reference(character, source, transcript, provenance)
            self.store.assign_character(self.record['line_id'], character)
            self.refresh_references()
        self.report(operation)

    def add_original_reference(self):
        character, transcript = self.character.currentText(), self.transcript.toPlainText()
        def resolved(source):
            if source:
                self.add_reference(source, dict(self.audio_info, source='original', line_id=self.record['line_id']), character, transcript)
            else:
                self.status_label.setText('Original indisponível.')
        self.resolve_audio('original', resolved)

    def import_reference(self):
        source, _ = QFileDialog.getOpenFileName(self, 'Importar referência', '', 'WAV (*.wav)')
        if source:
            self.add_reference(source, {'source': 'importado'})

    def play_reference(self):
        reference = self.reference_list.currentData()
        if reference:
            self.play_path(self.store.resolve_path(reference['path']))

    def remove_reference(self):
        reference = self.reference_list.currentData()
        if reference:
            self.report(lambda: self.store.remove_reference(self.character.currentText(), reference['id']))
            self.refresh_references()

    def generated(self, candidate):
        if self._closing:
            return
        self.refresh(candidate['id'])
        if self.selected() and self.selected()['id'] == candidate['id']:
            self.request_analysis(stage=True)

    def set_busy(self, busy):
        for button in self.take_buttons:
            button.setEnabled(not busy)
        self.install_button.setEnabled(not busy)
        self.effect_button.setEnabled(not busy)
        if busy:
            self.operation_progress.setRange(0, 0)
        self.operation_progress.setVisible(busy)

    def install(self):
        """Compatibility entry point: Save in the editor owns installation."""
        self.choose_selected()

    def installed(self, result):
        self.status_label.setText(result_summary(result))
        self.refresh()

    def operation_finished(self):
        self.set_busy(False)
        self.generation.setEnabled(True)
        self.effect_button.setText('Criar take com efeito')
        self.worker_kind = None

    def operation_active(self):
        return (self.generation.busy or (self.worker and self.worker.isRunning()) or
                (self.analysis_worker and self.analysis_worker.isRunning()) or
                any(worker.isRunning() for worker in self.probes.values()))

    def request_cancel(self):
        self.generation.cancel()
        self.analysis_token += 1
        self.analysis_pending = None
        self.probe_callbacks.clear()
        if self.analysis_worker:
            self.analysis_worker.cancel()
        for worker in self.probes.values():
            worker.cancel()
        if self.worker and self.worker_kind != 'install':
            self.worker.cancel()

    def reject(self):
        self.flush_text()
        if self.operation_active():
            self.request_cancel()
            self.status_label.setText('Aguarde o término da operação antes de fechar.')
            return
        self.context.player.stop()
        self._closing = True
        super().reject()

    def closeEvent(self, event):
        self.flush_text()
        if self.operation_active():
            self.request_cancel()
            self.status_label.setText('Aguarde o término da operação antes de fechar.')
            event.ignore()
        else:
            self.context.player.stop()
            self._closing = True
            super().closeEvent(event)


class BulkAudioReviewDialog(QDialog):
    def __init__(self, context, records, parent=None):
        super().__init__(parent)
        self.context = context
        self.records = [normalize_record(r) for r in records]
        self.rows = []
        self.worker = None
        self.setWindowTitle('Revisão de áudio em lote')
        self.resize(850, 550)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Fala', 'Estado', 'Detalhes'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.override = QCheckBox('Aceitar explicitamente diferenças de duração')
        layout.addWidget(self.override)
        self.actions = buttons(layout, [('Atualizar', self.scan), ('Ouvir', self.listen),
            ('Confirmar selecionados', lambda: self.confirm(False)), ('Confirmar todos válidos', lambda: self.confirm(True)),
            ('Aplicar aprovados ao mod…', self.install)])
        self.cancel_button = QPushButton('Cancelar após a fala atual')
        self.cancel_button.clicked.connect(self.cancel)
        layout.addWidget(self.cancel_button)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.scan()

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def run(self, operation, callback):
        if self.busy():
            return
        self.worker = Operation(operation, self)
        for button in self.actions:
            button.setEnabled(False)
        self.worker.succeeded.connect(callback)
        self.worker.failed.connect(self.status_label.setText)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished.connect(self.finished_operation)
        self.worker.start()

    def show_progress(self, done, total, label):
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.status_label.setText(label)

    def finished_operation(self):
        for button in self.actions:
            button.setEnabled(True)

    def scan(self):
        rows = []
        for record in self.records:
            row = dict(record)
            callback = getattr(self.context, 'current_text', None)
            row['text'] = callback(record) if callback else record.get('text', '')
            rows.append(row)
        def operation(cancel, progress):
            found = []
            for index, row in enumerate(rows):
                if cancel.is_set():
                    break
                found.extend(collect_ai_rows(self.context.review_store, [row]))
                progress(index + 1, len(rows), row['line_id'])
            return found
        self.run(operation, self.render)

    def render(self, rows):
        self.rows = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate((row['line_id'], review_status_label(row['state']), row['reason'])):
                self.table.setItem(index, column, QTableWidgetItem(value))

    def selected_rows(self):
        return [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()]

    def listen(self):
        rows = self.selected_rows()
        if rows and rows[0].get('path'):
            try:
                self.context.player.play(rows[0]['path'])
            except Exception as exc:
                self.status_label.setText(str(exc))

    def validate_rows(self, rows):
        callback = getattr(self.context, 'current_text', None)
        if callback and any(callback(row) != row['text'] for row in rows):
            raise ValueError('Texto alterado desde a listagem. Atualize a lista.')

    def confirm(self, all_rows=True):
        if self.busy():
            return
        rows = list(self.rows) if all_rows else self.selected_rows()
        try:
            self.validate_rows(rows)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        if not rows:
            return
        override = self.override.isChecked()
        def operation(cancel, progress):
            result = {'approved': [], 'skipped': [], 'errors': []}
            for index, row in enumerate(rows):
                if cancel.is_set():
                    result['cancelled'] = True
                    break
                part = approve_rows(self.context.review_store, [row], duration_override=override)
                for key in ('approved', 'skipped', 'errors'):
                    result[key].extend(part[key])
                progress(index + 1, len(rows), row['line_id'])
            return result
        self.run(operation, self.completed)

    def completed(self, result):
        self.status_label.setText(result_summary(result))
        # Refresh states synchronously from the store; no audio decoding or game writes.
        self.render(collect_ai_rows(self.context.review_store, self.rows))

    def install(self):
        if self.busy():
            return
        rows = [row for row in self.rows if row['state'] == 'APPROVED']
        try:
            self.validate_rows(rows)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        if not rows:
            self.status_label.setText('Confirme os takes e atualize a lista primeiro.')
            return
        if QMessageBox.question(self, 'Aplicar aprovados', 'Aplicar %d falas ao mod? Feche o GTA. Os bancos serão reconstruídos com backup e conferência.' % len(rows)) != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText('Aplicando bancos; aguarde a conferência. Esta etapa não pode ser interrompida no meio de um banco.')
        self.run(lambda cancel, progress: self.context.install(rows), self.completed)

    def cancel(self):
        if self.worker:
            self.worker.cancel()

    def reject(self):
        if self.busy():
            self.cancel()
            self.status_label.setText('Cancelamento solicitado; aguarde o término da operação.')
            return
        super().reject()

    def closeEvent(self, event):
        if self.busy():
            self.cancel()
            event.ignore()
            self.status_label.setText('Aguarde o término da operação antes de fechar.')
        else:
            super().closeEvent(event)
