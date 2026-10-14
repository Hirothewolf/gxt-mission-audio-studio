import copy
import os
import time
import wave

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from studio.ui_qt.timeline import TimelineWindow, ClipItem


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def make_wav(path, rate=24000):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.sin(np.arange(rate) * .1) * 8000).astype('<i2').tobytes())
    return path


def wait_job(app, window):
    end = time.monotonic() + 20
    while window.job is not None and time.monotonic() < end:
        app.processEvents()
        QTest.qWait(10)
    assert window.job is None, window.status.text()
    app.processEvents()


def cleanup(window):
    window.project._saved = copy.deepcopy(window.project.data)
    window.close()


def test_real_async_import_edit_split_render_and_reopen(app, tmp_path):
    path = make_wav(tmp_path / 'original.wav')
    voice = make_wav(tmp_path / 'voice.wav', 22050)
    window = TimelineWindow(tmp_path)
    window.show()
    window.open_record({'table': 'CUT', 'hash': 123, 'name': 'HELLO', 'info': {}}, path, 'Olá')
    wait_job(app, window)
    assert len(window.project.data['clips']) == 1
    assert window.project.sample_rate == 24000
    assert window.project.data['clips'][0]['metadata']['text'] == 'Olá'
    original_track = window.project.data['tracks'][0]
    assert original_track['locked'] and original_track['mute']
    window.import_wav(voice, position=1200)
    wait_job(app, window)
    selected = window.selected_id
    assert selected is not None
    assert len(window.view.scene().items()) == 2
    window.fields['position'].setValue(2400)
    window.fields['source_in'].setValue(100)
    window.apply_inspector()
    assert window.project.clip(selected)['position'] == 2400
    assert window.project.clip(selected)['source_in'] == 100
    window.seek(4800)
    window.split_selected()
    assert len(window.project.data['clips']) == 3
    window.undo()
    assert len(window.project.data['clips']) == 2
    window.redo()
    assert len(window.project.data['clips']) == 3
    window.project.save(tmp_path / 'project.timeline.json')
    expected = copy.deepcopy(window.project.data)
    window.render_to(tmp_path / 'render.wav')
    wait_job(app, window)
    assert (tmp_path / 'render.wav').is_file(), window.status.text()
    assert '2 clipes' in window.status.text()
    window.load_project(tmp_path / 'project.timeline.json')
    wait_job(app, window)
    assert window.project.data == expected
    cleanup(window)


