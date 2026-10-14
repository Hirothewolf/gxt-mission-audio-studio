import json
import os
import threading
import time
import urllib.error
import unittest
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from studio.text.hypertranslate import (
    GoogleTranslationV2,
    HypertranslateCancelled,
    HypertranslateError,
    choose_route,
    run_hypertranslate,
)


class FakeTranslator:
    def __init__(self, cancel_event=None):
        self.calls = []
        self.cancel_event = cancel_event

    def translate(self, text, target, source=None, cancel_event=None):
        self.calls.append((text, target, source))
        if self.cancel_event and len(self.calls) == 1:
            self.cancel_event.set()
        return f"{target}:{text}", source or "en"


def test_route_is_reproducible_unique_and_excludes_portuguese_and_final():
    seed, first = choose_route(100, "pt", 92831)
    same_seed, second = choose_route(100, "pt", 92831)

    assert seed == same_seed == 92831
    assert first == second
    assert len(first) == len(set(first)) == 100
    assert not (set(first) & {"pt", "pt-BR", "pt-PT"})


def test_pipeline_preserves_gxt_controls_and_checkpoints_each_stage(tmp_path):
    source = "Go ~z~\nUse ~k~~ACTION~ now ~1~!"
    translator = FakeTranslator()
    checkpoint = tmp_path / "runs" / "run.json"
    result = run_hypertranslate(
        source, rounds=1, final_language="pt", translator=translator,
        identity={"table": "MAIN", "hash": 0x12345678}, seed=4,
        checkpoint_path=checkpoint,
    )

    assert result.text.count("~z~") == 1
    assert result.text.count("~k~~ACTION~") == 1
    assert result.text.count("~1~") == 1
    assert result.text.count("\n") == 1
    assert [call[1] for call in translator.calls] == [result.route[0]] * 4 + ["pt"] * 4
    assert all("~" not in call[0] and "\n" not in call[0] for call in translator.calls)
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["status"] == "complete"
    assert data["route_seed"] == 4
    assert [stage["kind"] for stage in data["stages"]] == ["original", "intermediate", "final"]
    assert data["stages"][-1]["text"] == result.text
    assert data["identity"] == {"table": "MAIN", "hash": 0x12345678}


def test_cancellation_keeps_completed_stages_and_marks_checkpoint(tmp_path):
    cancel = threading.Event()
    translator = FakeTranslator(cancel_event=cancel)
    checkpoint = tmp_path / "cancelled.json"
    with pytest.raises(HypertranslateCancelled):
        run_hypertranslate(
            "hello", rounds=1, final_language="pt", translator=translator,
            seed=1, cancel_event=cancel, checkpoint_path=checkpoint,
        )

    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["status"] == "cancelled"
    assert len(data["stages"]) == 2


def test_api_error_does_not_expose_key_or_request_url(monkeypatch):
    key = "secret-test-key"

    def fail(_request, timeout):
        raise urllib.error.HTTPError(
            f"https://translation.googleapis.com/?key={key}", 403, "Forbidden", {},
            __import__("io").BytesIO(json.dumps({"error": {"message": f"bad key {key}"}}).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    translator = GoogleTranslationV2(api_key=key)
    with pytest.raises(HypertranslateError) as caught:
        translator.translate("hello", "es")
    assert key not in str(caught.value)
    assert "URL" not in str(caught.value)
    assert "autenticação foi recusada" in str(caught.value)


class QtHypertranslateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_cancel_after_worker_completed_suppresses_queued_result(self):
        from types import SimpleNamespace
        from studio.ui_qt.hypertranslate import HypertranslateBar
        with __import__('tempfile').TemporaryDirectory() as temporary:
            widget = HypertranslateBar(SimpleNamespace(project_root=Path(temporary)))
            widget._worker = SimpleNamespace(cancel=lambda: None, isRunning=lambda: False)
            results = []
            widget.resultReady.connect(results.append)
            widget.cancel()
            widget._on_succeeded(dict(text='late result'))
            self.assertEqual(results, [])
            self.assertEqual(widget.status_label.text(), 'Cancelado')
            widget._worker = None
            widget.close()

    def test_widget_emits_original_identity_and_source_snapshot(self):
        from studio.ui_qt.hypertranslate import HypertranslateBar

        with __import__("tempfile").TemporaryDirectory() as temporary:
            class Context:
                project_root = Path(temporary)

            widget = HypertranslateBar(Context())
            widget._selected_translator = lambda: FakeTranslator()
            widget.round_combo.setCurrentIndex(widget.round_combo.findData(1))
            results = []
            widget.resultReady.connect(results.append)
            identity = {"table": "MAIN", "hash": 0x87654321}
            self.assertTrue(widget.start("original line ~z~", identity))
            identity["hash"] = 123
            deadline = time.monotonic() + 5
            while not results and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertTrue(results, "worker did not emit a result")

            result = results[0]
            self.assertEqual(result["source_text"], "original line ~z~")
            self.assertEqual(result["identity"], {"table": "MAIN", "hash": 0x87654321})
            self.assertEqual(result["request"]["identity"], {"table": "MAIN", "hash": 0x87654321})
            self.assertEqual(result["text"].count("~z~"), 1)
            self.assertTrue(result["stages_path"].endswith(".json"))
            widget._api_key_session = "secret-session-only"
            widget._save_preferences()
            saved_preferences = widget._preferences_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-session-only", saved_preferences)
            self.assertNotIn('"api_key"', saved_preferences)
            if widget._worker is not None:
                widget._worker.wait(5000)
                self.app.processEvents()
            widget.close()
