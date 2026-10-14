import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication

from studio.audio.timeline_project import TimelineProject, digest
from studio.ui_qt.scene_browser import SceneBrowserDialog, scene_catalog
from studio.ui_qt.scene_sources import full_track
from studio.ui_qt.scene_mix import render_project
from studio.ui_qt.timeline import TimelineWindow, _peaks


def test_picker_lists_native_scene_without_gxt_or_extraction(tmp_path):
    context = SimpleNamespace(project_root=tmp_path, game_audio=SimpleNamespace(game_dir=tmp_path),
                              document=SimpleNamespace(navigation=lambda: {}))
    with patch('studio.ui_qt.scene_browser.track_lookup', return_value={'INTRO2A': {'track': 'Track_083'}}), \
         patch('studio.ui_qt.scene_browser.load_current', return_value=None):
        rows = scene_catalog(context, [], lambda _: None, threading.Event())
    assert rows[0]['container'] == 'BEATS'
    assert any(row['label'] == 'INTRO2A' and not row['records'] for row in rows)
    app = QApplication.instance() or QApplication([])
    dialog = SceneBrowserDialog(context, [], autoload=False)
    dialog.populate(rows)
    dialog.search.setText('intro2a')
    item = next(dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden())
    dialog.tree.setCurrentItem(item)
    opened = []
    dialog.sceneRequested.connect(opened.append)
    dialog.open_button.click()
    assert opened[0]['track'] == 'Track_083'
    dialog.close()


def test_beats_subtitles_are_indexed_and_searchable_in_scene_picker(tmp_path):
    members = [('INTRO1', index) for index in range(24)]
    context = SimpleNamespace(project_root=tmp_path, game_audio=SimpleNamespace(game_dir=tmp_path),
                              document=SimpleNamespace(navigation=lambda: {
                                  'mission:prologue': {'section': 'Missões', 'label': 'Prólogo', 'members': members}}))
    records = [dict(table='INTRO1', hash=index, name='PRO@%d' % (24 + index), text='Subtitle line %d' % index,
                    info={'stable_audio_id': 'POLICE:PRO@%d' % (24 + index)})
               for index in range(24)]
    with patch('studio.ui_qt.scene_browser.track_lookup', return_value={}), \
         patch('studio.ui_qt.scene_browser.load_current', return_value=None):
        rows = scene_catalog(context, records, lambda _: None, threading.Event())
    row = next(row for row in rows if row['container'] == 'BEATS')
    assert row['track'] == 'Track_010'
    assert len(row['records']) == 24
    assert 'BEATS' in row['label']
    assert 'Prólogo' in row['missions']
    app = QApplication.instance() or QApplication([])
    dialog = SceneBrowserDialog(context, records, autoload=False)
    dialog.populate(rows)
    dialog.search.setText('Subtitle line 17')
    assert [i for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden()] == [0]
    dialog.search.setText('PRO@40')
    assert [i for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden()] == [0]
    dialog.close()


def test_cache_opens_without_export_and_detects_changed_audio(tmp_path):
    game = tmp_path / 'game'
    archive = game / 'audio/streams/BEATS'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'original bank')
    ga = SimpleNamespace(game_dir=game)
    context = SimpleNamespace(project_root=tmp_path, game_audio=ga)
    cache = tmp_path / '.cache/qt_full_streams' / digest(archive)
    track = cache / 'BEATS/Track_010.ogg'
    track.parent.mkdir(parents=True)
    track.write_bytes(b'fixture track')
    (cache / 'source.json').write_text(json.dumps(dict(archive_sha256=digest(archive), files={'Track_010': digest(track)})))
    assert full_track(context, 'BEATS', 'Track_010')['source_path'] == str(track)
    track.write_bytes(b'changed track')
    import pytest
    with pytest.raises(ValueError, match='cache'):
        full_track(context, 'BEATS', 'Track_010')