def test_graphics_drag_trim_seek_zoom_and_delete(app, tmp_path):
    path = make_wav(tmp_path / 'voice.wav')
    window = TimelineWindow(tmp_path)
    window.show()
    window.import_wav(path, position=12000)
    wait_job(app, window)
    clip_id = window.selected_id
    item = next(i for i in window.view.scene().items() if isinstance(i, ClipItem))
    origin = window.view.mapFromScene(item.mapToScene(QPointF(30, 40)))
    target = origin + window.view.mapFromScene(QPointF(45, 0)) - window.view.mapFromScene(QPointF(0, 0))
    QTest.mousePress(window.view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(window.view.viewport(), target, 20)
    QTest.mouseRelease(window.view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    app.processEvents()
    assert window.project.clip(clip_id)['position'] == 24000
    window.undo()
    assert window.project.clip(clip_id)['position'] == 12000
    item = next(i for i in window.view.scene().items() if isinstance(i, ClipItem))
    right = window.view.mapFromScene(item.mapToScene(QPointF(item.length - 2, 40)))
    shorter = right - (window.view.mapFromScene(QPointF(18, 0)) - window.view.mapFromScene(QPointF(0, 0)))
    QTest.mousePress(window.view.viewport(), Qt.MouseButton.LeftButton, pos=right)
    QTest.mouseMove(window.view.viewport(), shorter, 20)
    QTest.mouseRelease(window.view.viewport(), Qt.MouseButton.LeftButton, pos=shorter)
    app.processEvents()
    assert window.project.clip(clip_id)['source_out'] < 24000
    window.undo()
    assert window.project.clip(clip_id)['source_out'] == 24000
    old_zoom = window.view.pixels_per_second
    window.zoom(2)
    assert window.view.pixels_per_second == old_zoom * 2
    blank = window.view.mapFromScene(QPointF(30, 10))
    QTest.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, pos=blank)
    assert abs(window.cursor_frame - 4000) <= 150
    window.select_clip(clip_id)
    window.delete_selected()
    assert not window.project.data['clips']
    window.undo()
    assert len(window.project.data['clips']) == 1
    cleanup(window)


def test_close_cancels_worker_before_destroying_dialog(app, tmp_path):
    window = TimelineWindow(tmp_path)
    window.show()
    applied = []
    def operation(cancel, progress):
        while not cancel.wait(.01):
            progress(0, 0, 'Trabalhando')
        raise InterruptedError('Cancelado')
    window._start(operation, applied.append)
    app.processEvents()
    window.close()
    assert window.job is not None
    wait_job(app, window)
    assert not window.isVisible()
    assert not applied


def test_seconds_inspector_preserves_exact_frames_and_review_signal(app, tmp_path):
    path = make_wav(tmp_path / 'voice.wav', 22050)
    window = TimelineWindow(tmp_path)
    window.import_wav(path, metadata={'table': 'CUT', 'gxt_hash': 123, 'text': 'Olá'})
    wait_job(app, window)
    clip_id = window.selected_id
    window.project.update_clip(clip_id, position=133, source_in=133)
    window.select_clip(clip_id)
    window.apply_inspector()
    assert window.project.clip(clip_id)['position'] == 133
    assert window.project.clip(clip_id)['source_in'] == 133
    window.seconds_fields['position'].setValue(.5)
    window.seconds_fields['source_in'].setValue(.125)
    window.seconds_fields['fade_in'].setValue(.01)
    window.apply_inspector()
    clip = window.project.clip(clip_id)
    assert clip['position'] == round(.5 * 22050)
    assert clip['source_in'] == round(.125 * 22050)
    assert clip['fade_in'] == round(.01 * 22050)
    received = []
    window.reviewRequested.connect(received.append)
    window.review_button.setEnabled(True)
    window.review_button.click()
    assert received[0]['timeline_clip_id'] == clip_id
    assert received[0]['gxt_hash'] == 123 and received[0]['source_sample_rate'] == 22050
    window.show()
    window.clip_text.setFocus()
    window.clip_text.clear()
    QTest.keyClicks(window.clip_text, 'S')
    QTest.keyClick(window.clip_text, Qt.Key.Key_Delete)
    assert window.clip_text.toPlainText() == 'S'
    assert len(window.project.data['clips']) == 1
    cleanup(window)


def test_import_groups_characters_and_manual_selected_layer(app, tmp_path):
    source = make_wav(tmp_path / ('a' * 64 + '.wav'))
    window = TimelineWindow(tmp_path)
    window.open_source(source)
    wait_job(app, window)
    assert window.project.data['tracks'][0]['name'] == 'Original da cena'
    window.import_wav(source, position=111, metadata={'character': 'CJ'})
    wait_job(app, window)
    cj_id = window.selected_id
    cj_track = window.project.clip(cj_id)['track_id']
    window.import_wav(source, position=222, metadata={'character': 'Smoke'})
    wait_job(app, window)
    smoke_track = window.project.clip(window.selected_id)['track_id']
    assert cj_track != smoke_track
    window.import_wav(source, position=333, metadata={'character': 'CJ'})
    wait_job(app, window)
    assert window.project.clip(window.selected_id)['track_id'] == cj_track
    assert window.project.clip(window.selected_id)['position'] == 333
    window.select_clip(cj_id)
    window.import_wav(source, position=444)
    wait_job(app, window)
    assert window.project.clip(window.selected_id)['track_id'] == cj_track
    window.import_wav(source, position=450, metadata={'character': 'CJ'})
    wait_job(app, window)
    assert window.project.clip(window.selected_id)['track_id'] == cj_track
    window.selected_id = None
    window.import_wav(source, position=555)
    wait_job(app, window)
    generic = window.project.clip(window.selected_id)['track_id']
    assert generic not in (cj_track, smoke_track)
    assert window.project.track(generic)['name'] == 'Vozes PT-BR'
    snapshot = copy.deepcopy(window.project.data)
    window.project.update_track(generic, locked=True)
    window.import_wav(source, position=666)
    wait_job(app, window)
    assert window.project.clip(window.selected_id)['track_id'] != generic
    assert all(t['name'] != 'a' * 64 for t in window.project.data['tracks'])
    window.undo()
    assert len(window.project.data['clips']) == len(snapshot['clips'])
    assert len(window.project.data['sources']) == 1
    cleanup(window)


def test_layer_rows_scroll_together_and_keep_position_after_edit(app, tmp_path):
    window = TimelineWindow(tmp_path)
    for index in range(16):
        window.project.add_track('Voz %d' % index)
    window.refresh()
    window.show()
    app.processEvents()
    window.view.verticalScrollBar().setValue(300)
    app.processEvents()
    assert window.layer_scroll.verticalScrollBar().value() == 300
    window.project.update_track(window.project.data['tracks'][10]['id'], mute=True)
    window.refresh()
    app.processEvents()
    assert window.view.verticalScrollBar().value() == 300
    assert window.layer_scroll.verticalScrollBar().value() == 300
    blocks = window.layer_scroll.widget().layout()
    assert blocks.itemAt(1).widget().y() - blocks.itemAt(0).widget().y() == 84
    window.view.verticalScrollBar().setValue(window.view.verticalScrollBar().maximum())
    app.processEvents()
    assert window.view.verticalScrollBar().value() == window.view.verticalScrollBar().maximum()
    assert window.layer_scroll.verticalScrollBar().value() == window.view.verticalScrollBar().value()
    cleanup(window)


def test_empty_locked_and_deleted_selection_have_clear_available_actions(app, tmp_path):
    window = TimelineWindow(tmp_path)
    assert not window.listen_selected_button.isEnabled()
    assert not window.apply_button.isEnabled()
    assert not window.play_button.isEnabled()
    assert 'Abra' in window.scene_label.text()
    window.open_source(make_wav(tmp_path / 'original.wav'))
    wait_job(app, window)
    assert window.listen_selected_button.isEnabled()
    assert not window.apply_button.isEnabled()
    assert not window.split_button.isEnabled()
    assert not window.delete_button.isEnabled()
    assert 'Original' in window.clip_label.text()
    window.import_wav(tmp_path / 'original.wav', role='pt', position=12000)
    wait_job(app, window)
    assert window.apply_button.isEnabled()
    window.seek(16000)
    assert window.split_button.isEnabled()
    window.delete_selected()
    assert not window.apply_button.isEnabled()
    assert window.clip_text.toPlainText() == ''
    cleanup(window)


def test_basic_controls_are_concise_and_auxiliary_scene_lanes_are_compact(app, tmp_path):
    window = TimelineWindow(tmp_path)
    assert window.more_menu.actions()[0].text() == 'Arquivo externo…'
    assert window.more_menu.actions()[-1].text() == 'Nova camada PT'
    assert window.inspector_form.isRowVisible(window.take)
    assert not window.inspector_form.isRowVisible(window.reference)
    assert window.metadata.isHidden()
    window.project.add_track('Vozes já existentes')
    window.project.add_track('Referências originais por fala', role='original')
    window.project.add_track('Janelas de substituição', role='installed')
    window.project.add_track('Vozes PT-BR')
    window.refresh()
    window.show()
    QTest.qWait(1)
    app.processEvents()
    assert window._track_row_height(0) == 84
    assert window._track_row_height(1) < window._track_row_height(0)
    assert window._track_row_height(2) < window._track_row_height(0)
    assert window._track_row_height(3) == 84
    from PySide6.QtWidgets import QComboBox, QCheckBox, QDoubleSpinBox, QLabel
    layer_layout = window.layer_scroll.widget().layout()
    for row in (1, 2):
        block = layer_layout.itemAt(row).widget()
        row_layout = block.layout()
        assert block.height() == window._track_row_height(row) - 4
        assert row_layout.count() == 2
        name = row_layout.itemAt(0).widget()
        legend = row_layout.itemAt(1).widget()
        assert isinstance(name, QLabel) and isinstance(legend, QLabel)
        assert name.geometry().bottom() < legend.geometry().top()
        assert legend.geometry().bottom() <= block.contentsRect().bottom()
        assert not block.findChildren(QComboBox)
        assert not block.findChildren(QCheckBox)
        assert not block.findChildren(QDoubleSpinBox)
    for row in range(len(window.project.data['tracks'])):
        center = window._track_row_top(row) + window._track_row_height(row) / 2
        assert window._track_row_at(center) == row
    cleanup(window)


def test_selected_original_audition_is_audible_without_changing_mix_policy(app, tmp_path):
    from studio.audio.timeline_project import read_pcm
    window = TimelineWindow(tmp_path)
    window.open_source(make_wav(tmp_path / 'original.wav'))
    wait_job(app, window)
    snapshot = copy.deepcopy(window.project.data)
    destination = tmp_path / 'audition.wav'
    window.render_to(destination, selected=True)
    wait_job(app, window)
    data, rate = read_pcm(destination)
    assert np.max(np.abs(data)) > .1
    assert window.project.data == snapshot
    assert not window.comparison.isChecked()
    assert window.project.data['tracks'][0]['mute']
    window.render_to(tmp_path / 'mix.wav')
    wait_job(app, window)
    mixed, _ = read_pcm(tmp_path / 'mix.wav')
    assert not np.any(mixed)
    cleanup(window)
