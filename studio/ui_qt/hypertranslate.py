"""Compact inline controls for the multi-language text route."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from studio.text.hypertranslate import (
    ALLOWED_ROUNDS,
    LANGUAGES,
    GoogleTranslationV2,
    HypertranslateError,
    load_user_credentials,
    run_hypertranslate,
)


class _HypertranslateTask(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, request: dict, *, translator, checkpoint_path: Path, parent=None):
        super().__init__(parent)
        self.request = request
        self.translator = translator
        self.checkpoint_path = checkpoint_path
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = run_hypertranslate(
                self.request["source_text"],
                rounds=self.request["rounds"],
                final_language=self.request["final_language"],
                translator=self.translator,
                identity=self.request["identity"],
                cancel_event=self.cancel_event,
                checkpoint_path=self.checkpoint_path,
                progress=self.progress.emit,
            )
            payload = result.as_dict()
            payload["request_id"] = self.request["request_id"]
            payload["request"] = copy.deepcopy(self.request)
            self.succeeded.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)

    def cancel(self):
        self.cancel_event.set()


class _HypertranslateSettings(QDialog):
    def __init__(self, parent, *, final_language: str, auth_mode: str,
                 service_account_path: str, api_key_configured: bool):
        super().__init__(parent)
        self.setWindowTitle("Configurações do Hypertranslate")
        self.setModal(True)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.final_combo = QComboBox()
        for code, label in sorted(LANGUAGES.items(), key=lambda item: item[1].casefold()):
            self.final_combo.addItem(f"{label} ({code})", code)
        index = self.final_combo.findData(final_language)
        if index >= 0:
            self.final_combo.setCurrentIndex(index)
        form.addRow("Idioma final:", self.final_combo)

        self.auth_combo = QComboBox()
        self.auth_combo.addItem("Chave de API", "key")
        self.auth_combo.addItem("Conta de serviço (JSON)", "service_account")
        index = self.auth_combo.findData(auth_mode)
        if index >= 0:
            self.auth_combo.setCurrentIndex(index)
        form.addRow("Autenticação:", self.auth_combo)

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "Credencial já configurada" if api_key_configured else "Cole a chave; ela ficará apenas nesta sessão"
        )
        form.addRow("Chave de API:", self.api_key)

        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.service_path = QLineEdit(service_account_path)
        self.service_path.setPlaceholderText("Caminho do JSON de conta de serviço")
        browse = QPushButton("Procurar…")
        browse.clicked.connect(self._browse)
        path_layout.addWidget(self.service_path, 1)
        path_layout.addWidget(browse)
        form.addRow("Arquivo JSON:", path_row)
        layout.addLayout(form)

        note = QLabel("A chave nova fica somente em memória. As preferências do projeto não guardam credenciais.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Conta de serviço", "", "JSON (*.json);;Todos (*.*)")
        if selected:
            self.service_path.setText(selected)

    def values(self):
        return {
            "final_language": self.final_combo.currentData(),
            "auth_mode": self.auth_combo.currentData(),
            "service_account_path": self.service_path.text().strip(),
            "api_key": self.api_key.text().strip(),
        }


class HypertranslateBar(QWidget):
    """Small reusable toolbar widget for an inline hypertranslation request.

    Connect ``resultReady`` to the host application's document update method.
    The emitted mapping includes the captured ``identity`` and ``source_text``;
    the host should compare that snapshot with the current row/text before
    applying it through the document's undo-aware setter.
    """

    requestStarted = Signal(object)
    resultReady = Signal(object)
    failed = Signal(str)
    busyChanged = Signal(bool)

    PREFERENCES_NAME = "hypertranslate_preferences.json"

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.project_root = Path(context.project_root).resolve()
        self._preferences_path = self.project_root / "workspace" / self.PREFERENCES_NAME
        self._preferences = self._load_preferences()
        self._credentials = load_user_credentials()
        default_mode = self._credentials.get("auth_mode")
        if default_mode not in {"key", "service_account"}:
            default_mode = "service_account" if self._credentials.get("service_account_path") else "key"
        self._auth_mode = self._preferences.get("auth_mode", default_mode)
        if self._auth_mode not in {"key", "service_account"}:
            self._auth_mode = default_mode
        self._service_account_path = str(
            self._preferences.get("service_account_path") or self._credentials.get("service_account_path", "")
        )
        self._api_key_session = ""
        self._final_language = str(self._preferences.get("final_language", "pt"))
        if self._final_language not in LANGUAGES:
            self._final_language = "pt"
        rounds = self._preferences.get("rounds", 25)
        self._rounds = rounds if rounds in ALLOWED_ROUNDS else 25
        self._worker: _HypertranslateTask | None = None
        self._cancel_requested = False
        self._build_ui()

    def _load_preferences(self):
        try:
            data = json.loads(self._preferences_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save_preferences(self):
        # Credentials are intentionally excluded from this project-local file.
        data = {
            "rounds": self.round_combo.currentData(),
            "final_language": self._final_language,
            "auth_mode": self._auth_mode,
            "service_account_path": self._service_account_path,
        }
        self._preferences_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._preferences_path.with_name(self._preferences_path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._preferences_path)
        self._preferences = data

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.round_combo = QComboBox(self)
        for count in ALLOWED_ROUNDS:
            self.round_combo.addItem(str(count), count)
        self.round_combo.setCurrentIndex(self.round_combo.findData(self._rounds))
        self.round_combo.setToolTip("Quantidade de idiomas intermediários")
        layout.addWidget(self.round_combo)
        self.translate_button = QPushButton("Hipertraduzir", self)
        self.translate_button.setToolTip("Traduzir esta linha por vários idiomas")
        self.translate_button.clicked.connect(self._start_from_provider)
        layout.addWidget(self.translate_button)
        self.settings_button = QPushButton("⚙", self)
        self.settings_button.setFixedWidth(30)
        self.settings_button.setToolTip("Configurações do Hypertranslate")
        self.settings_button.clicked.connect(self.open_settings)
        layout.addWidget(self.settings_button)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(68)
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.setToolTip("Etapas da tradução")
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel("", self)
        self.status_label.setMinimumWidth(74)
        self.status_label.setMaximumWidth(126)
        self.status_label.setToolTip("Progresso da tradução")
        layout.addWidget(self.status_label)
        self.cancel_button = QPushButton("×", self)
        self.cancel_button.setFixedWidth(28)
        self.cancel_button.setToolTip("Cancelar tradução")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        layout.addWidget(self.cancel_button)
        self.round_combo.currentIndexChanged.connect(self._rounds_changed)

    def _start_from_provider(self):
        """Convenience entry point when the host supplies ``get_text_provider``."""
        provider = getattr(self, "get_text_provider", None)
        if not callable(provider):
            self.failed.emit("O editor ainda não forneceu a linha selecionada.")
            return
        try:
            snapshot = provider()
        except (ValueError, HypertranslateError) as exc:
            self.failed.emit(str(exc))
            return
        if isinstance(snapshot, dict):
            self.start(snapshot.get("text", ""), snapshot.get("identity"))
        else:
            text, identity = snapshot
            self.start(text, identity)

    def set_text_provider(self, provider):
        """Set a callable returning ``(text, identity)`` for the selected row."""
        self.get_text_provider = provider

    def _rounds_changed(self, _index):
        if self.round_combo.currentData() in ALLOWED_ROUNDS:
            self._rounds = self.round_combo.currentData()
            try:
                self._save_preferences()
            except OSError:
                self.failed.emit("Não foi possível guardar as preferências. A escolha continua válida nesta sessão.")

    def open_settings(self):
        dialog = _HypertranslateSettings(
            self, final_language=self._final_language, auth_mode=self._auth_mode,
            service_account_path=self._service_account_path,
            api_key_configured=bool(self._credentials.get("api_key") or self._api_key_session),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._final_language = values["final_language"]
        self._auth_mode = values["auth_mode"]
        self._service_account_path = values["service_account_path"]
        if values["api_key"]:
            self._api_key_session = values["api_key"]
        try:
            self._save_preferences()
        except OSError:
            self.failed.emit("Não foi possível guardar as preferências. A escolha continua válida nesta sessão.")

    def _selected_translator(self):
        if self._auth_mode == "service_account":
            path = self._service_account_path or self._credentials.get("service_account_path", "")
            return GoogleTranslationV2(service_account_path=path)
        key = self._api_key_session or self._credentials.get("api_key", "")
        return GoogleTranslationV2(api_key=key)

    def start(self, text: str, identity=None):
        """Start a request for an immutable current-row snapshot.

        ``resultReady`` later emits text and that same identity/source snapshot;
        this widget never edits the document itself.
        """
        if self._worker is not None and self._worker.isRunning():
            return False
        if not isinstance(text, str) or not text.strip():
            self.failed.emit("Selecione uma linha com texto antes de traduzir.")
            return False
        snapshot = {
            "request_id": uuid.uuid4().hex,
            "source_text": str(text),
            "identity": copy.deepcopy(identity),
            "rounds": int(self.round_combo.currentData()),
            "final_language": self._final_language,
        }
        try:
            translator = self._selected_translator()
        except HypertranslateError as exc:
            self.status_label.setText("Configuração")
            self.failed.emit(str(exc))
            return False
        checkpoint_dir = self.project_root / "workspace" / "hypertranslate_runs"
        checkpoint_path = checkpoint_dir / (snapshot["request_id"] + ".json")
        self._worker = _HypertranslateTask(snapshot, translator=translator,
                                           checkpoint_path=checkpoint_path, parent=self)
        self._cancel_requested = False
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._set_busy(True)
        self.status_label.setText("Iniciando…")
        self.requestStarted.emit(copy.deepcopy(snapshot))
        self._worker.start()
        return True

    def cancel(self):
        worker = self._worker
        if worker is not None:
            self._cancel_requested = True
            worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelando…")

    def operation_active(self):
        return self._worker is not None

    def _set_busy(self, busy: bool):
        self.translate_button.setEnabled(not busy)
        self.round_combo.setEnabled(not busy)
        self.settings_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress_bar.setRange(0, 0)
        self.busyChanged.emit(busy)

    def _on_progress(self, completed: int, total: int, language: str):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(completed)
        self.status_label.setText(f"{completed}/{total} · {language}")

    def _on_succeeded(self, result: dict):
        if self._cancel_requested:
            self.status_label.setText("Cancelado")
            return
        self.status_label.setText("Concluído")
        self.resultReady.emit(result)

    def _on_failed(self, message: str):
        self.status_label.setText("Cancelado" if "cancelad" in message.casefold() else "Falhou")
        if self.status_label.text() == "Falhou":
            self.failed.emit(message)

    def _on_finished(self):
        worker = self._worker
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._set_busy(False)
        self._worker = None

