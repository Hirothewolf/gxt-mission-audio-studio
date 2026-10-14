"""Selected original frames reach the immutable request without running TTS."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

from studio.ui_qt.context import QtContext
from studio.ui_qt.document import QtDocument
from studio.tts.omnivoice import sha256
from tests.test_qt_app import _fixture_gxt


def test_manual_original_span_is_exact_and_reused_for_audition_and_tts(tmp_path):
    source = tmp_path / 'original.wav'
    samples = np.linspace(-.3, .3, 8000, dtype=np.float32).reshape(-1, 1)
    sf.write(source, samples, 8000, subtype='FLOAT')
    game = tmp_path / 'game'
    (game / 'text').mkdir(parents=True)
    gxt = game / 'text/american.gxt'
    gxt.write_bytes(_fixture_gxt([(42, b'Hello original')]))
    script = tmp_path / 'never-run.py'
    script.touch()
    doc = QtDocument()
    doc.load(gxt)
    context = QtContext.__new__(QtContext)
    context.project_root, context.document = tmp_path, doc
    context.game_audio = SimpleNamespace(game_dir=game)
    context._base_signature = None
    info = dict(pak='CUTSCENE', stream={'track': 'Track_001'},
                stable_audio_id='CUTSCENE:MAIN:KEY', gxt_stable_id='MAIN:0000002A')
    context.info = lambda r: info
    record = dict(table='MAIN', hash=42, line_id='MAIN|0000002a',
                  reference_override=dict(source_path=str(source), source_sha256=sha256(source), start_frame=1000, end_frame=5000))
    engine = SimpleNamespace(id='omnivoice', python=Path(sys.executable), script=script)
    with patch('studio.tts.registry.select_engine', return_value=engine):
        request = context.generation_request(record, 'Olá personagem', duration='original', seed=9)
    payload = json.loads(request['request'].read_text(encoding='utf-8'))
    reference = Path(payload['reference_path'])
    cut, rate = sf.read(reference, dtype='float32', always_2d=True)
    assert rate == 8000
    assert np.array_equal(cut, samples[1000:5000])
    assert context.original_path(record) == reference
    assert payload['duration'] == .5
    assert payload['reference_text'] == 'Hello original'
    assert payload['provenance']['reference_start_frame'] == 1000
    assert payload['provenance']['reference_end_frame'] == 5000
    assert payload['provenance']['reference_asr_verification'] == 'PENDING'


def test_whole_scene_and_pt_take_use_the_linked_original_utterance(tmp_path):
    from studio.audio.timeline_project import TimelineProject
    from studio.ui_qt.scene import selected_original_reference
    source = tmp_path / 'original.wav'
    pt = tmp_path / 'dub.wav'
    sf.write(source, np.ones(8000, dtype=np.float32)*.2, 8000)
    sf.write(pt, np.ones(800, dtype=np.float32)*.1, 8000)
    project = TimelineProject()
    original = project.add_wav(source, role='original', native_format=True)
    lane = project.add_track('Referência', 'original')
    project.update_track(lane, locked=False)
    speech = project.duplicate(original, 1000, lane)
    project.update_clip(speech, source_in=1000, source_out=3000,
                        metadata=dict(table='MAIN', gxt_hash=42, window_role='reference'))
    dub = project.add_wav(pt, role='pt', position=1000)
    project.data['scene'] = dict(original_path=str(source), original_sha256=sha256(source))
    for selected in (original, dub):
        ref = selected_original_reference(project, selected, (0, 0), 'MAIN', 42)
        assert (ref['start_frame'], ref['end_frame']) == (1000, 3000)
    ratio = project.sample_rate / 8000
    chosen = selected_original_reference(project, original, (round(1500*ratio), round(2500*ratio)), 'MAIN', 42)
    assert (chosen['start_frame'], chosen['end_frame']) == (1500, 2500)
