"""Qt batch planning and explicit generation for native CUTSCENE tracks."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QThread, QUrl, Qt, Signal, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QProgressBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout)

from studio.audio import cutscene_batch as batch
from studio.audio import cutscene_studio as audio
from studio.audio.cutscene import cache_root as cutscene_cache_root
from studio.audio.cutscene import extract as extract_cutscene
from studio.gxt.model import Gxt
from studio.script.native_index import load_current


_OUTCOMES = {'OK': 'Substituída', 'WARN': 'Substituída (conferir)',
             'KEPT_ORIGINAL': 'Mantida original', 'MISSING_PTBR': 'Sem take PT-BR (silêncio)'}
_DURATION_LABELS = {'Seguir o tempo da legenda': 'janela', 'Duração livre': 'livre'}


def _snapshot_target(context):
    """Capture the editor's current GXT on the UI thread before background work."""
    document = getattr(context, 'document', None)
    target = getattr(document, 'gxt', None)
    return copy.deepcopy(target) if target is not None else None


class _BatchWorker(QThread):
    progress = Signal(int, int, str)
    status = Signal(str)
    succeeded = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, owner, kind, operation, args=(), parent=None):
        super().__init__(parent)
        self.owner = owner
        self.kind = kind
        self.operation = operation
        self.args = args
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            value = self.operation(*self.args, cancel_event=self.cancel_event,
                                   progress=self.progress.emit, status=self.status.emit)
            self.succeeded.emit(self.kind, value)
        except (batch.CancelledError, InterruptedError) as exc:
            self.succeeded.emit(self.kind, {'cancelled': True, 'message': str(exc)})
        except Exception as exc:
            self.failed.emit(self.kind, str(exc) or exc.__class__.__name__)


