"""Physical NPC editing and review, independent of any GXT key or caption."""
import copy
import json
import threading
import uuid
from pathlib import Path

from studio.core.utils import json_atomico
from studio.tts.omnivoice import build_command, sha256


class NpcContext:
    def __init__(self, base_context, catalogue):
        self.base_context = base_context
        self.project_root = Path(base_context.project_root).resolve()
        self.catalogue = catalogue
        self._records = {r['stable_audio_id']: r for r in catalogue.npc_records}
        for name in ('review_store', 'jobs', 'player', 'game_audio'):
            setattr(self, name, getattr(base_context, name))
        self._write_lock = threading.RLock()
        self.path = self.project_root / 'workspace/npc_editor.json'
        self.data = {'version': 1, 'scope': 'NPC', 'lines': {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if (self.data.get('version') != 1 or self.data.get('scope') != 'NPC' or
                    not isinstance(self.data.get('lines'), dict) or
                    any(k not in self._records or not isinstance(v, dict) or not isinstance(v.get('text'), str)
                        for k, v in self.data['lines'].items())):
                raise ValueError('Workspace NPC inválido; arquivo preservado.')
        self.stale_edits = {}
        self.catalog_changed_edits = set()
        self._refresh_edit_bindings()

    def _refresh_edit_bindings(self):
        """Loading a draft never rewrites or silently rebinds its original source."""
        self.stale_edits.clear()
        self.catalog_changed_edits.clear()
        for sid, entry in self.data['lines'].items():
            row = self._records[sid]
            if (not entry.get('source_sha256') or not entry.get('catalog_sha256') or
                    entry.get('physical_id', sid) != sid):
                self.stale_edits[sid] = 'Edição legada sem vínculo completo ao original; salve explicitamente para vincular.'
            elif entry['source_sha256'] != row['source_sha256']:
                self.stale_edits[sid] = 'O original deste ID físico mudou; revise e salve explicitamente para vincular.'
            elif entry['catalog_sha256'] != self.catalogue.summary['catalog_sha256']:
                self.catalog_changed_edits.add(sid)

    def _require_current_edit_binding(self, record):
        row = self._record(record)
        self._refresh_edit_bindings()
        reason = self.stale_edits.get(row['stable_audio_id'])
        if reason:
            raise ValueError(reason)
        return row

    def _record(self, record):
        sid = record.get('stable_audio_id')
        row = self._records.get(sid)
        if not row or record.get('line_id', 'NPC|' + sid) != 'NPC|' + sid:
            raise ValueError('ID físico NPC desconhecido ou conflitante.')
        return row

    def current_text(self, record):
        row = self._record(record)
        return self.data['lines'].get(row['stable_audio_id'], {}).get('text', row.get('reference_text', ''))

    def save_text(self, record, text):
        row = self._record(record)
        if not isinstance(text, str):
            raise ValueError('Texto NPC inválido.')
        with self._write_lock:
            self.original_path(record)
            if sha256(self.catalogue.catalog_path) != self.catalogue.summary['catalog_sha256']:
                raise ValueError('Catálogo mudou desde a abertura; recarregue antes de vincular a edição.')
            self._refresh_edit_bindings()
            updated = copy.deepcopy(self.data)
            sid = row['stable_audio_id']
            previous = updated['lines'].get(sid, {})
            history = previous.get('previous_bindings', [])
            if sid in self.stale_edits:
                history = history + [{k: v for k, v in previous.items() if k != 'previous_bindings'}]
            updated['lines'][sid] = dict(text=text, physical_id=sid,
                source_sha256=row['source_sha256'], catalog_sha256=self.catalogue.summary['catalog_sha256'])
            if history:
                updated['lines'][sid]['previous_bindings'] = history
            self.path.parent.mkdir(parents=True, exist_ok=True)
            json_atomico(self.path, updated)
            self.data = updated
            self._refresh_edit_bindings()
        return text

    def info(self, record):
        row = dict(self._record(record))
        row.update(pak=row['archive'], bank_num=row['bank_index']+1, sound_num=row['sound_index']+1)
        return row

    def original_path(self, record):
        row = self._record(record)
        path = Path(row['source_audio']).resolve()
        if not path.is_relative_to(self.project_root) or not path.is_file() or sha256(path) != row['source_sha256']:
            raise ValueError('WAV original NPC ausente ou alterado; revalide o catálogo.')
        return path

    def selected_mod(self):
        return self.base_context.selected_mod()

    def audio_evidence(self, record, source='mod', take_path=None):
        if source == 'mod':
            from .audio_sync import installed_line_evidence
            return installed_line_evidence(self.info(record), self.project_root, self.game_audio,
                take_path=take_path, mod=self.selected_mod())
        if source == 'original':
            path = self.original_path(record)
        elif source == 'take':
            path = self.approved_path('NPC|' + self._record(record)['stable_audio_id'], self.current_text(record))
        else:
            raise ValueError('Fonte de áudio desconhecida.')
        return dict(path=path, state='available' if path else 'not_found', verified=bool(path),
                    status='verified' if path else 'unavailable', matches_take=None)

    def installed_path(self, record):
        """Resolve explicit audition, extracting only this physical slot if needed."""
        from .audio_sync import installed_line_evidence, extract_sfx_slot
        info = self.info(record)
        mod = self.selected_mod()
        evidence = installed_line_evidence(info, self.project_root, self.game_audio, mod=mod)
        if evidence.get('path'):
            return evidence['path']
        if evidence.get('state') == 'needs_extract':
            return extract_sfx_slot(info, self.project_root, self.game_audio, mod=mod)
        return None

    def approved_path(self, line_id, text):
        if not line_id.startswith('NPC|') or line_id[4:] not in self._records:
            raise ValueError('ID de revisão NPC inválido.')
        path = self.review_store.approved_path(line_id, text)
        if path:
            candidate = next((c for c in self.review_store.list_candidates(line_id, text)
                              if c['status'] == 'approved' and self.review_store.resolve_path(c['path']) == path), None)
            metadata = (candidate or {}).get('metadata', {})
            if metadata.get('engine') == 'OmniVoice':
                receipt = self.review_store.resolve_path(metadata.get('omnivoice_receipt', ''))
                if sha256(receipt) != metadata.get('omnivoice_receipt_sha256'):
                    raise ValueError('Receipt NPC mudou desde a aprovação.')
                self.review_store._validate_omnivoice_receipt(path, receipt, text, line_id=line_id)
        return path

    def chosen_path(self, line_id, text):
        return self.approved_path(line_id, text)

    def generation_request(self, record, text, speed=1.0, duration='livre', seed=None,
                           engine='OmniVoice', **options):
        from studio.tts.registry import select_engine
        row = self._require_current_edit_binding(record)
        if str(engine).lower() != 'omnivoice':
            raise ValueError('NPC físico requer OmniVoice com referência original exata.')
        source = self.original_path(record)
        provenance = dict(source_kind='verified_npc_catalog_original', scope='NPC',
            physical_id=row['stable_audio_id'], source_path=str(source), source_sha256=row['source_sha256'],
            catalog_path=str(self.catalogue.catalog_path), catalog_sha256=self.catalogue.summary['catalog_sha256'],
            reference_text=row.get('reference_text', '').strip(), reference_transcript_authority='VERIFIED_ASR_DRAFT',
            reference_asr_verification='ASR_DRAFT_HUMAN_REVIEW_PENDING',
            asr_language=row.get('asr_language'), asr_language_probability=row.get('asr_language_probability'),
            target_text_source='physical_NPC_editor_text', target_speaker_identity='UNKNOWN')
        selected = select_engine('OmniVoice', root=self.project_root)
        run = self.project_root / 'workspace/studio_runs' / uuid.uuid4().hex
        output = run / 'npc' / (row['stable_audio_id'].replace(':', '_') + '.wav')
        argv = build_command(self.project_root, source, provenance['reference_text'], text, output,
            provenance=provenance, speed=speed, duration=duration, seed=seed,
            window_seconds=row['duration_seconds'], python_path=selected.python, script_path=selected.script)
        return dict(engine='OmniVoice', argv=argv, request=Path(argv[-1]), run=run,
                    output=output, receipt=output.with_suffix('.omnivoice.json'))

    def install(self, records, progress=None):
        from studio.projects.bulk_audio import collect_ai_rows, install_rows
        contexts = []
        with self._write_lock:
            for record in records:
                row = self._require_current_edit_binding(record)
                text = self.current_text(record)
                if record.get('text') != text:
                    raise ValueError('Texto NPC mudou desde a seleção; atualize a lista.')
                self.original_path(record)
                self.approved_path('NPC|' + row['stable_audio_id'], text)
                contexts.append(dict(record, line_id='NPC|' + row['stable_audio_id'], text=text,
                    info=self.info(record), ref=(row['archive'], row['bank_index']+1, row['sound_index']+1),
                    label=row['stable_audio_id']))
            rows = collect_ai_rows(self.review_store, contexts)
            return install_rows(self.review_store, rows, self.game_audio, self.selected_mod(), progress)
