import sys
import time
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from studio.ui_qt.voice_analysis import VoiceAnalysisWidget


def test_analysis_is_opt_in_and_uses_existing_cli_scope(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    context = SimpleNamespace(project_root=tmp_path, document=SimpleNamespace(gxt=SimpleNamespace(order=['INTRO1'])))
    from studio.audio import voice_runtime
    monkeypatch.setattr(voice_runtime, 'runtime_status', lambda _: dict(ready=True, can_setup=True, reason='Disponível'))
    commands = []
    def command(root, tables=None):
        commands.append((root, tables))
        return [sys.executable, '-c', 'print("resultado salvo")']
    monkeypatch.setattr(voice_runtime, 'build_analysis_command', command)
    widget = VoiceAnalysisWidget(context)
    assert widget.process is None
    assert commands == []
    results = []
    widget.completed.connect(lambda: results.append(True))
    widget.analyze.click()
    deadline = time.monotonic() + 10
    while widget.busy() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert commands == [(tmp_path, ['INTRO1'])]
    assert results == [True]
    assert 'resultado salvo' in widget.logs.toPlainText()
    assert not widget.busy()
    widget.close()


def test_missing_runtime_does_not_offer_fake_analysis(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from studio.audio import voice_runtime
    monkeypatch.setattr(voice_runtime, 'runtime_status', lambda _: dict(ready=False, can_setup=False, reason='Configure Python'))
    widget = VoiceAnalysisWidget(SimpleNamespace(project_root=tmp_path))
    assert not widget.analyze.isEnabled()
    assert not widget.setup.isEnabled()
    assert widget.status.text() == 'Configure Python'
    assert widget.process is None
    widget.close()