class CutsceneBatchDialog(QDialog):
    """Plan native .cut subtitle windows and run selected batches on request."""
    sceneRequested = Signal(object)

    def __init__(self, context, parent=None, autoload=True, tracks_spec=None):
        super().__init__(parent)
        self.context = context
        self.project = Path(context.project_root).resolve()
        game_audio = getattr(context, 'game_audio', None)
        self.game = Path(game_audio.game_dir).resolve() if game_audio and game_audio.game_dir else None
        self.report = None
        self.tracks = {}
        self.plans = {}
        self.results = {}
        self.errors = {}
        self.worker = None
        self._busy = False
        self._close_after_worker = False
        self._current_snapshot = None
        self._current_paths = ('', '')
        self._tracks_spec = tracks_spec
        self.setWindowTitle('Lote automático de cutscenes')
        self.resize(1180, 780)
        self._build()
        if autoload:
            self.autoload()

    def _build(self):
        root = QVBoxLayout(self)
        intro = QLabel('As janelas vêm das legendas nativas (.cut); o texto PT vem do GXT aberto. Planejar não gera áudio. ' 
                       'A geração começa somente após você escolher faixas e confirmar. Nenhum arquivo do jogo ou GXT é alterado.')
        intro.setWordWrap(True)
        root.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel('Duração:'))
        self.duration_mode = QComboBox()
        self.duration_mode.addItems(_DURATION_LABELS)
        controls.addWidget(self.duration_mode)
        controls.addWidget(QLabel('Velocidade:'))
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.5, 2.0)
        self.speed.setSingleStep(.05)
        self.speed.setDecimals(2)
        self.speed.setValue(1.0)
        self.speed.setSuffix('×')
        controls.addWidget(self.speed)
        controls.addWidget(QLabel('Fundo:'))
        self.background = QComboBox()
        for label, value in audio.BACKGROUND_MODES.items():
            self.background.addItem(label, value)
        self.background.setCurrentIndex(max(0, self.background.findData('voice_only')))
        self.background.setToolTip('Preservar música/efeitos originais exige seleção explícita.')
        controls.addWidget(self.background, 1)
        self.refresh_button = QPushButton('Carregar / atualizar plano')
        self.refresh_button.clicked.connect(self.autoload)
        controls.addWidget(self.refresh_button)
        root.addLayout(controls)

        self.track_table = QTableWidget(0, 7)
        self.track_table.setHorizontalHeaderLabels(['Faixa', 'Cena', 'Falas', 'Geráveis', 'Avisos', 'Excluídas', 'Estado'])
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.itemSelectionChanged.connect(self.show_details)
        root.addWidget(self.track_table, 3)

        self.exception_table = QTableWidget(0, 6)
        self.exception_table.setHorizontalHeaderLabels(['Chave', 'Início', 'Fim', 'Resultado', 'Motivo', 'O que fazer'])
        self.exception_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.exception_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.exception_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.exception_table, 2)
        self.details = QLabel('Selecione uma faixa para ver o plano ou suas exceções.')
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.details)

        actions = QHBoxLayout()
        self.run_selected_button = QPushButton('Gerar faixas selecionadas')
        self.run_selected_button.clicked.connect(self.run_selected)
        self.run_all_button = QPushButton('Gerar todas com falas geráveis')
        self.run_all_button.clicked.connect(self.run_all)
        self.cancel_button = QPushButton('Cancelar')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.open_scene_button = QPushButton('Abrir faixa no estúdio manual')
        self.open_scene_button.clicked.connect(self.open_scene)
        self.open_folder_button = QPushButton('Pasta dos resultados')
        self.open_folder_button.clicked.connect(self.open_folder)
        self.csv_button = QPushButton('Resumo CSV')
        self.csv_button.clicked.connect(self.export_csv)
        for button in (self.run_selected_button, self.run_all_button, self.cancel_button,
                       self.open_scene_button, self.open_folder_button, self.csv_button):
            actions.addWidget(button)
        root.addLayout(actions)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress_row.addWidget(self.progress, 1)
        self.status = QLabel('Pronto para carregar o índice nativo.' if not self.game else 'Carregando índice nativo…')
        self.status.setWordWrap(True)
        progress_row.addWidget(self.status, 2)
        root.addLayout(progress_row)

    def _policy(self):
        return batch.Policy(duration_mode=_DURATION_LABELS[self.duration_mode.currentText()],
                            speed=float(self.speed.value()), background_mode=self.background.currentData())

    def _set_busy(self, worker, busy=True):
        if worker is not None:
            self.worker = worker
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self.run_selected_button.setEnabled(not busy)
        self.run_all_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    def _start_worker(self, kind, operation, *args):
        if self._busy:
            return False
        worker = _BatchWorker(self, kind, operation, args, self)
        worker.progress.connect(self._progress)
        worker.status.connect(self.status.setText)
        worker.succeeded.connect(self._succeeded)
        worker.failed.connect(self._failed)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        self._set_busy(worker)
        worker.start()
        return True

    def autoload(self):
        if self._busy:
            return
        if not self.game or not self.game.is_dir():
            self.status.setText('Defina uma pasta válida do jogo em Opções → Configurar jogo e mod.')
            return
        self._current_snapshot = _snapshot_target(self.context)
        self._worker_policy = self._policy()
        document = getattr(self.context, 'document', None)
        target = getattr(document, 'gxt', None)
        comparison = getattr(document, 'comparison', None)
        self._current_paths = (str(getattr(target, 'path', '') or ''), str(getattr(comparison, 'path', '') or ''))
        self.plans = {}
        self.track_table.setRowCount(0)
        self.exception_table.setRowCount(0)
        self.status.setText('Carregando índice nativo e verificando as faixas originais…')
        self._start_worker('catalog', self._catalog_sync, self._current_snapshot, self._current_paths,
                           self._worker_policy)

    def _catalog_sync(self, target_snapshot, gxt_paths, policy, cancel_event, progress, status):
        cancel_event_check = cancel_event.is_set
        if cancel_event_check():
            return {'cancelled': True}
        report = load_current(self.project, self.game)
        if not report:
            raise ValueError('Índice nativo atual indisponível. Atualize-o em Análise nativa → SCM e cutscenes.')
        stream = (report.get('sources') or {}).get('stream') or {}
        archive_sha = stream.get('sha256')
        cache_manifest = cutscene_cache_root(self.project) / str(archive_sha)[:20] / 'source.json'
        if archive_sha and not cache_manifest.is_file():
            status('Extraindo as 141 faixas originais CUTSCENE para o cache do projeto. Esta extração não interrompe no meio; cancelar será aplicado ao fim.')
            if cancel_event_check():
                return {'cancelled': True}
            game_audio = getattr(self.context, 'game_audio', None)
            executable = game_audio.saat().get('stream') if game_audio else None
            if not executable:
                raise ValueError('Configure o executável SAAT de cutscenes antes de extrair o original.')
            extract_cutscene(self.game, self.project, executable)
            if cancel_event_check():
                return {'cancelled': True, 'message': 'Extração concluída; cancelada antes da indexação.'}
        tracks = audio.list_tracks(self.project, report, cancel_event,
            lambda done, total, message: progress(done, total or 0, message))
        if cancel_event_check():
            return {'cancelled': True}
        return self._plan_sync(report, tracks, target_snapshot, gxt_paths, policy, cancel_event, progress, status)

    def _plan_sync(self, report, tracks, target_snapshot, gxt_paths, policy, cancel_event, progress, status):
        if cancel_event.is_set():
            return {'cancelled': True}
        native_info = (report.get('sources') or {}).get('gxt') or {}
        native = None
        try:
            if native_info.get('path') and audio.digest(native_info['path']) == native_info.get('sha256'):
                native = Gxt(native_info['path'])
        except (OSError, ValueError):
            native = None
        lookup = batch.make_text_lookup(native, target_snapshot)
        plans = batch.plan_all(report, lookup, None, policy, gxt_paths)
        track_names = {row['track'] for row in tracks}
        return {'report': report, 'tracks': {row['track']: row for row in tracks},
                'plans': {name: items for name, items in plans.items() if name in track_names},
                'policy': policy, 'target_available': target_snapshot is not None}

    def _succeeded(self, kind, value):
        if kind == 'catalog':
            if value.get('cancelled'):
                self.status.setText(value.get('message') or 'Carregamento cancelado.')
                self._finish_task()
                return
            self.report = value['report']
            self.tracks = value['tracks']
            self.plans = value['plans']
            self._render_plans(value.get('policy'))
            if self._tracks_spec:
                self.select_tracks_spec(self._tracks_spec)
            gxt_note = '' if value.get('target_available') else ' Abra um GXT do mod para incluir traduções no plano.'
            self.status.setText('%d faixas com legendas · %d falas geráveis.%s' % (
                len(self.plans), sum(batch.summarize(items)['generate'] for items in self.plans.values()), gxt_note))
            self._finish_task()
        elif kind == 'run':
            if value.get('cancelled'):
                self.status.setText('Lote cancelado. Resultados parciais foram preservados no cache e incluídos no resumo quando disponíveis.')
            else:
                self.status.setText('Lote concluído. Resumo: %s' % (value.get('csv') or 'não disponível'))
            self.results.update(value.get('results') or {})
            self.errors.update(value.get('errors') or {})
            self._render_plans()
            self.show_details()
            self._finish_task()

    def _failed(self, kind, message):
        self.errors[kind] = message
        self.status.setText(message)
        self._finish_task()

    def _finish_task(self):
        self._set_busy(self.worker, False)

    def _worker_finished(self, worker):
        if self.worker is worker and not self._busy:
            self.worker = None
        if self._close_after_worker and not self._busy:
            QTimer.singleShot(0, self.close)

    def _progress(self, done, total, message):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
        self.status.setText(message)

    def _render_plans(self, policy=None):
        self.track_table.setRowCount(len(self.plans))
        for row_index, (name, items) in enumerate(sorted(self.plans.items())):
            summary = batch.summarize(items)
            scenes = ', '.join(sorted({str(item.get('scene')) for item in items if item.get('scene')}))
            result = self.results.get(name)
            state = self._state_text(result) if result else 'Plano pronto'
            values = (name, scenes, summary['total'], summary['generate'], summary['WARN'], summary['SKIP'], state)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, name)
                self.track_table.setItem(row_index, column, item)
        self.show_details()

    @staticmethod
    def _state_text(result):
        counts = {key: 0 for key in ('OK', 'WARN', 'KEPT_ORIGINAL', 'MISSING_PTBR')}
        for item in result.get('items', []):
            if item.get('outcome') in counts:
                counts[item['outcome']] += 1
        return '%d substituídas · %d conferir · %d sem PT-BR%s' % (
            counts['OK'] + counts['WARN'], counts['WARN'], counts['MISSING_PTBR'],
            ' · cancelada' if result.get('cancelled') else '')

    def selected_tracks(self):
        names = []
        for index in self.track_table.selectionModel().selectedRows():
            item = self.track_table.item(index.row(), 0)
            if item:
                names.append(str(item.data(Qt.ItemDataRole.UserRole) or item.text()))
        return names

    def select_tracks_spec(self, tracks_spec):
        """Select exact planned track names supplied by a scene browser."""
        if isinstance(tracks_spec, str):
            names = [part.strip() for part in tracks_spec.replace(';', ',').split(',') if part.strip()]
        else:
            names = [str(name).strip() for name in (tracks_spec or ()) if str(name).strip()]
        wanted = set(names)
        selection = self.track_table.selectionModel()
        selection.clearSelection()
        for row in range(self.track_table.rowCount()):
            item = self.track_table.item(row, 0)
            name = str(item.data(Qt.ItemDataRole.UserRole) or item.text()) if item else ''
            if name in wanted:
                selection.select(self.track_table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        return [name for name in names if name in self.plans]

    def show_details(self):
        self.exception_table.setRowCount(0)
        names = self.selected_tracks()
        if len(names) != 1:
            self.details.setText('Selecione uma única faixa para ver o plano ou suas exceções.')
            return
        name = names[0]
        result = self.results.get(name)
        if result:
            rows = batch.exceptions(result)
            self.details.setText('Faixa de revisão: %s\nRascunho para o estúdio manual: %s' %
                                 (result.get('render') or '(nenhuma substituição)', result.get('draft') or '—'))
            for item in rows:
                codes = item.get('final_reasons') or []
                outcome = _OUTCOMES.get(item.get('outcome'), item.get('outcome') or '')
                reason_text = ' | '.join(batch.explain(code)['message'] for code in codes)
                if item.get('error'):
                    reason_text = (reason_text + ' — ' if reason_text else '') + str(item['error'])
                actions = ' | '.join(dict.fromkeys(batch.explain(code)['action'] for code in codes if batch.explain(code)['action']))
                self._append_exception(item, outcome, reason_text, actions)
        else:
            self.details.setText('Prévia do plano: as janelas .cut não são alinhamento fonético.')
            for item in self.plans.get(name, []):
                attention = item.get('status') == 'WARN' or any(code in batch.ATTENTION_SKIPS for code in item.get('reasons', []))
                if not attention:
                    continue
                codes = item.get('reasons') or []
                self._append_exception(item, 'Excluída no plano' if item.get('status') == 'SKIP' else 'Gerar com aviso',
                    ' | '.join(batch.explain(code)['message'] for code in codes),
                    ' | '.join(dict.fromkeys(batch.explain(code)['action'] for code in codes if batch.explain(code)['action'])))

    def _append_exception(self, item, outcome, reason, action):
        row = self.exception_table.rowCount()
        self.exception_table.insertRow(row)
        values = (item.get('key') or item.get('gid') or '', '%.2f' % float(item.get('start') or 0),
                  '%.2f' % float(item.get('end') or 0), outcome, reason, action)
        for column, value in enumerate(values):
            self.exception_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _start_run(self, names):
        if self._busy:
            return
        names = [name for name in names if name in self.plans and batch.summarize(self.plans[name])['generate']]
        if not names:
            QMessageBox.information(self, 'Lote', 'Nenhuma faixa selecionada tem falas geráveis no GXT atual.')
            return
        available, reason = batch.omnivoice_disponivel(self.project)
        if not available:
            QMessageBox.warning(self, 'OmniVoice indisponível', reason)
            return
        policy = self._policy()
        self._worker_policy = policy
        snapshot = {'names': list(names), 'report': self.report, 'tracks': dict(self.tracks),
                    'plans': {name: list(self.plans[name]) for name in names}, 'policy': policy,
                    'project': self.project}
        speech_count = sum(batch.summarize(snapshot['plans'][name])['generate'] for name in names)
        answer = QMessageBox.question(self, 'Confirmar geração',
            '%d faixa(s), %d fala(s). A geração usará o OmniVoice local. Falhas e cancelamentos preservam os relatórios parciais; nada será gravado no jogo ou no GXT.\n\nIniciar agora?' %
            (len(names), speech_count), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.progress.setValue(0)
        self._start_worker('run', self._run_sync, snapshot)

    def run_selected(self):
        self._start_run(self.selected_tracks())

    def run_all(self):
        self._start_run(batch.select_tracks(self.plans))

    def _run_sync(self, snapshot, cancel_event, progress, status):
        policy = snapshot['policy']
        factory = batch.omnivoice_factory(snapshot['project'], snapshot['report'], policy)
        results, errors = {}, {}
        done = 0
        for name in snapshot['names']:
            if cancel_event.is_set():
                break
            items = snapshot['plans'][name]
            def report_progress(count, total, message, base=done):
                progress(base + count, sum(batch.summarize(snapshot['plans'][track])['generate']
                                           for track in snapshot['names']), message)
            try:
                result = batch.run_track(snapshot['project'], snapshot['report'], snapshot['tracks'][name],
                                         items, factory, policy, cancel_event, report_progress)
                results[name] = result
            except batch.CancelledError:
                saved = batch.batch_dir(snapshot['project'], name) / 'report.json'
                if saved.is_file():
                    try:
                        partial = json.loads(saved.read_text(encoding='utf-8'))
                        if partial.get('track') == name:
                            results[name] = partial
                    except (OSError, ValueError):
                        pass
                break
            except (ValueError, RuntimeError, OSError) as exc:
                errors[name] = str(exc)
                continue
            done += batch.summarize(items)['generate']
        summary = None
        if results:
            summary = str(batch.write_summary_csv(snapshot['project'], list(results.values())))
        return {'results': results, 'errors': errors, 'csv': summary, 'cancelled': cancel_event.is_set()}

    def cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText('Cancelamento solicitado; a fala ou extração atual será encerrada com segurança.')

    def open_scene(self):
        names = self.selected_tracks()
        if len(names) == 1 and names[0] in self.tracks:
            self.sceneRequested.emit({'track': dict(self.tracks[names[0]]), 'report': self.report})

    def open_folder(self):
        folder = self.project / 'workspace' / 'cutscene_studio' / 'batch'
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            self.status.setText('A pasta de resultados será criada ao gerar a primeira faixa.')

    def export_csv(self):
        if not self.results:
            QMessageBox.information(self, 'Resumo CSV', 'Gere ao menos uma faixa antes de exportar o resumo.')
            return
        try:
            path = batch.write_summary_csv(self.project, list(self.results.values()))
            self.status.setText('Resumo salvo em %s' % path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Resumo CSV', str(exc))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self._close_after_worker = True
            self.worker.cancel()
            self.status.setText('Cancelando e aguardando a etapa atual antes de fechar…')
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)
