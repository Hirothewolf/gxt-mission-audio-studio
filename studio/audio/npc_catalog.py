"""Read-only physical SPC catalog from a hash-verified completed ASR recovery.

ASR language, transcription and identity remain drafts. Catalog availability
does not approve a speaker, GXT link, translation or audio for the game.
"""
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class NpcCatalogError(ValueError):
    pass


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise NpcCatalogError('Não foi possível ler o catálogo NPC: ' + str(path)) from exc


def _inside(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise NpcCatalogError('Arquivo NPC fora do projeto: ' + str(path))
    return path


@dataclass
class NpcCatalog:
    records: tuple
    summary: dict
    catalog_path: Path
    recovery_report_path: Path

    @property
    def npc_records(self):
        return tuple(row for row in self.records if row['scope'] != 'PLAYER_ROUTE_SEPARATE')

    def get(self, stable_audio_id):
        return next((row for row in self.records if row['stable_audio_id'] == stable_audio_id), None)


def load_verified_catalog(project_root):
    root = Path(project_root).resolve()
    receipt = _json(root / 'reports/npc_whisper_recovery_v2.json')
    submission = _json(root / 'reports/npc_whisper_submission_v2.json')
    report_path = _inside(receipt['report'], root)
    report = _json(report_path)
    catalog_path = _inside(report['merged_catalog'], root)
    manifest_path = root / 'deliverables/npc_whisper_v1/npc_whisper_catalog.jsonl'
    try:
        if not report.get('transcription_complete') or report.get('pending') != 0 or report.get('invalid_records'):
            raise NpcCatalogError('Transcrição SPC incompleta ou inválida.')
        manifest_hash = _sha(manifest_path)
        if manifest_hash != report.get('input_manifest_sha256') or manifest_hash != submission.get('manifest_sha256'):
            raise NpcCatalogError('Manifesto SPC não corresponde ao job recuperado.')
        if _sha(catalog_path) != report.get('merged_sha256'):
            raise NpcCatalogError('Hash do catálogo SPC divergente.')
        if receipt.get('archive_sha256') != report.get('archive_sha256'):
            raise NpcCatalogError('Recibo da recuperação SPC divergente.')
        manifest = {r['stable_audio_id']: r for r in (json.loads(s) for s in manifest_path.read_text(encoding='utf-8').splitlines())}
        records, ids = [], set()
        for line in catalog_path.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            sid = row['stable_audio_id']
            if sid in ids or sid not in manifest:
                raise NpcCatalogError('ID SPC duplicado ou desconhecido: ' + sid)
            ids.add(sid)
            original = manifest[sid]
            for field in ('archive','bank_index','sound_index','source_sha256','input_path','duration_seconds','scope'):
                if row.get(field) != original.get(field):
                    raise NpcCatalogError('Origem SPC divergente: ' + sid + ' / ' + field)
            if row.get('status') not in ('ASR_DRAFT', 'NO_SPEECH_DETECTED'):
                raise NpcCatalogError('Estado SPC não concluído: ' + sid)
            if row.get('speaker') != 'UNKNOWN' or row.get('gxt_key') is not None:
                raise NpcCatalogError('Identidade SPC não autorizada: ' + sid)
            records.append(row)
        if ids != set(manifest) or len(records) != report.get('expected') or len(records) != report.get('verified_asr_records'):
            raise NpcCatalogError('Cobertura SPC incompleta.')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, NpcCatalogError):
            raise
        raise NpcCatalogError('Não foi possível verificar o catálogo SPC: ' + str(exc)) from exc
    non_en = sum(r['status']=='ASR_DRAFT' and r.get('asr_language')!='en' for r in records)
    low_speech = sum(r['status']=='ASR_DRAFT' and r.get('asr_language_probability',0)<.7 for r in records)
    summary = dict(catalog_complete=True, english_complete=non_en==0 and low_speech==0,
        total=len(records), pending=0, npc_count=sum(r['scope']!='PLAYER_ROUTE_SEPARATE' for r in records),
        player_count=sum(r['scope']=='PLAYER_ROUTE_SEPARATE' for r in records), non_english_speech=non_en,
        uncertain_language_speech=low_speech,
        low_language_confidence_all=sum(r.get('asr_language_probability',0)<.7 for r in records),
        counts=dict(Counter(r['status'] for r in records)), languages=dict(Counter(r.get('asr_language') for r in records)),
        scopes=dict(Counter(r['scope'] for r in records)), human_review_complete=False,
        manifest_sha256=manifest_hash, catalog_sha256=report['merged_sha256'], archive_sha256=report['archive_sha256'])
    return NpcCatalog(tuple(records), summary, catalog_path, report_path)
