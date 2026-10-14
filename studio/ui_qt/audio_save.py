"""Durable per-GXT audio choices, consumed only by an explicit Save action."""
import hashlib
from pathlib import Path

from studio.core.utils import json_atomico
from studio.projects.review import text_hash


class PendingAudio:
    def __init__(self, project_root):
        import json
        self.path = Path(project_root) / 'workspace/qt_pending_audio.json'
        self.data = {'version': 1, 'documents': {}}
        if self.path.is_file():
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if loaded.get('version') != 1 or not isinstance(loaded.get('documents'), dict):
                raise ValueError('Lista de áudios pendentes inválida; arquivo preservado.')
            self.data = loaded

    @staticmethod
    def key(path):
        return str(Path(path).resolve()).casefold()

    def choices(self, path):
        import copy
        return copy.deepcopy(self.data['documents'].get(self.key(path), {})) if path else {}

    def select(self, path, record, candidate):
        if candidate.get('text_hash') != text_hash(record['text']):
            raise ValueError('Este take foi gerado para outro texto. Escolha um take do texto atual.')
        import json
        fields = ('table', 'hash', 'name', 'label', 'text', 'line_id', 'info', 'ref', '_target_mod')
        clean = {key: record[key] for key in fields if key in record}
        def encode(value):
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, set):
                return sorted(value)
            raise TypeError('Metadado de áudio não serializável: ' + type(value).__name__)
        clean = json.loads(json.dumps(clean, default=encode))
        item = dict(record=clean, candidate_id=candidate['id'], sha256=candidate['sha256'])
        self.data['documents'].setdefault(self.key(path), {})[record['line_id']] = item
        json_atomico(self.path, self.data)

    def clear(self, path, line_id):
        self.data['documents'].get(self.key(path), {}).pop(line_id, None)
        json_atomico(self.path, self.data)

    def saved_as(self, before, after):
        if self.key(before) != self.key(after) and self.choices(before):
            self.data['documents'][self.key(after)] = self.choices(before)
            json_atomico(self.path, self.data)

    def complete(self, path, snapshot, installed):
        choices = self.data['documents'].get(self.key(path), {})
        for line_id in installed:
            if choices.get(line_id) == snapshot.get(line_id):
                choices.pop(line_id, None)
        json_atomico(self.path, self.data)


def apply_selected(context, choices, progress=None):
    """Validate exact selected IDs; never substitute another/latest take."""
    records, errors = [], []
    for line_id, choice in choices.items():
        record = dict(choice['record'])
        try:
            if record.get('_target_mod') and context.game_audio.mod_ativo() != record['_target_mod']:
                raise ValueError('O mod de destino mudou durante o salvamento; áudio existente mantido.')
            if context.current_text(record) != record['text']:
                raise ValueError('Texto alterado depois da escolha; áudio existente mantido.')
            candidate = context.review_store.validate_candidate_source(line_id, choice['candidate_id'])
            if candidate['sha256'] != choice['sha256']:
                raise ValueError('O take mudou depois da escolha; áudio existente mantido.')
            # Selecting a take is the user's approval, including timing differences.
            context.review_store.set_status(line_id, candidate['id'], 'approved', record['text'],
                duration_override=bool(candidate.get('metadata', {}).get('duration_override_required')))
            records.append(record)
        except (ValueError, OSError) as exc:
            errors.append(dict(line_id=line_id, reason=str(exc)))
    result = context.install(records, progress) if records else dict(installed=[], skipped=[], errors=[])
    result['errors'].extend(errors)
    return result