def test_replacement_clears_old_voice_and_preserves_background_outside_window(tmp_path):
    original, voice = tmp_path / 'original.wav', tmp_path / 'voice.wav'
    sf.write(original, np.ones(8000) * .1, 8000, subtype='PCM_16')
    sf.write(voice, np.ones(800) * .2, 8000, subtype='PCM_16')
    project = TimelineProject()
    base = project.add_wav(original, role='ambience', native_format=True, metadata={'scene_background': True})
    project.data['scene'] = dict(background_mode='installed', background_clip_id=base)
    dub = project.add_wav(voice, role='pt', position=2000, metadata={'replace_end': .4})
    unchanged = json.dumps(project.data, sort_keys=True)
    rendered = render_project(project)
    rendered.render(tmp_path / 'mix.wav')
    pcm, _ = sf.read(tmp_path / 'mix.wav')
    assert abs(pcm[100] - .1) < .001
    assert abs(pcm[2100] - .2) < .001  # no old .1 underneath the new .2
    assert pcm[3000] == 0  # old voice is removed through the destination end
    assert abs(pcm[4000] - .1) < .001
    assert json.dumps(project.data, sort_keys=True) == unchanged


def test_background_choice_undo_and_select_full_scene(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'base.wav'
    sf.write(source, np.ones(8000) * .1, 8000)
    window = TimelineWindow(tmp_path)
    cid = window.project.add_wav(source, role='original', native_format=True, metadata={'scene_background': True})
    window.project.data['scene'] = {'background_mode': 'silence'}
    window.refresh()
    window.background.setCurrentIndex(window.background.findData('original'))
    assert not window.project.track(window.project.clip(cid)['track_id'])['mute']
    window.select_full_button.click()
    assert window.selected_id == cid
    assert window.selection_frames == (0, 8000)
    window.undo()
    assert window.project.track(window.project.clip(cid)['track_id'])['mute']
    window.project._saved = json.loads(json.dumps(window.project.data))
    window.close()


def test_waveform_reuses_unchanged_sources_and_invalidates_changed_file(tmp_path):
    source = tmp_path / 'base.wav'
    sf.write(source, np.ones(8000) * .1, 8000)
    project = TimelineProject()
    cid = project.add_wav(source, native_format=True)
    import studio.ui_qt.timeline as timeline
    with patch.object(timeline, 'read_pcm', wraps=timeline.read_pcm) as read:
        first = _peaks(project)
        assert _peaks(project) == first
        assert read.call_count == 1
        sf.write(source, np.ones(8000) * .2, 8000)
        assert _peaks(project) != first
        assert read.call_count == 2


def test_scene_uses_native_stereo_rate_even_when_alignment_was_resampled(tmp_path):
    from studio.ui_qt.scene import prepare_scene, selected_original_reference
    native, alignment, installed = [tmp_path / name for name in ('native.wav', 'asr.wav', 'mod.wav')]
    sf.write(native, np.ones((32000, 2)) * .1, 32000)
    sf.write(alignment, np.ones(16000) * .1, 16000)
    sf.write(installed, np.ones((32000, 2)) * .2, 32000)
    original = dict(source_path=str(native), source_sha256=digest(native), archive_path=str(native), archive_sha256=digest(native))
    mod = dict(source_path=str(installed), source_sha256=digest(installed), archive_path=str(installed), archive_sha256=digest(installed))
    info = dict(pak='CUTSCENE', stream={'track': 'Track_001'}, gxt_stable_id='MAIN:0000002A', stable_audio_id='CUTSCENE:TEST:0')
    row = dict(table='MAIN', hash=42, name='LINE', text='fala', line_id='MAIN|0000002a', info=info)
    context = SimpleNamespace(project_root=tmp_path, approved_path=lambda *_: None, game_audio=SimpleNamespace(mod_ativo=lambda: 'test'))
    window = dict(source_path=str(alignment), source_sha256=digest(alignment), start=.2, end=.5, status='ALIGNMENT_CANDIDATE', timing_authority='saved')
    with patch('studio.ui_qt.scene_sources.full_track', side_effect=[original, mod]), \
         patch('studio.ui_qt.scene.lookup_window', return_value=window), \
         patch('studio.ui_qt.scene.destination_window', side_effect=ValueError('no destination')):
        result = prepare_scene(context, row, [row], lambda _: None, threading.Event())
    project = TimelineProject.load(result['path'])
    assert (project.sample_rate, project.data['channels']) == (32000, 2)
    clip = next(c for c in project.data['clips'] if c['metadata'].get('window_role') == 'reference')
    ref = selected_original_reference(project, clip['id'], (0, 0), 'MAIN', 42)
    assert (ref['start_frame'], ref['end_frame']) == (6400, 16000)
    assert ref['source_path'] == str(native)


def test_closing_scene_dialog_releases_parent_registry(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = TimelineWindow(tmp_path)
    finished = []
    window.finished.connect(finished.append)
    window.show()
    window.close()
    app.processEvents()
    assert len(finished) == 1
