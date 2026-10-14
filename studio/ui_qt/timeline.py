"""Layered timeline editor using the current backend and a non-destructive project."""
from __future__ import annotations

import copy
import json
import threading
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QThread, Signal, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QCheckBox, QDoubleSpinBox, QSpinBox, QFormLayout, QSplitter, QScrollArea,
    QPlainTextEdit, QProgressBar, QLineEdit, QComboBox, QToolButton, QMenu)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from studio.audio.timeline_project import TimelineProject, read_pcm


def _peaks(project, cancel=None):
    result = {}
    for sid, source in project.data['sources'].items():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Operação cancelada.')
        stat = Path(source['path']).stat()
        result[sid] = _source_peaks(source['path'], source['sha256'], stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return result


@lru_cache(maxsize=128)
def _source_peaks(path, sha, size, mtime, ctime):
    import numpy as np
    data, _ = read_pcm(path)
    amplitude = np.max(np.abs(data), axis=1)
    step = max(1, (len(amplitude) + 2999) // 3000)
    padded = np.pad(amplitude, (0, (-len(amplitude)) % step))
    return np.max(padded.reshape(-1, step), axis=1).tolist()


class _Job(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel_event = threading.Event()

    def run(self):
        try:
            value = self.operation(self.cancel_event, self.progress.emit)
            if self.cancel_event.is_set():
                raise InterruptedError('Operação cancelada.')
            self.completed.emit(value)
        except Exception as exc:
            self.failed.emit(str(exc))


class ClipItem(QGraphicsItem):
    def __init__(self, view, clip, row):
        super().__init__()
        self.view, self.clip_id = view, clip['id']
        self.height = view.window._track_row_height(row) - 12
        self.length = view.window.project.clip_frames(clip) / view.window.project.sample_rate * view.pixels_per_second
        self.setPos(clip['position'] / view.window.project.sample_rate * view.pixels_per_second,
                    view.window._track_row_top(row) + 6)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        self.setAcceptHoverEvents(True)
        self.setToolTip('Arraste: mover · bordas: aparar · clique: selecionar')
        self.drag = None

    def boundingRect(self):
        return QRectF(0, 0, max(2, self.length), self.height)

    def paint(self, painter, option, widget=None):
        project = self.view.window.project
        clip = project.clip(self.clip_id)
        track = project.track(clip['track_id'])
        color = {'original': '#697888', 'installed': '#88735e', 'pt': '#276d84', 'ambience': '#586d40'}[track['role']]
        painter.setBrush(QColor(color))
        painter.setPen(QPen(QColor('#ffd064' if self.isSelected() else '#a8bbc8'), 2 if self.isSelected() else 1))
        painter.drawRoundedRect(self.boundingRect(), 3, 3)
        peaks = self.view.window.peaks.get(clip['source_id'], [])
        source = project.data['sources'][clip['source_id']]
        if peaks:
            painter.setPen(QColor('#b9e3ea'))
            columns = max(1, min(1200, int(self.length)))
            left = max(0, int(option.exposedRect.left() / max(1, self.length) * columns) - 1)
            right = min(columns, int(option.exposedRect.right() / max(1, self.length) * columns) + 2)
            for x in range(left, right):
                fraction = (clip['source_in'] + x / columns * (clip['source_out'] - clip['source_in'])) / source['frames']
                index = min(len(peaks) - 1, int(fraction * len(peaks)))
                amp = min(1, peaks[index] * clip['gain']) * max(4, self.height * .28)
                px = x * self.length / columns
                center = self.height * .58
                painter.drawLine(int(px), int(center - amp), int(px), int(center + amp))
        painter.setPen(QColor('white'))
        name = clip['metadata'].get('character') or clip['metadata'].get('name') or track['name']
        painter.drawText(QRectF(6, 2, max(1, self.length - 12), 16), Qt.AlignmentFlag.AlignLeft, str(name))
        painter.setPen(QColor('#f3c978'))
        duration = project.clip_frames(clip)
        for fade, reverse in ((clip['fade_in'], False), (clip['fade_out'], True)):
            width = fade / duration * self.length
            if width:
                edge_y = self.height - 2
                painter.drawLine(int(self.length - width if reverse else 0), edge_y if not reverse else 19,
                                 int(self.length if reverse else width), 19 if not reverse else edge_y)

    def hoverMoveEvent(self, event):
        edge = event.pos().x() < 8 or event.pos().x() > self.length - 8
        self.setCursor(Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        project = self.view.window.project
        clip = project.clip(self.clip_id)
        self.view.window.select_clip(self.clip_id)
        if not project.track(clip['track_id'])['locked']:
            mode = 'left' if event.pos().x() < 8 else 'right' if event.pos().x() > self.length - 8 else 'move'
            self.drag = (mode, event.scenePos().x(), copy.deepcopy(clip), self.pos().x(), self.length)

    def mouseMoveEvent(self, event):
        if not self.drag:
            return
        mode, x, clip, old_x, old_length = self.drag
        delta = event.scenePos().x() - x
        if mode == 'move':
            self.setX(max(0, old_x + delta))
        elif mode == 'left':
            self.prepareGeometryChange()
            actual = max(-old_x, min(delta, old_length - 2))
            self.setX(old_x + actual)
            self.length = old_length - actual
        else:
            self.prepareGeometryChange()
            self.length = max(2, old_length + delta)
        self.update()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.drag:
            mode, x, clip, old_x, old_length = self.drag
            self.drag = None
            window = self.view.window
            project = window.project
            source = project.data['sources'][clip['source_id']]
            scale = project.sample_rate / self.view.pixels_per_second
            try:
                if mode == 'move':
                    position = round(self.x() * scale)
                    if window.snap.isChecked():
                        boundaries = [window.cursor_frame]
                        for other in project.data['clips']:
                            if other['id'] != self.clip_id:
                                boundaries.extend((other['position'], other['position'] + project.clip_frames(other)))
                        closest = min(boundaries, key=lambda b: abs(b - position))
                        if abs(closest - position) < project.sample_rate * 10 / self.view.pixels_per_second:
                            position = closest
                    row = window._track_row_at(event.scenePos().y())
                    project.update_clip(self.clip_id, position=position, track_id=project.data['tracks'][row]['id'])
                else:
                    shift = round((self.x() - old_x) * source['sample_rate'] / self.view.pixels_per_second)
                    length = round(self.length * source['sample_rate'] / self.view.pixels_per_second)
                    start = clip['source_in'] + shift if mode == 'left' else clip['source_in']
                    end = clip['source_out'] if mode == 'left' else start + length
                    values = dict(source_in=start, source_out=end, position=round(self.x() * scale))
                    output_length = max(1, round((end - start) * project.sample_rate / source['sample_rate']))
                    values.update(fade_in=min(clip['fade_in'], output_length), fade_out=min(clip['fade_out'], output_length))
                    project.update_clip(self.clip_id, **values)
            except (ValueError, StopIteration) as exc:
                window.status.setText(str(exc))
            window.refresh()


class TimelineView(QGraphicsView):
    viewportResized = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.timeline_scene = QGraphicsScene(self)
        self.setScene(self.timeline_scene)
        self.window = window
        self.pixels_per_second = 90.0
        self.range_anchor = None
        self.setBackgroundBrush(QColor('#17222c'))
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMinimumHeight(250)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewportResized.emit()

    def drawForeground(self, painter, rect):
        project = self.window.project
        painter.setPen(QColor('#778697'))
        step = max(1, round(70 / self.pixels_per_second))
        first = max(0, int(rect.left() / self.pixels_per_second) // step * step)
        for second in range(first, int(rect.right() / self.pixels_per_second) + step, step):
            x = second * self.pixels_per_second
            painter.drawLine(int(x), 0, int(x), 24)
            painter.drawText(int(x + 3), 18, '%ds' % second)
        start, end = self.window.selection_frames
        if end > start:
            painter.fillRect(QRectF(start / project.sample_rate * self.pixels_per_second, 25,
                (end - start) / project.sample_rate * self.pixels_per_second,
                max(84, sum(self.window._track_row_height(row) for row in range(len(project.data['tracks']))))),
                QColor(250, 195, 70, 35))
        x = self.window.cursor_frame / project.sample_rate * self.pixels_per_second
        painter.setPen(QPen(QColor('#ffce64'), 2))
        painter.drawLine(int(x), 0, int(x), int(self.sceneRect().height()))

    def mousePressEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if not isinstance(self.itemAt(event.position().toPoint()), ClipItem):
            frame = max(0, round(point.x() / self.pixels_per_second * self.window.project.sample_rate))
            self.range_anchor = frame
            self.window.seek(frame)
            self.window.selection_frames = (frame, frame)
            self.viewport().update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.range_anchor is not None:
            point = self.mapToScene(event.position().toPoint())
            frame = max(0, round(point.x() / self.pixels_per_second * self.window.project.sample_rate))
            self.window.selection_frames = tuple(sorted((self.range_anchor, frame)))
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.range_anchor = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.window.zoom(1.25 if event.angleDelta().y() > 0 else .8)
            event.accept()
        else:
            super().wheelEvent(event)


class TimelineWindow(QDialog):
    reviewRequested = Signal(object)
    sceneRequested = Signal()
    publishRequested = Signal()

    def __init__(self, project_root, game_audio=None, parent=None):
        super().__init__(parent)
        self.project_root, self.game_audio = Path(project_root), game_audio
        self.project = TimelineProject()
        self.project_path = None
        self.peaks = {}
        self.selected_id = None
        self.clipboard_id = None
        self.cursor_frame = 0
        self.selection_frames = (0, 0)
        self.job = None
        self._pending_close = False
        self._result_handler = None
        self._preview_offset = 0
        self.setWindowTitle('Estúdio da cena — rascunho de áudio')
        self.resize(1200, 760)
        root = QVBoxLayout(self)
        self.scene_label = QLabel('Abra o áudio original da cena ou um rascunho salvo para começar.')
        self.scene_label.setWordWrap(True)
        self.scene_label.setStyleSheet('font-size: 14px; font-weight: bold;')
        root.addWidget(self.scene_label)
        guidance = QLabel('Escolha a cena → selecione ou corte um trecho → ajuste ou gere a fala → aplique ao mod')
        guidance.setWordWrap(True)
        root.addWidget(guidance)
        self.controls = QWidget()
        toolbar = QHBoxLayout(self.controls)
        self.scene_button = QPushButton('Escolher cutscene…')
        self.scene_button.clicked.connect(self.sceneRequested.emit)
        toolbar.addWidget(self.scene_button)
        self.more_button = QToolButton()
        self.more_button.setText('Mais opções ▾')
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.more_menu = QMenu(self.more_button)
        for text, slot in [('Arquivo externo…', self.choose_source), ('Importar voz…', self.choose_import),
                           ('Abrir rascunho…', self.choose_load), ('Salvar rascunho…', self.save_project),
                           ('Nova camada PT', self.new_track)]:
            self.more_menu.addAction(text, slot)
        self.more_button.setMenu(self.more_menu)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(self.more_button)
        root.addWidget(self.controls)
        self.source_bar = QWidget()
        source_controls = QHBoxLayout(self.source_bar)
        source_controls.setContentsMargins(0, 0, 0, 0)
        source_controls.addWidget(QLabel('Fundo da edição:'))
        self.background = QComboBox()
        self.background.addItem('Áudio atual do mod', 'installed')
        self.background.addItem('Áudio original do jogo', 'original')
        self.background.addItem('Silêncio (somente vozes habilitadas)', 'silence')
        self.background.currentIndexChanged.connect(self.set_background)
        source_controls.addWidget(self.background)
        self.select_full_button = QPushButton('Selecionar cena inteira')
        self.select_full_button.clicked.connect(self.select_full_scene)
        source_controls.addWidget(self.select_full_button)
        source_controls.addStretch()
        self.source_bar.hide()
        root.addWidget(self.source_bar)
        self.edit_controls = QWidget()
        edit = QHBoxLayout(self.edit_controls)
        self.edit_buttons = {}
        for text, slot in [('Desfazer', self.undo), ('Refazer', self.redo), ('Dividir no cursor', self.split_selected),
                           ('Remover', self.delete_selected), ('− Zoom', lambda: self.zoom(.8)), ('+ Zoom', lambda: self.zoom(1.25))]:
            button = QPushButton(text)
            button.clicked.connect(slot)
            edit.addWidget(button)
            self.edit_buttons[text] = button
        self.split_button = self.edit_buttons['Dividir no cursor']
        self.delete_button = self.edit_buttons['Remover']
        self.snap = QCheckBox('Encaixar nas bordas')
        self.snap.setToolTip('Ao mover um trecho, aproxima o início do cursor ou das bordas de outros trechos.')
        edit.addWidget(self.snap)
        root.addWidget(self.edit_controls)
        self.content = QSplitter()
        self.layer_scroll = QScrollArea()
        self.layer_scroll.setWidgetResizable(True)
        self.layer_scroll.setMinimumWidth(205)
        self.layer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content.addWidget(self.layer_scroll)
        self.view = TimelineView(self)
        self.view.viewportResized.connect(self._align_layer_viewport)
        self.view.horizontalScrollBar().rangeChanged.connect(self._align_layer_viewport)
        self.view.verticalScrollBar().valueChanged.connect(self.layer_scroll.verticalScrollBar().setValue)
        self.layer_scroll.verticalScrollBar().valueChanged.connect(self.view.verticalScrollBar().setValue)
        self.content.addWidget(self.view)
        inspector = QWidget()
        self.inspector = inspector
        inspector.setMinimumWidth(260)
        form = QFormLayout(inspector)
        self.inspector_form = form
        self.clip_label = QLabel('Selecione um clipe')
        self.clip_label.setWordWrap(True)
        form.addRow(self.clip_label)
        self.fields = {}
        self.seconds_fields = {}
        self._seconds_baseline = {}
        frame_panel = QWidget()
        frames_form = QFormLayout(frame_panel)
        frames_form.setContentsMargins(0, 0, 0, 0)
        for key, label in [('position', 'Posição na cena'), ('source_in', 'Início na fonte'),
                           ('source_out', 'Fim na fonte'), ('fade_in', 'Fade entrada'), ('fade_out', 'Fade saída')]:
            seconds = QDoubleSpinBox()
            seconds.setDecimals(3)
            seconds.setRange(0, 86400)
            seconds.setSingleStep(.01)
            seconds.setSuffix(' s')
            self.seconds_fields[key] = seconds
            form.addRow(label, seconds)
            value = QSpinBox()
            value.setRange(0, 2147483647)
            self.fields[key] = value
            frames_form.addRow(label + ' (frames)', value)
        gain = QDoubleSpinBox()
        gain.setRange(0, 16)
        gain.setSingleStep(.1)
        self.fields['gain'] = gain
        form.addRow('Ganho', gain)
        self.character = QLineEdit()
        self.take = QLineEdit()
        self.reference = QLineEdit()
        self.clip_text = QPlainTextEdit()
        self.clip_text.setMaximumHeight(90)
        form.addRow('Personagem (manual)', self.character)
        form.addRow('Voz / take', self.take)
        form.addRow('Referência original', self.reference)
        form.addRow('Texto gravado', self.clip_text)
        advanced = QCheckBox('Detalhes avançados')
        form.addRow(advanced)
        frame_panel.hide()
        advanced.toggled.connect(frame_panel.setVisible)
        form.addRow(frame_panel)
        self.metadata = QPlainTextEdit()
        self.metadata.setPlaceholderText('Texto, personagem, referência, take e parâmetros (JSON)')
        self.metadata.hide()
        advanced.toggled.connect(self.metadata.setVisible)
        advanced.toggled.connect(lambda visible: form.setRowVisible(self.reference, visible))
        form.setRowVisible(self.reference, False)
        form.addRow(self.metadata)
        self.apply_button = QPushButton('Aplicar ajustes ao trecho')
        self.apply_button.clicked.connect(self.apply_inspector)
        form.addRow(self.apply_button)
        self.review_button = QPushButton('Revisar / gerar fala…')
        self.review_button.setEnabled(False)
        self.review_button.setToolTip('Revisão da fala associada pelo editor principal')
        self.review_button.clicked.connect(self.request_review)
        form.addRow(self.review_button)
        self.content.addWidget(inspector)
        self.content.setSizes([220, 690, 290])
        root.addWidget(self.content, 1)
        transport = QHBoxLayout()
        self.listen_selected_button = QPushButton('Ouvir trecho selecionado')
        self.listen_selected_button.setToolTip('Ouve apenas este trecho, inclusive original, sem alterar a mistura exportada.')
        self.listen_selected_button.clicked.connect(self.play_selected)
        transport.addWidget(self.listen_selected_button)
        self.play_button = QPushButton('Ouvir dublagem')
        self.play_button.clicked.connect(self.play_preview)
        transport.addWidget(self.play_button)
        stop = QPushButton('Parar')
        stop.clicked.connect(lambda: self.player.stop())
        transport.addWidget(stop)
        self.loop = QCheckBox('Repetir seleção')
        transport.addWidget(self.loop)
        self.comparison = QCheckBox('Comparar com original')
        self.comparison.setToolTip('Inclui camadas Original/Instalado na mistura somente se não estiverem silenciadas. Para ouvir o original sozinho, use Ouvir trecho selecionado.')
        transport.addWidget(self.comparison)
        self.export_button = QPushButton('Exportar WAV…')
        self.export_button.clicked.connect(self.choose_render)
        transport.addWidget(self.export_button)
        self.publish_button = QPushButton('Aplicar cena ao mod')
        self.publish_button.setToolTip('Reconstrói o banco do mod com backup; não altera o jogo base. Feche o GTA antes.')
        self.publish_button.clicked.connect(self.publishRequested.emit)
        self.publish_button.setEnabled(False)
        transport.addWidget(self.publish_button)
        self.time_label = QLabel('0.000 s')
        transport.addWidget(self.time_label)
        root.addLayout(transport)
        self.progress = QProgressBar()
        self.progress.hide()
        root.addWidget(self.progress)
        self.cancel = QPushButton('Cancelar operação')
        self.cancel.clicked.connect(self.cancel_job)
        self.cancel.hide()
        root.addWidget(self.cancel)
        self.status = QLabel('Escolha uma cutscene para carregar o áudio completo. Arraste as bordas para aparar; selecione um intervalo para usar a voz original na geração.')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.player.positionChanged.connect(self._play_position)
        self.player.errorOccurred.connect(lambda error, message: self.status.setText('Reprodução: ' + message))
        self.player.mediaStatusChanged.connect(self._media_status)
        for key, slot in [('Ctrl+Z', self.undo), ('Ctrl+Shift+Z', self.redo), ('Delete', self.delete_selected),
                          ('Ctrl+C', self.copy_selected), ('Ctrl+V', self.paste_clip), ('S', self.split_selected)]:
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        self.refresh()

    def _busy(self, active):
        for widget in (self.controls, self.edit_controls, self.content, self.source_bar, self.play_button, self.listen_selected_button, self.export_button, self.publish_button, self.comparison):
            widget.setEnabled(not active)
        self.progress.setVisible(active)
        self.cancel.setVisible(active)
        if not active:
            self._update_actions()

    def _start(self, operation, handler):
        if self.job is not None:
            return
        self.player.stop()
        self._busy(True)
        self.progress.setRange(0, 0)
        self._result_handler = handler
        self.job = _Job(operation, self)
        self.job.progress.connect(self._progress)
        self.job.completed.connect(self._completed)
        self.job.failed.connect(self.status.setText)
        self.job.finished.connect(self._finished)
        self.job.start()

    def _progress(self, done, total, message):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(message)

    def _completed(self, result):
        if not self._pending_close:
            try:
                self._result_handler(result)
            except Exception as exc:
                self.status.setText(str(exc))

    def _finished(self):
        previous = self.job
        self.job = None
        # A closed dialog can lose its last Python reference before Qt delivers
        # DeferredDelete. Detach the finished thread to avoid parent/child
        # wrapper destruction racing that deferred deletion.
        previous.setParent(None)
        previous.deleteLater()
        self._busy(False)
        if self._pending_close:
            self._pending_close = False
            self.close()

    def cancel_job(self):
        if self.job:
            self.job.cancel_event.set()
            self.status.setText('Cancelando…')

    def open_source(self, path):
        self.import_wav(path, role='original', position=0)

    def open_record(self, record, original_path, text):
        metadata = {'table': record.get('table'), 'gxt_hash': record.get('hash'), 'name': record.get('name'),
                    'text': text, 'record_info': record.get('info', {})}
        self.import_wav(original_path, role='original', position=0, metadata=metadata)

    def import_wav(self, path, role='pt', position=None, metadata=None, track_id=None):
        frozen = copy.deepcopy(self.project)
        position = self.cursor_frame if position is None else position
        supplied_metadata = metadata is not None
        metadata = copy.deepcopy(metadata or {})
        character = str(metadata.get('character') or '').strip()
        layer_name = {'original': 'Original da cena', 'installed': 'Resultado instalado',
                      'pt': character or 'Vozes PT-BR', 'ambience': 'Ambiência / efeitos'}.get(role, role)
        # An explicit target or manual import may reuse the selected layer.
        # Character-bearing takes group by their recorded association instead
        # of following the selection left behind by a previous import.
        target_track = track_id
        if target_track is None and not supplied_metadata and self.selected_id:
            selected = next((c for c in frozen.data['clips'] if c['id'] == self.selected_id), None)
            if selected:
                candidate = frozen.track(selected['track_id'])
                if candidate['role'] == role and not candidate['locked']:
                    target_track = candidate['id']
        if target_track is None and role in ('pt', 'ambience', 'installed'):
            for candidate in frozen.data['tracks']:
                if candidate['role'] != role or candidate['locked'] or candidate['mute']:
                    continue
                characters = {str(c['metadata'].get('character') or '').strip().casefold()
                    for c in frozen.data['clips'] if c['track_id'] == candidate['id']
                    and str(c['metadata'].get('character') or '').strip()}
                if character:
                    matches = characters == {character.casefold()} or (not characters and candidate['name'].casefold() == character.casefold())
                else:
                    matches = not characters and (candidate['name'] == layer_name or
                        (role == 'pt' and candidate['name'].startswith('Vozes PT-BR ·')))
                if matches:
                    target_track = candidate['id']
                    break
        def operation(cancel, progress):
            progress(0, 0, 'Lendo fonte e forma de onda…')
            actual = Path(path)
            if actual.suffix.lower() != '.wav':
                from studio.audio.cutscene_studio import _probe, _decode_raw, _write_wav
                from studio.audio.timeline_project import digest
                meta = _probe(actual, cancel)
                cache = self.project_root / 'cache' / 'qt_timeline_sources'
                cache.mkdir(parents=True, exist_ok=True)
                actual = cache / (digest(actual) + '.wav')
                if not actual.is_file():
                    pcm = _decode_raw(path, meta['sample_rate'], meta['channels'], cancel, progress)
                    _write_wav(actual, pcm, meta['channels'], meta['sample_rate'])
            if cancel.is_set():
                raise InterruptedError('Importação cancelada.')
            clip_id = frozen.add_wav(actual, track_id=target_track, position=position, role=role,
                                     metadata=metadata, native_format=True)
            if role == 'pt' and metadata.get('table') and metadata.get('gxt_hash') is not None:
                for old in frozen.data['clips']:
                    detail = old.get('metadata', {})
                    if (old['id'] != clip_id and frozen.track(old['track_id'])['role'] == 'pt'
                            and detail.get('table') == metadata['table'] and detail.get('gxt_hash') == metadata['gxt_hash']):
                        old['gain'] = 0  # preserved as an alternative, with one-step undo
            if target_track is None:
                # Naming belongs to this same import command: its existing
                # undo snapshot already precedes creation of the new layer.
                frozen.track(frozen.clip(clip_id)['track_id'])['name'] = layer_name
                frozen.validate()
            return frozen, _peaks(frozen, cancel), clip_id
        def done(result):
            self.project, self.peaks, self.selected_id = result
            self.refresh()
            self.status.setText('Fonte importada. Corte e movimento preservam o WAV; original não entra automaticamente na mistura PT.')
        self._start(operation, done)

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Original da cena', '', 'Áudio (*.wav *.ogg *.mp3 *.flac)')
        if path:
            self.open_source(path)

    def choose_import(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Importar voz PT', '', 'WAV PCM (*.wav)')
        if path:
            self.import_wav(path)

    def choose_load(self):
        if not self._allow_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Abrir projeto', str(self.project_root), 'Projeto (*.timeline.json *.json)')
        if path:
            self.load_project(path)

    def load_project(self, path):
        def operation(cancel, progress):
            progress(0, 0, 'Verificando projeto e fontes…')
            project = TimelineProject.load(path)
            return project, _peaks(project, cancel)
        def done(result):
            self.project, self.peaks = result
            self.project_path = Path(path)
            self.selected_id = self.project.data.get('scene', {}).get('background_clip_id')
            self.clipboard_id = None
            self.cursor_frame = 0
            self.selection_frames = (0, 0)
            self.refresh()
            self.status.setText('Rascunho reaberto; fontes verificadas por SHA-256.')
        self._start(operation, done)

    def save_project(self):
        path = str(self.project_path or self.project_root / 'scene.timeline.json')
        path, _ = QFileDialog.getSaveFileName(self, 'Salvar rascunho', path, 'Projeto (*.timeline.json)')
        if path:
            try:
                self.project.save(path)
                self.project_path = Path(path)
                self.setWindowTitle('Estúdio da cena — rascunho salvo')
                self.status.setText('Rascunho salvo. Áudio ainda não foi instalado.')
            except Exception as exc:
                self.status.setText(str(exc))

    def render_to(self, path, preview=False, selected=False):
        self.player.setSource(QUrl())
        from .scene_mix import render_project
        frozen = copy.deepcopy(self.project) if selected else render_project(self.project)
        include_comparison = self.comparison.isChecked()
        offset = 0
        if selected:
            if not self.selected_id:
                self.status.setText('Selecione um trecho na forma de onda para ouvir.')
                return
            clip = frozen.clip(self.selected_id)
            offset = clip['position']
            clip['position'] = 0
            frozen.data['clips'] = [clip]
            for track in frozen.data['tracks']:
                track['mute'] = track['id'] != clip['track_id']
                track['solo'] = False
            include_comparison = True
        def operation(cancel, progress):
            progress(0, 0, 'Preparando mistura PCM…')
            return frozen.render(path, include_comparison, cancel, progress)
        def done(result):
            self.status.setText('Renderizado: %d clipes audíveis; %d amostras saturadas. Não instalado.' % (result['mixed_clips'], result['clipped_samples']))
            if not result['mixed_clips']:
                self.status.setText('Mistura silenciosa: nenhuma camada PT/ambiência audível. Original/instalado exigem comparação e desmutar.')
            if preview:
                self._preview_offset = offset
                self.player.setSource(QUrl.fromLocalFile(result['path']))
                start = 0 if selected else self.selection_frames[0] if self.loop.isChecked() and self.selection_frames[1] > self.selection_frames[0] else self.cursor_frame
                self.player.setPosition(round(start * 1000 / self.project.sample_rate))
                self.player.play()
        self._start(operation, done)

    def choose_render(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Renderizar mistura', str(self.project_root / 'scene-preview.wav'), 'WAV (*.wav)')
        if path:
            self.render_to(path)

    def play_preview(self):
        # Preview uses the same renderer, fades and layer policy as export.
        self.player.setSource(QUrl())
        self.render_to(self.project_root / 'cache' / 'qt_timeline_preview.wav', preview=True)

    def play_selected(self):
        self.render_to(self.project_root / 'cache' / 'qt_timeline_selected.wav', preview=True, selected=True)

    def _play_position(self, milliseconds):
        self.cursor_frame = self._preview_offset + round(milliseconds * self.project.sample_rate / 1000)
        self.time_label.setText('%.3f s' % (self.cursor_frame / self.project.sample_rate))
        start, end = self.selection_frames
        if self.loop.isChecked() and end > start and self.cursor_frame >= end:
            self.player.setPosition(round(max(0, start - self._preview_offset) * 1000 / self.project.sample_rate))
        self._update_actions()
        self.view.viewport().update()

    def _media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.loop.isChecked():
            start, end = self.selection_frames
            if end > start:
                self.player.setPosition(round(max(0, start - self._preview_offset) * 1000 / self.project.sample_rate))
                self.player.play()

    def seek(self, frame):
        self.cursor_frame = max(0, frame)
        self.player.setPosition(round(max(0, self.cursor_frame - self._preview_offset) * 1000 / self.project.sample_rate))
        self.time_label.setText('%.3f s' % (self.cursor_frame / self.project.sample_rate))
        self.view.viewport().update()
        self._update_actions()

    def zoom(self, factor):
        self.view.pixels_per_second = min(1600, max(8, self.view.pixels_per_second * factor))
        self.refresh()

    def new_track(self):
        self.project.add_track('Voz PT %d' % (len(self.project.data['tracks']) + 1))
        self.refresh()

    def select_clip(self, clip_id):
        self.selected_id = clip_id
        clip = self.project.clip(clip_id)
        source = self.project.data['sources'][clip['source_id']]
        track = self.project.track(clip['track_id'])
        role = 'Áudio completo da cena' if clip['metadata'].get('scene_background') else {'original': 'Referência original', 'installed': 'Áudio instalado', 'pt': 'Dublagem PT-BR', 'ambience': 'Ambiência / efeitos'}[track['role']]
        duration = self.project.clip_frames(clip) / self.project.sample_rate
        self.clip_label.setText('%s\n%s · %.3f s%s' % (clip['metadata'].get('name') or clip['metadata'].get('character') or track['name'], role, duration, '\nProtegido contra edição' if track['locked'] else ''))
        self.clip_label.setToolTip('%s\n%d Hz · %d amostras\n%s' % (source['path'], source['sample_rate'], source['frames'], clip['source_id']))
        for key, widget in self.fields.items():
            widget.setValue(clip[key])
        for key, widget in self.seconds_fields.items():
            rate = source['sample_rate'] if key in ('source_in', 'source_out') else self.project.sample_rate
            widget.setValue(clip[key] / rate)
            self._seconds_baseline[key] = widget.value()
        self.metadata.setPlainText(json.dumps(clip['metadata'], ensure_ascii=False, indent=2))
        self.character.setText(str(clip['metadata'].get('character') or ''))
        self.take.setText(str(clip['metadata'].get('take') or ''))
        self.reference.setText(str(clip['metadata'].get('reference') or ''))
        self.clip_text.setPlainText(str(clip['metadata'].get('text') or ''))
        self._update_actions()

    def _update_actions(self):
        clip = next((c for c in self.project.data['clips'] if c['id'] == self.selected_id), None)
        editable = bool(clip and not self.project.track(clip['track_id'])['locked'])
        ready = self.job is None
        self.listen_selected_button.setEnabled(bool(clip) and ready)
        self.play_button.setEnabled(bool(self.project.data['clips']) and ready)
        self.export_button.setEnabled(bool(self.project.data['clips']) and ready)
        self.apply_button.setEnabled(editable and ready)
        self.delete_button.setEnabled(editable and ready)
        self.split_button.setEnabled(bool(editable and clip['position'] < self.cursor_frame < clip['position'] + self.project.clip_frames(clip)) and ready)
        for widget in [*self.fields.values(), *self.seconds_fields.values(), self.character, self.take, self.reference, self.clip_text, self.metadata]:
            widget.setEnabled(editable and ready)
        self.edit_buttons['Desfazer'].setEnabled(bool(self.project._undo) and ready)
        self.edit_buttons['Refazer'].setEnabled(bool(self.project._redo) and ready)

    def apply_inspector(self):
        if self.selected_id:
            try:
                values = {key: widget.value() for key, widget in self.fields.items()}
                clip = self.project.clip(self.selected_id)
                source = self.project.data['sources'][clip['source_id']]
                for key, widget in self.seconds_fields.items():
                    rate = source['sample_rate'] if key in ('source_in', 'source_out') else self.project.sample_rate
                    # Merely opening/applying the inspector cannot quantize an
                    # exact frame boundary to the displayed millisecond grid.
                    if widget.value() != self._seconds_baseline.get(key, widget.value()):
                        values[key] = round(widget.value() * rate)
                values['metadata'] = json.loads(self.metadata.toPlainText() or '{}')
                for key, value in [('character', self.character.text()), ('take', self.take.text()),
                                   ('reference', self.reference.text()), ('text', self.clip_text.toPlainText())]:
                    if value or key in values['metadata']:
                        values['metadata'][key] = value
                self.project.update_clip(self.selected_id, **values)
                self.refresh()
            except Exception as exc:
                self.status.setText(str(exc))

    def request_review(self):
        if not self.selected_id:
            self.status.setText('Selecione o clipe da fala para revisar.')
            return
        clip = self.project.clip(self.selected_id)
        source = self.project.data['sources'][clip['source_id']]
        metadata = copy.deepcopy(clip['metadata'])
        metadata.update(timeline_clip_id=clip['id'], position_frames=clip['position'],
                        source_path=source['path'], source_in=clip['source_in'],
                        source_out=clip['source_out'], source_sample_rate=source['sample_rate'])
        self.reviewRequested.emit(metadata)

    def _edit(self, action):
        if self.job:
            return
        try:
            action()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def undo(self):
        self._edit(self.project.undo)

    def redo(self):
        self._edit(self.project.redo)

    def split_selected(self):
        if self.selected_id:
            self._edit(lambda: self.project.split(self.selected_id, self.cursor_frame))

    def delete_selected(self):
        if self.selected_id:
            self._edit(lambda: self.project.delete(self.selected_id))

    def copy_selected(self):
        self.clipboard_id = self.selected_id

    def paste_clip(self):
        if self.clipboard_id:
            self._edit(lambda: self.project.duplicate(self.clipboard_id, self.cursor_frame))

    def _track_row_height(self, row):
        track = self.project.data['tracks'][row]
        if track['name'] in {'Referências originais por fala', 'Janelas de substituição'}:
            return 52
        return 84

    def _track_row_top(self, row):
        return 32 + sum(self._track_row_height(index) for index in range(row))

    def _track_row_at(self, y):
        offset = max(0, y - 32)
        for row in range(len(self.project.data['tracks'])):
            height = self._track_row_height(row)
            if offset < height:
                return row
            offset -= height
        return max(0, len(self.project.data['tracks']) - 1)

    def refresh(self):
        self.setWindowTitle('Estúdio da cena — ' + ('alterações não salvas' if self.project.dirty else 'rascunho salvo'))
        scroll_y = self.view.verticalScrollBar().value()
        scroll_x = self.view.horizontalScrollBar().value()
        self.view.scene().clear()
        tracks = self.project.data['tracks']
        widget = QWidget()
        layers = QVBoxLayout(widget)
        layers.setContentsMargins(4, 32, 4, 4)
        layers.setSpacing(4)
        for row, track in enumerate(tracks):
            block = QWidget()
            layout = QVBoxLayout(block)
            layout.setContentsMargins(0, 0, 0, 0)
            row_height = self._track_row_height(row)
            compact_guide = row_height < 84
            if compact_guide:
                layout.setContentsMargins(2, 2, 2, 2)
                layout.setSpacing(1)
                name = QLabel(track['name'])
                name.setFixedHeight(20)
                name.setToolTip(track['name'])
                name.setStyleSheet('font-weight: 600;')
                layout.addWidget(name)
                legend = QLabel('Guia de referência · somente leitura' if track['name'] == 'Referências originais por fala'
                                else 'Janela de destino · somente leitura')
                legend.setFixedHeight(16)
                legend.setStyleSheet('color: #758291; font-size: 10px;')
                layout.addWidget(legend)
            else:
                name = QLineEdit(track['name'])
                name.setToolTip(track['name'])
                name.editingFinished.connect(lambda tid=track['id'], field=name: self._track_change(tid, 'name', field.text()))
                layout.addWidget(name)
                controls = QHBoxLayout()
                role = QComboBox()
                role.addItem('PT', 'pt')
                is_background = any(c['track_id'] == track['id'] and c.get('metadata', {}).get('scene_background') for c in self.project.data['clips'])
                role.addItem('Base da cena' if is_background else 'Ambiência', 'ambience')
                role.addItem('Instalado', 'installed')
                role.addItem('Original', 'original')
                role.setCurrentIndex(role.findData(track['role']))
                role.setEnabled(track['role'] != 'original')
                role.currentIndexChanged.connect(lambda index, tid=track['id'], field=role: self._track_change(tid, 'role', field.itemData(index)))
                layout.addWidget(role)
                for key, title in [('mute', 'Mudo'), ('solo', 'Só'), ('locked', 'Travar')]:
                    box = QCheckBox(title)
                    box.setToolTip({'mute': 'Silenciar camada', 'solo': 'Ouvir somente camadas em solo', 'locked': 'Bloquear edição de clipes'}[key])
                    box.setChecked(track[key])
                    box.toggled.connect(lambda value, tid=track['id'], k=key: self._track_change(tid, k, value))
                    controls.addWidget(box)
                gain = QDoubleSpinBox()
                gain.setRange(0, 16)
                gain.setMaximumWidth(65)
                gain.setSingleStep(.1)
                gain.setValue(track['gain'])
                gain.setToolTip('Ganho da camada')
                gain.editingFinished.connect(lambda tid=track['id'], g=gain: self._track_change(tid, 'gain', g.value()))
                controls.addWidget(gain)
                layout.addLayout(controls)
            block.setFixedHeight(row_height - 4)
            layers.addWidget(block)
            for clip in self.project.data['clips']:
                if clip['track_id'] == track['id']:
                    item = ClipItem(self.view, clip, row)
                    self.view.scene().addItem(item)
                    item.setSelected(clip['id'] == self.selected_id)
        layers.addStretch()
        scene_height = max(250, sum(self._track_row_height(row) for row in range(len(tracks))) + 40)
        widget.setMinimumHeight(scene_height)
        old = self.layer_scroll.takeWidget()
        self.layer_scroll.setWidget(widget)
        if old:
            old.deleteLater()
        length = max(10, self.project.duration_frames / self.project.sample_rate) * self.view.pixels_per_second
        self.view.setSceneRect(0, 0, length + 100, scene_height)
        self.view.verticalScrollBar().setValue(scroll_y)
        self.layer_scroll.verticalScrollBar().setValue(scroll_y)
        self.view.horizontalScrollBar().setValue(scroll_x)
        if self.selected_id and any(c['id'] == self.selected_id for c in self.project.data['clips']):
            self.select_clip(self.selected_id)
        else:
            self.selected_id = None
            self.clip_label.setText('Selecione um trecho na forma de onda para ouvir ou editar.')
            for widget in (self.character, self.take, self.reference, self.clip_text, self.metadata):
                widget.clear()
        originals = [c for c in self.project.data['clips'] if self.project.track(c['track_id'])['role'] == 'original']
        if originals:
            source = self.project.data['sources'][originals[0]['source_id']]
            scene = self.project.data.get('scene', {})
            name = (scene.get('container', '') + '/' + scene['track']) if scene.get('track') else Path(source['path']).name
            pending = len(scene.get('unavailable_windows', []))
            self.scene_label.setText('Cena: %s · %d trecho(s)%s' % (name, len(self.project.data['clips']),
                ' · %d legenda(s) com intervalo a conferir' % pending if pending else ''))
        elif self.project.data['clips']:
            self.scene_label.setText('Rascunho de dublagem · %d trecho(s) · Abra o áudio original para comparar.' % len(self.project.data['clips']))
        else:
            self.scene_label.setText('Abra o áudio original da cena ou um rascunho salvo para começar.')
        self._update_actions()
        scene = self.project.data.get('scene', {})
        self.source_bar.setVisible(bool(scene))
        self.background.blockSignals(True)
        self.background.setCurrentIndex(max(0, self.background.findData(scene.get('background_mode', 'silence'))))
        self.background.blockSignals(False)
        self.publish_button.setEnabled(bool(scene.get('installed_archive_sha256')) and not self.job)
        self.view.viewport().update()

    def select_full_scene(self):
        scene = self.project.data.get('scene', {})
        clip = next((c for c in self.project.data['clips'] if c.get('metadata', {}).get('scene_background')
            and (self.project.track(c['track_id'])['role'] == 'ambience')
            and not self.project.track(c['track_id'])['mute']), None)
        if clip is None:
            clip = next((c for c in self.project.data['clips'] if c.get('metadata', {}).get('scene_background')), None)
        if clip:
            self.select_clip(clip['id'])
            self.selection_frames = (0, self.project.duration_frames)
            self.cursor_frame = 0
            self.view.viewport().update()
            self.status.setText('Cena inteira selecionada. Para substituir só uma fala, marque um intervalo menor.')

    def set_background(self):
        scene = self.project.data.get('scene')
        if not scene or self.job:
            return
        mode = self.background.currentData()
        def change():
            for clip in self.project.data['clips']:
                if not clip.get('metadata', {}).get('scene_background'):
                    continue
                track = self.project.track(clip['track_id'])
                installed = clip['id'] == scene.get('background_clip_id')
                track.update(mute=mode == 'silence' or (installed != (mode == 'installed')),
                             role='ambience', locked=False)
            scene['background_mode'] = mode
            self.project.validate()
        self._edit(lambda: self.project._change(change))

    def _track_change(self, track_id, key, value):
        self._edit(lambda: self.project.update_track(track_id, **{key: value}))

    def _align_layer_viewport(self, *args):
        # The waveform's horizontal scrollbar consumes height. Reserve the
        # same space beneath layer controls so both vertical ranges agree,
        # including at the final row where feedback used to clamp scrolling.
        margin = self.layer_scroll.viewportMargins().bottom()
        difference = self.layer_scroll.viewport().height() - self.view.viewport().height()
        target = max(0, margin + difference)
        if target != margin:
            self.layer_scroll.setViewportMargins(0, 0, 0, target)

    def _allow_discard(self):
        if self.project.dirty:
            return QMessageBox.question(self, 'Rascunho alterado', 'Descartar alterações não salvas?',
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard
        return True

    def closeEvent(self, event):
        if self.job:
            self._pending_close = True
            self.cancel_job()
            event.ignore()
            return
        if not self._allow_discard():
            event.ignore()
            return
        self.player.stop()
        super().closeEvent(event)
