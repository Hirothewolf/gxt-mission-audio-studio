"""Physical NPC adapter proof using temporary catalogs and an injected model only."""
import copy
import importlib.util
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
import soundfile as sf

from studio.audio.npc_catalog import load_verified_catalog
from studio.projects.review import ReviewStore
from studio.tts.omnivoice import build_command, sha256, validate_npc_provenance
from studio.ui_qt.npc_context import NpcContext


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / 'extracted/audio/sfx/SPC_EA/Bank_001/sound_001.wav'
    source.parent.mkdir(parents=True)
    samples = .1*np.sin(np.arange(24000)*2*np.pi*440/24000)
    sf.write(source, samples, 24000)
    row = dict(stable_audio_id='SPC_EA:B000:S000', archive='SPC_EA', bank_index=0, sound_index=0,
        source_audio=str(source), input_path='audio/SPC_EA/Bank_001/sound_001.wav', source_sha256=sha256(source),
        duration_seconds=1., scope='SPC_NPC_OR_UNCLASSIFIED', status='ASR_DRAFT', speaker='UNKNOWN',
        gxt_key=None, text='Hello there.', reference_text='Hello there.', asr_language='en', asr_language_probability=.99)
    report_dir = tmp_path/'reports'; report_dir.mkdir()
    manifest = tmp_path/'deliverables/npc_whisper_v1/npc_whisper_catalog.jsonl'
    manifest.parent.mkdir(parents=True)
    catalog = report_dir/'catalog.jsonl'
    for p in (manifest, catalog): p.write_text(json.dumps(row)+'\n', encoding='utf-8')
    report = dict(transcription_complete=True, pending=0, invalid_records=[], input_manifest_sha256=sha256(manifest),
        merged_sha256=sha256(catalog), merged_catalog=str(catalog), archive_sha256='b'*64, expected=1, verified_asr_records=1)
    (report_dir/'report.json').write_text(json.dumps(report))
    (report_dir/'npc_whisper_recovery_v2.json').write_text(json.dumps(dict(report=str(report_dir/'report.json'),archive_sha256='b'*64)))
    (report_dir/'npc_whisper_submission_v2.json').write_text(json.dumps(dict(manifest_sha256=sha256(manifest))))
    store = ReviewStore(tmp_path)
    base = SimpleNamespace(project_root=tmp_path, review_store=store, jobs=object(), player=object(), game_audio=Mock(),
        selected_mod=lambda:'Existing NPC Mod')
    context = NpcContext(base, load_verified_catalog(tmp_path))
    record = dict(row, line_id='NPC|'+row['stable_audio_id'], kind='npc')
    return context, record, samples


def request_for(context, record, text='Olá.'):
    source = context.original_path(record)
    prov = dict(source_kind='verified_npc_catalog_original', scope='NPC', physical_id=record['stable_audio_id'],
        source_path=str(source), source_sha256=record['source_sha256'], catalog_path=str(context.catalogue.catalog_path),
        catalog_sha256=context.catalogue.summary['catalog_sha256'], reference_text=record['reference_text'])
    argv = build_command(context.project_root, source, record['reference_text'], text, context.project_root/'run/take.wav',
        prov, python_path=sys.executable, script_path=Path(__file__).parents[1]/'tools/omnivoice_tts.py', window_seconds=1., seed=123)
    return json.loads(Path(argv[-1]).read_text(encoding='utf-8'))


