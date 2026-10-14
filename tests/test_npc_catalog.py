import hashlib
import json
from pathlib import Path

import pytest

from studio.audio.npc_catalog import NpcCatalogError, load_verified_catalog


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def catalog_root(tmp_path):
    (tmp_path/'reports').mkdir()
    (tmp_path/'deliverables/npc_whisper_v1').mkdir(parents=True)
    records = []
    for archive, scope, language in [('SPC_GA','SPC_NPC_OR_UNCLASSIFIED','en'), ('SPC_PA','PLAYER_ROUTE_SEPARATE','es')]:
        records.append(dict(stable_audio_id=archive+':B000:S000',archive=archive,bank_index=0,sound_index=0,
            source_sha256='a'*64,input_path='audio/'+archive+'/Bank_001/sound_001.wav',duration_seconds=1.,
            scope=scope,status='ASR_DRAFT',speaker='UNKNOWN',gxt_key=None,text='hi',asr_language=language,asr_language_probability=.9))
    manifest = tmp_path/'deliverables/npc_whisper_v1/npc_whisper_catalog.jsonl'
    merged = tmp_path/'reports/catalog.jsonl'
    for path in (manifest,merged):
        path.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
    report_path = tmp_path/'reports/report.json'
    report = dict(transcription_complete=True,pending=0,invalid_records=[],input_manifest_sha256=digest(manifest),merged_sha256=digest(merged),
        merged_catalog=str(merged),archive_sha256='b'*64,expected=2,verified_asr_records=2)
    report_path.write_text(json.dumps(report))
    (tmp_path/'reports/npc_whisper_submission_v2.json').write_text(json.dumps(dict(manifest_sha256=digest(manifest))))
    (tmp_path/'reports/npc_whisper_recovery_v2.json').write_text(json.dumps(dict(report=str(report_path),archive_sha256='b'*64)))
    return tmp_path


def test_complete_catalog_preserves_language_and_separates_cj(catalog_root):
    catalog = load_verified_catalog(catalog_root)
    assert catalog.summary['catalog_complete'] is True
    assert catalog.summary['english_complete'] is False
    assert len(catalog.npc_records)==1
    assert catalog.get('SPC_PA:B000:S000')['asr_language']=='es'
    assert catalog.get('missing') is None


def test_changed_catalog_is_rejected(catalog_root):
    path=catalog_root/'reports/catalog.jsonl'
    path.write_text(path.read_text().replace('"text": "hi"','"text": "changed"'))
    with pytest.raises(NpcCatalogError,match='Hash'):
        load_verified_catalog(catalog_root)


def test_partial_recovery_is_rejected(catalog_root):
    path=catalog_root/'reports/report.json'
    report=json.loads(path.read_text());report['pending']=1
    path.write_text(json.dumps(report))
    with pytest.raises(NpcCatalogError,match='incompleta'):
        load_verified_catalog(catalog_root)


def test_duplicate_physical_id_is_rejected_even_with_recomputed_hash(catalog_root):
    path=catalog_root/'reports/catalog.jsonl'
    first=path.read_text().splitlines()[0]
    path.write_text(first+'\n'+first+'\n')
    report_path=catalog_root/'reports/report.json'
    report=json.loads(report_path.read_text());report['merged_sha256']=digest(path)
    report_path.write_text(json.dumps(report))
    with pytest.raises(NpcCatalogError,match='duplicado'):
        load_verified_catalog(catalog_root)
