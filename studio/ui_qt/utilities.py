"""Native Qt replacements for the remaining small classic utility dialogs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QProgressBar,
    QPushButton, QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from studio import config
from studio.audio.resolver import BaseAudioResolver, detectar_saat_sfx
from studio.script.mapping import PAKS


class _Worker(QThread):
    message = Signal(str)
    result = Signal(object, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            value = self.fn(self.message.emit)
            self.result.emit(value, None)
        except Exception as exc:  # deliver errors on the UI thread
            self.result.emit(None, str(exc))


class UtilityDialog(QDialog):
    changed = Signal()

    def __init__(self, title, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.setWindowTitle(title)
        self.resize(900, 620)
        self._worker = None
        self._cancel = Event()
        self.layout_root = QVBoxLayout(self)
        self.status = QLabel('Pronto. Escolha uma opção abaixo.')
        self.status.setWordWrap(True)
        self.layout_root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.layout_root.addWidget(self.progress)
        self.cancel_button = QPushButton('Cancelar operação')
        self.cancel_button.hide()
        self.layout_root.addWidget(self.cancel_button)
        self.cancel_button.clicked.connect(self._cancel.set)

    def start_work(self, label, fn, done=None, cancellable=False):
        if self._worker and self._worker.isRunning():
            return False
        self._cancel.clear()
        self.status.setText(label)
        self.progress.show()
        self.cancel_button.setVisible(cancellable)
        worker = _Worker(fn, self)
        self._worker = worker
        worker.message.connect(self.status.setText)
        worker.result.connect(lambda value, error: self._work_finished(value, error, done))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def _work_finished(self, value, error, done):
        worker = self._worker
        if worker:
            worker.wait()
            worker.setParent(None)
        self.progress.hide()
        self.cancel_button.hide()
        self._worker = None
        if error:
            self.status.setText('Falha: ' + error)
            QMessageBox.warning(self, 'Operação', error)
        elif done:
            previous_status = self.status.text()
            done(value)
            if self._worker is None:
                if self.status.text() == previous_status:
                    self.status.setText('Concluído.')

    def operation_active(self):
        return self._worker is not None

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self.status.setText('Aguarde a operação em andamento terminar para fechar esta janela.')
            event.ignore()
            return
        super().closeEvent(event)


class NativeSourcesDialog(UtilityDialog):
    def __init__(self, context, parent=None):
        super().__init__('Fontes do jogo — SCRIPT, cutscenes e legendas', context, parent)
        self.report = None
        self.rows = []
        intro = QLabel('Relatório do índice do jogo base. Os vínculos distinguem SCRIPT, cutscenes e usos de interface; uso de interface não comprova ausência de voz.')
        intro.setWordWrap(True)
        self.layout_root.addWidget(intro)
        row = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem('Somente desconhecidas', 'unknown')
        self.filter.addItem('SCRIPT', 'script')
        self.filter.addItem('Cutscenes', 'cutscene')
        self.filter.addItem('Uso de interface', 'interface_usage')
        self.filter.addItem('Todas', None)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Pesquisar tabela, chave ou legenda')
        self.refresh_button = QPushButton('Atualizar relatório')
        self.build_button = QPushButton('Gerar índice do jogo base')
        row.addWidget(self.filter); row.addWidget(self.search, 1)
        row.addWidget(self.refresh_button); row.addWidget(self.build_button)
        self.layout_root.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Tabela', 'Chave', 'Fonte/uso', 'Legenda original'])
        self.table.setColumnWidth(0, 90); self.table.setColumnWidth(1, 160); self.table.setColumnWidth(2, 190)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(150)
        splitter = QSplitter(); splitter.setOrientation(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.Orientation.Vertical)
        splitter.addWidget(self.table); splitter.addWidget(self.details); splitter.setStretchFactor(0, 1)
        self.layout_root.addWidget(splitter, 1)
        self.refresh_button.clicked.connect(self.refresh)
        self.build_button.clicked.connect(self.build)
        self.filter.currentIndexChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        self.table.itemSelectionChanged.connect(self.show_details)
        self.refresh()

    def _load(self, emit):
        from studio.script.native_index import load_current
        from studio.ui.native_sources import index_report
        root = Path(self.context.project_root).resolve()
        game = getattr(getattr(self.context, 'game_audio', None), 'game_dir', None) or config.CFG.get('game_dir')
        report = load_current(root, game)
        return (report, index_report(report)) if report else (None, None)

    def refresh(self):
        self.report = None
        self.rows = []
        self.table.setRowCount(0)
        self.start_work('Lendo o relatório atual do índice…', self._load, self._loaded)

    def build(self):
        game = getattr(getattr(self.context, 'game_audio', None), 'game_dir', None) or config.CFG.get('game_dir')
        if not game:
            QMessageBox.information(self, 'Jogo base', 'Configure a pasta do GTA San Andreas primeiro.')
            return
        root = Path(self.context.project_root).resolve()
        script = Path(config.APP_DIR) / 'tools' / 'build_native_index.py'
        def run(emit):
            emit('Indexando os arquivos do jogo base em segundo plano…')
            proc = subprocess.run([sys.executable, '-u', str(script), '--game', str(game), '--project', str(root)],
                                  cwd=str(config.APP_DIR), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout or 'A indexação terminou com erro.')[-4000:])
            return proc.stdout[-4000:] or 'Índice atualizado.'
        def done(message):
            self.status.setText(message)
            self.changed.emit()
            self.refresh()
        self.start_work('Preparando a indexação…', run, done)

    def _loaded(self, loaded):
        report, indexes = loaded
        self.report = report
        self.records_by_id = indexes[1] if indexes else {}
        if report:
            built = report.get('generated_at') or report.get('created_at') or 'data não informada'
            self.status.setText('Índice atual carregado (%s). %s classificação(ões).' %
                                (built, len(report.get('classifications', ()))))
        else:
            self.status.setText('Nenhum índice atual para esta instalação. Gere o índice do jogo base.')
        self.render()

    def render(self, *_):
        if not self.report:
            return
        from studio.ui.native_sources import LABELS, filter_classifications
        category = self.filter.currentData()
        rows = filter_classifications(self.report, category, self.search.text())
        self.rows = rows[:500]
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            values = (row.get('table', ''), ', '.join(row.get('names') or ()) or row.get('gxt_stable_id', '').split(':')[-1],
                      LABELS.get(row.get('category'), 'Desconhecida'), row.get('source_text', ''))
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(32, row)
                self.table.setItem(i, col, item)
        self.status.setText('%d entradas correspondem; exibindo até 500. O relatório é informativo e não altera arquivos do jogo.' % len(rows))

    def show_details(self):
        row = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        data = row.data(32) if row else None
        if not data:
            self.details.clear(); return
        lines = [data.get('gxt_stable_id', ''), data.get('source_text', '')]
        for record in self.records_by_id.get(data.get('gxt_stable_id'), ()):
            source = (record.get('source_wav_path') or record.get('source_path') or
                      record.get('path') or record.get('file') or 'caminho não registrado')
            lines.append('Referência %s · %s · %s · %s' %
                         (record.get('source_type') or record.get('origin') or record.get('pak', 'áudio'),
                          record.get('stable_audio_id') or 'ID não registrado',
                          record.get('evidence_method') or 'evidência não registrada', source))
        for use in data.get('uses', ())[:30]:
            lines.append('SCM @%s · %s · %s' % (use.get('offset'), use.get('segment'), use.get('command')))
        self.details.setPlainText('\n'.join(lines))


def _configured_roots(project_root, game_dir):
    roots = list(config.CFG.get('audio_search_roots') or [])
    cache = project_root / '.cache' / 'saat'
    if cache.is_dir():
        for sub in cache.iterdir():
            exp = sub / 'export' / 'SCRIPT'
            if exp.is_dir(): roots.append(exp)
            elif (sub / 'export').is_dir(): roots.append(sub / 'export')
    for name in ('audio_original', 'original_audio', 'audio_extracted', 'extracted_audio'):
        p = project_root / name
        if p.is_dir(): roots.append(p)
    if game_dir and (Path(game_dir) / 'audio' / 'SFX' / 'SCRIPT').is_dir():
        roots.append(Path(game_dir) / 'audio' / 'SFX' / 'SCRIPT')
    return BaseAudioResolver.normalize_roots(roots)


class AudioFoldersDialog(UtilityDialog):
    def __init__(self, context, parent=None):
        super().__init__('Pastas de áudio base — AutoSearch', context, parent)
        self.list = QListWidget(); self.layout_root.addWidget(QLabel('Pastas adicionais pesquisadas para localizar WAVs originais:'))
        self.layout_root.addWidget(self.list, 1)
        buttons = QHBoxLayout(); self.add_button = QPushButton('Adicionar pasta…'); self.remove_button = QPushButton('Remover selecionadas'); self.scan_button = QPushButton('Salvar e reindexar')
        buttons.addWidget(self.add_button); buttons.addWidget(self.remove_button); buttons.addStretch(1); buttons.addWidget(self.scan_button)
        self.layout_root.addLayout(buttons)
        self.roots = [str(Path(p).expanduser().resolve()) for p in config.CFG.get('audio_search_roots', []) or []]
        self._render()
        self.add_button.clicked.connect(self.add); self.remove_button.clicked.connect(self.remove); self.scan_button.clicked.connect(self.save_and_scan)

    def _render(self):
        self.list.clear(); self.list.addItems(self.roots)

    def add(self):
        chosen = QFileDialog.getExistingDirectory(self, 'Adicionar pasta de áudio', str(self.context.project_root))
        if chosen:
            p = str(Path(chosen).resolve())
            if os.path.normcase(p) not in {os.path.normcase(x) for x in self.roots}:
                self.roots.append(p); self._render()

    def remove(self):
        indexes = sorted((self.list.row(x) for x in self.list.selectedItems()), reverse=True)
        for i in indexes: self.roots.pop(i)
        self._render()

    def save_and_scan(self):
        config.CFG['audio_search_roots'] = list(self.roots); config.cfg_save()
        project = Path(self.context.project_root).resolve()
        roots = _configured_roots(project, getattr(getattr(self.context, 'game_audio', None), 'game_dir', None) or config.CFG.get('game_dir'))
        events = [p for p in (project / 'data/AudioEvents.txt', project / 'AudioEvents.txt') if p.is_file()]
        game = config.CFG.get('game_dir')
        if game and (Path(game) / 'data/AudioEvents.txt').is_file(): events.append(Path(game) / 'data/AudioEvents.txt')
        resolver = self.context.base_audio
        def scan(emit):
            emit('Indexando WAVs das pastas selecionadas…')
            resolver.scan(roots, events)
            return len(resolver.records)
        def done(count):
            self.context._resolver_ready = True
            self.status.setText('AutoSearch atualizado: %d WAV(s) em %d pasta(s).' % (count, len(roots)))
            self.changed.emit()
        self.start_work('Salvando pastas e preparando a busca…', scan, done)


class DiagnosticsDialog(UtilityDialog):
    def __init__(self, context, parent=None):
        super().__init__('Diagnóstico do sistema', context, parent)
        self.text = QPlainTextEdit(); self.text.setReadOnly(True); self.layout_root.addWidget(self.text, 1)
        row = QHBoxLayout(); copy = QPushButton('Copiar relatório'); refresh = QPushButton('Atualizar'); close = QPushButton('Fechar')
        row.addWidget(copy); row.addWidget(refresh); row.addStretch(1); row.addWidget(close); self.layout_root.addLayout(row)
        copy.clicked.connect(lambda: __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication.clipboard().setText(self.text.toPlainText()))
        refresh.clicked.connect(self.refresh); close.clicked.connect(self.accept); self.refresh()

    def refresh(self):
        def collect(emit):
            emit('Coletando diagnóstico somente leitura…')
            context = self.context
            game = getattr(context, 'game_audio', None)
            root = Path(context.project_root)
            lines = ['GXT Editor & Dubbing Studio — diagnóstico (%s)' % datetime.now().strftime('%d/%m/%Y %H:%M'),
                     'Python %s · %s' % (sys.version.split()[0], sys.platform), '',
                     'Raiz do projeto: %s' % root,
                     'GXT aberto: %s' % ('sim' if getattr(context.document, 'gxt', None) else 'não'),
                     'Mapeamentos SCRIPT carregados: %s' % len(getattr(context, 'script_db', {})),
                     'GTA SA: %s' % (getattr(game, 'game_dir', None) or 'não configurado')]
            saat = game.saat() if game else {'sfx': detectar_saat_sfx(config.CFG.get('saat_sfx'), config.CFG.get('game_dir')), 'stream': None}
            lines += ['SAAT SFX: %s' % (saat.get('sfx') or 'não encontrado'), 'SAAT stream: %s' % (saat.get('stream') or 'não encontrado'),
                      'Pastas adicionais AutoSearch: %d' % len(config.CFG.get('audio_search_roots', []) or []),
                      'WAVs no índice carregado: %d' % len(getattr(getattr(context, 'base_audio', None), 'records', [])),
                      'FFmpeg no PATH: %s' % (shutil.which('ffmpeg') or 'não encontrado (opcional)')]
            return '\n'.join(lines)
        self.start_work('Coletando diagnóstico…', collect, self.text.setPlainText)


class ArchiveExtractDialog(UtilityDialog):
    def __init__(self, context, parent=None):
        super().__init__('Extrair bancos de áudio', context, parent)
        self.game = getattr(context, 'game_audio', None)
        if not self.game:
            self.layout_root.addWidget(QLabel('Serviço de áudio do jogo indisponível neste contexto.'))
            return
        self.layout_root.addWidget(QLabel('Escolha os bancos SFX disponíveis. A extração preserva bancos concluídos e salva no cache do projeto; rádio e CUTSCENE não são suportados por este extrator.'))
        self.paks = {}
        available = set(self.game.available_archives('original')) | set(self.game.available_archives('mod'))
        shortcuts = QHBoxLayout()
        for label, preset in (('Missões · SCRIPT', 'missoes'), ('NPCs · SPC', 'npcs'),
                              ('Diálogos · SCRIPT + SPC', 'dialogos')):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, name=preset: self.apply_preset(name))
            shortcuts.addWidget(button)
        self.layout_root.addLayout(shortcuts)
        for pak in PAKS:
            box = QCheckBox('%s — %s' % (pak, 'disponível' if pak in available else 'não encontrado'))
            box.setEnabled(pak in available); box.setChecked(pak == 'SCRIPT' and pak in available)
            self.paks[pak] = box; self.layout_root.addWidget(box)
        form = QFormLayout(); self.target = QComboBox(); self.target.addItems(['Original e mod', 'Somente original', 'Somente mod']); form.addRow('Origem:', self.target)
        self.force = QCheckBox('Refazer mesmo se o cache estiver atualizado'); form.addRow(self.force); self.layout_root.addLayout(form)
        row = QHBoxLayout(); run = QPushButton('Extrair selecionados'); close = QPushButton('Fechar'); row.addStretch(1); row.addWidget(run); row.addWidget(close); self.layout_root.addLayout(row)
        run.clicked.connect(self.extract); close.clicked.connect(self.accept)

    def apply_preset(self, preset):
        from studio.ui.archive_extract import selection_for_preset
        selected = selection_for_preset(preset)
        for pak, box in self.paks.items():
            box.setChecked(box.isEnabled() and selected.get(pak, False))

    def extract(self):
        selected = [p for p in PAKS if self.paks[p].isChecked() and self.paks[p].isEnabled()]
        if not selected:
            QMessageBox.information(self, 'Extração', 'Marque ao menos um banco disponível.'); return
        target = {'Original e mod': 'ambos', 'Somente original': 'jogo', 'Somente mod': 'mod'}[self.target.currentText()]
        force = self.force.isChecked()
        def work(emit):
            emit('Extraindo %d banco(s) pelo SAAT…' % len(selected))
            return self.game.indexar_arquivos(selected, alvo=target, on_line=emit, cancel=self._cancel, forcar=force)
        def done(result):
            self.status.setText('Extração finalizada.' if result.get('ok') else 'Extração parcial: ' + str(result.get('motivo') or result))
            if result.get('ok'): self.changed.emit()
        self.start_work('Preparando extração…', work, done, cancellable=True)


class SaatDialog(UtilityDialog):
    def __init__(self, context, parent=None):
        super().__init__('Gerenciador SAAT', context, parent)
        self.game = getattr(context, 'game_audio', None)
        form = QFormLayout()
        self.sfx = QLineEdit(str(config.CFG.get('saat_sfx') or ''))
        self.stream = QLineEdit(str(config.CFG.get('saat_stream') or ''))
        for label, edit, key in (('saat_sfx.exe:', self.sfx, 'saat_sfx'), ('saat_stream.exe:', self.stream, 'saat_stream')):
            row = QHBoxLayout(); row.addWidget(edit, 1); browse = QPushButton('Selecionar…'); row.addWidget(browse)
            browse.clicked.connect(lambda _=False, e=edit, k=key: self._browse(e, k))
            form.addRow(label, row)
        self.layout_root.addLayout(form)
        if self.game:
            self.layout_root.addWidget(QLabel('Jogo: %s' % (self.game.game_dir or 'não configurado')))
            self.layout_root.addWidget(QLabel('Mod ativo: %s' % self.game.mod_ativo()))
        self.layout_root.addWidget(QLabel('Exportação usa o backend SAAT existente e grava somente no cache de áudio do projeto.'))
        controls = QFormLayout(); self.pak = QComboBox(); self.pak.addItems(PAKS); self.target = QComboBox(); self.target.addItems(['Jogo original', 'Mod ativo'])
        controls.addRow('Banco SFX:', self.pak); controls.addRow('Origem:', self.target); self.layout_root.addLayout(controls)
        buttons = QHBoxLayout(); save = QPushButton('Salvar ferramentas'); export = QPushButton('Exportar banco'); close = QPushButton('Fechar')
        buttons.addWidget(save); buttons.addWidget(export); buttons.addStretch(1); buttons.addWidget(close); self.layout_root.addLayout(buttons)
        save.clicked.connect(self.save); export.clicked.connect(self.export); close.clicked.connect(self.accept)
        detected = detectar_saat_sfx(config.CFG.get('saat_sfx'), config.CFG.get('game_dir'))
        if not self.sfx.text() and detected: self.sfx.setText(str(detected))

    def _browse(self, edit, key):
        path, _ = QFileDialog.getOpenFileName(self, 'Selecionar ferramenta SAAT', edit.text(), 'Executável (*.exe);;Todos os arquivos (*)')
        if path: edit.setText(path)

    def save(self):
        for key, edit in (('saat_sfx', self.sfx), ('saat_stream', self.stream)):
            value = edit.text().strip()
            if value: config.CFG[key] = value
            else: config.CFG.pop(key, None)
        config.cfg_save(); self.status.setText('Configuração SAAT salva.'); self.changed.emit()

    def export(self):
        if not self.game:
            QMessageBox.information(self, 'SAAT', 'Serviço do jogo indisponível neste contexto.'); return
        self.save()
        pak, target = self.pak.currentText(), self.target.currentText()
        def work(emit):
            emit('Exportando %s do %s pelo SAAT…' % (pak, target))
            if target == 'Mod ativo': return self.game.indexar_mod(pak=pak, on_line=emit, cancel=self._cancel)
            return self.game.indexar(pak=pak, on_line=emit, cancel=self._cancel)
        def done(result):
            self.status.setText('Banco exportado: %s WAV(s).' % result.get('wavs', 0) if result.get('ok') else 'Exportação falhou: ' + str(result.get('motivo') or result))
            if result.get('ok'): self.changed.emit()
        self.start_work('Preparando exportação…', work, done, cancellable=True)


def create_utility_dialog(key, context, parent=None):
    """Create one of the native utility dialogs used by the Qt application."""
    factories = {'native_sources': NativeSourcesDialog, 'audio_folders': AudioFoldersDialog,
                 'diagnostics': DiagnosticsDialog, 'archive_extract': ArchiveExtractDialog,
                 'saat': SaatDialog}
    try:
        factory = factories[key]
    except KeyError:
        raise ValueError('Ferramenta Qt desconhecida: %s' % key) from None
    return factory(context, parent)