def worker():
    spec = importlib.util.spec_from_file_location('npc_worker_fixture', Path(__file__).parents[1]/'tools/omnivoice_tts.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_edit_persists_physical_id_and_stales_review(fixture):
    context, record, _ = fixture
    context.save_text(record, 'Olá NPC.')
    restored = NpcContext(context.base_context, context.catalogue)
    assert restored.current_text(record) == 'Olá NPC.'
    data = json.loads(context.path.read_text(encoding='utf-8'))
    assert list(data['lines']) == ['SPC_EA:B000:S000']
    assert 'table' not in str(data)
    candidate = context.review_store.import_candidate(record['line_id'], context.original_path(record), 'Olá NPC.', 'manual')
    context.review_store.set_status(record['line_id'], candidate['id'], 'approved', 'Olá NPC.')
    assert context.approved_path(record['line_id'], 'Olá NPC.')
    context.save_text(record, 'Texto mudou.')
    assert context.approved_path(record['line_id'], context.current_text(record)) is None


def test_generation_request_uses_exact_catalog_original_without_gxt(fixture):
    context, record, _ = fixture
    runtime = SimpleNamespace(python=Path(sys.executable), script=Path(__file__).parents[1]/'tools/omnivoice_tts.py')
    with patch('studio.tts.registry.select_engine', return_value=runtime):
        result = context.generation_request(record, 'Olá NPC.', seed=123)
    request = json.loads(result['request'].read_text(encoding='utf-8'))
    assert request['reference_path'] == record['source_audio']
    assert request['reference_text'] == record['reference_text']
    assert request['physical_id'] == record['stable_audio_id']
    assert request['target_text'] == 'Olá NPC.'
    assert request['provenance']['target_speaker_identity'] == 'UNKNOWN'
    assert not any('gxt' in key for key in request['provenance'])
    with pytest.raises(ValueError): context.generation_request(record, 'Olá.', engine='Qwen')


def test_simulated_generation_import_approval_and_physical_receipt(fixture):
    context, record, samples = fixture
    request = request_for(context, record)
    assert request['scope'] == 'NPC' and request['physical_id'] == record['stable_audio_id']
    assert request['mission'] is None and request['jamcrc'] is None
    model = SimpleNamespace(create_voice_clone_prompt=Mock(return_value='prompt'), generate=Mock(return_value=[samples]))
    result = worker().generate(request, model=model, model_revision='fixture-only')
    assert result['scope'] == 'NPC' and result['target_gxt_id'] is None
    output = Path(result['output_path'])
    store = context.review_store
    candidate = store.import_omnivoice_candidate(record['line_id'], output, 'Olá.', output.with_suffix('.omnivoice.json'))
    store.set_status(record['line_id'], candidate['id'], 'approved', 'Olá.')
    assert context.approved_path(record['line_id'], 'Olá.')
    with pytest.raises(ValueError):
        store.import_omnivoice_candidate('NPC|SPC_EA:B001:S000', output, 'Olá.', output.with_suffix('.omnivoice.json'))
    with pytest.raises(ValueError):
        store.import_omnivoice_candidate('MAIN|00000001', output, 'Olá.', output.with_suffix('.omnivoice.json'))
    with pytest.raises(ValueError):
        store.import_omnivoice_candidate(record['line_id'], output, 'Texto diferente.', output.with_suffix('.omnivoice.json'))
    Path(record['source_audio']).write_bytes(b'changed original')
    with pytest.raises(ValueError): context.approved_path(record['line_id'], 'Olá.')


@pytest.mark.parametrize('field,value', [('reference_text','Invented transcript'),('physical_id','SPC_EA:B001:S000'),
                                       ('scope','GXT'),('mission','MAIN'), ('window_seconds', 50.)])
def test_worker_guards_run_before_model(fixture, field, value):
    context, record, _ = fixture
    request = request_for(context, record)
    request[field] = value
    model = Mock()
    with pytest.raises(ValueError): worker().generate(request, model=model)
    model.generate.assert_not_called()


def test_changed_catalog_and_original_are_rejected(fixture):
    context, record, _ = fixture
    request = request_for(context, record)
    context.catalogue.catalog_path.write_text('{}\n')
    with pytest.raises(ValueError):
        validate_npc_provenance(request['provenance'], record['reference_text'], record['stable_audio_id'], context.project_root)
    Path(record['source_audio']).write_bytes(b'changed')
    with pytest.raises(ValueError): context.original_path(record)


def test_install_routes_exact_physical_slot_and_existing_mod_without_writes(fixture):
    context, record, _ = fixture
    context.save_text(record, 'Olá.')
    record['text'] = 'Olá.'
    with patch('studio.projects.bulk_audio.collect_ai_rows', side_effect=lambda _store, rows:rows), \
         patch('studio.projects.bulk_audio.install_rows', return_value={'installed':[record['line_id']]}) as installer:
        context.install([record])
    args = installer.call_args.args
    assert args[1][0]['ref'] == ('SPC_EA',1,1)
    assert args[3] == 'Existing NPC Mod'
    record['text'] = 'Stale'
    with pytest.raises(ValueError): context.install([record])


def test_no_speech_and_player_scope_are_not_voice_references(fixture):
    context, record, _ = fixture
    request = request_for(context, record)
    for field, value in [('status', 'NO_SPEECH_DETECTED'), ('scope','PLAYER_ROUTE_SEPARATE')]:
        changed = copy.deepcopy(context.catalogue)
        changed.records[0][field] = value
        with patch('studio.audio.npc_catalog.load_verified_catalog', return_value=changed):
            with pytest.raises(ValueError):
                validate_npc_provenance(request['provenance'], record['reference_text'], record['stable_audio_id'], context.project_root)


def refresh_fixture_catalog(context, record, change_source=False):
    row = {k:v for k,v in record.items() if k not in ('line_id','kind')}
    if change_source:
        sf.write(row['source_audio'], .1*np.sin(np.arange(24000)*2*np.pi*880/24000), 24000)
        row['source_sha256'] = sha256(row['source_audio'])
    else:
        # An unrelated ASR confidence correction changes the catalog, not this original.
        row['asr_language_probability'] = .98
    root = context.project_root
    catalog = context.catalogue.catalog_path
    catalog.write_text(json.dumps(row)+'\n', encoding='utf-8')
    manifest = root/'deliverables/npc_whisper_v1/npc_whisper_catalog.jsonl'
    if change_source:
        manifest.write_text(json.dumps(row)+'\n', encoding='utf-8')
    report_path = root/'reports/report.json'
    report = json.loads(report_path.read_text())
    report.update(merged_sha256=sha256(catalog), input_manifest_sha256=sha256(manifest))
    report_path.write_text(json.dumps(report))
    (root/'reports/npc_whisper_submission_v2.json').write_text(json.dumps(dict(manifest_sha256=sha256(manifest))))
    return load_verified_catalog(root)


def test_source_replacement_preserves_stale_draft_until_explicit_rebind(fixture):
    context, record, _ = fixture
    context.save_text(record, 'Minha edição antiga.')
    old_bytes = context.path.read_bytes()
    old_pin = context.data['lines'][record['stable_audio_id']]['source_sha256']
    catalogue = refresh_fixture_catalog(context, record, change_source=True)
    restored = NpcContext(context.base_context, catalogue)
    assert restored.current_text(record) == 'Minha edição antiga.'
    assert restored.path.read_bytes() == old_bytes
    assert record['stable_audio_id'] in restored.stale_edits
    with pytest.raises(ValueError, match='original.*mudou'):
        restored.generation_request(record, 'Minha edição antiga.')
    with pytest.raises(ValueError, match='original.*mudou'):
        restored.install([dict(record,text='Minha edição antiga.')])
    restored.save_text(record, 'Minha edição revisada.')
    assert record['stable_audio_id'] not in restored.stale_edits
    saved = restored.data['lines'][record['stable_audio_id']]
    assert saved['source_sha256'] == catalogue.records[0]['source_sha256']
    assert saved['previous_bindings'][0]['text'] == 'Minha edição antiga.'
    assert saved['previous_bindings'][0]['source_sha256'] == old_pin
    runtime = SimpleNamespace(python=Path(sys.executable), script=Path(__file__).parents[1]/'tools/omnivoice_tts.py')
    with patch('studio.tts.registry.select_engine', return_value=runtime):
        assert restored.generation_request(record, 'Minha edição revisada.', seed=123)['request'].is_file()


def test_catalog_hash_change_keeps_original_bound_draft_usable(fixture):
    context, record, _ = fixture
    context.save_text(record, 'Minha edição.')
    old_bytes = context.path.read_bytes()
    catalogue = refresh_fixture_catalog(context, record)
    restored = NpcContext(context.base_context, catalogue)
    assert not restored.stale_edits
    assert record['stable_audio_id'] in restored.catalog_changed_edits
    assert restored.current_text(record) == 'Minha edição.'
    assert restored.path.read_bytes() == old_bytes
    runtime = SimpleNamespace(python=Path(sys.executable), script=Path(__file__).parents[1]/'tools/omnivoice_tts.py')
    with patch('studio.tts.registry.select_engine', return_value=runtime):
        assert restored.generation_request(record, 'Minha edição.', seed=123)['request'].is_file()


@pytest.mark.parametrize('missing_pin', ['source_sha256', 'catalog_sha256'])
def test_legacy_edit_missing_pin_is_preserved_and_blocked(fixture, missing_pin):
    context, record, _ = fixture
    context.save_text(record, 'Edição legada.')
    data = json.loads(context.path.read_text(encoding='utf-8'))
    data['lines'][record['stable_audio_id']].pop(missing_pin)
    context.path.write_text(json.dumps(data), encoding='utf-8')
    old_bytes = context.path.read_bytes()
    restored = NpcContext(context.base_context, context.catalogue)
    assert restored.current_text(record) == 'Edição legada.'
    assert restored.path.read_bytes() == old_bytes
    assert record['stable_audio_id'] in restored.stale_edits
    with pytest.raises(ValueError, match='legada'):
        restored.generation_request(record, 'Edição legada.')
    restored.save_text(record, 'Edição legada revisada.')
    assert not restored.stale_edits


def test_embedded_mod_audition_extracts_only_requested_slot_without_approval(fixture):
    context, record, _ = fixture
    # Scratch packed data: a prefix and the exact PCM span of one physical slot.
    bank = context.project_root/'scratch/std.bank'
    bank.parent.mkdir()
    pcm = np.array([0, 1000, -1000, 500], dtype='<i2').tobytes()
    bank.write_bytes(b'header'+pcm+b'untouched adjacent slot')
    slot = dict(path=bank, offset=6, size=len(pcm), rate=24000, ref=('SPC_EA',1,1))
    before = bank.read_bytes()
    with patch('studio.ui_qt.audio_sync.installed_line_evidence', return_value={'path':None,'state':'needs_extract'}), \
         patch('studio.ui_qt.audio_sync.sfx_slot', return_value=slot) as locator:
        path = context.installed_path(record)
    assert path.is_file() and path.is_relative_to(context.project_root/'.cache/qt_audio_slots')
    with wave.open(str(path),'rb') as audio:
        assert audio.getframerate() == 24000
        assert audio.readframes(audio.getnframes()) == pcm
    assert bank.read_bytes() == before
    args = locator.call_args.args
    assert (args[0]['archive'],args[0]['bank_num'],args[0]['sound_num']) == ('SPC_EA',1,1)
    assert args[2] == 'Existing NPC Mod'
    context.game_audio.carregar_indice.assert_not_called()
    assert context.review_store.data['lines'] == {}


def test_cached_mod_audition_reuses_verified_path_without_extraction(fixture):
    context, record, _ = fixture
    cached = context.original_path(record)
    with patch('studio.ui_qt.audio_sync.installed_line_evidence', return_value={'path':cached,'state':'available'}) as evidence, \
         patch('studio.ui_qt.audio_sync.extract_sfx_slot') as extractor:
        assert context.installed_path(record) == cached
    assert evidence.call_args.kwargs['mod'] == 'Existing NPC Mod'
    extractor.assert_not_called()
    assert context.review_store.data['lines'] == {}


def test_absent_mod_audition_does_not_extract_or_claim_audio(fixture):
    context, record, _ = fixture
    with patch('studio.ui_qt.audio_sync.installed_line_evidence', return_value={'path':None,'state':'not_found'}), \
         patch('studio.ui_qt.audio_sync.extract_sfx_slot') as extractor:
        assert context.installed_path(record) is None
    extractor.assert_not_called()
    assert context.review_store.data['lines'] == {}
