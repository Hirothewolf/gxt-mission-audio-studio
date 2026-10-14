"""Non-destructive takes and voice references. No operations write into the GTA."""
import hashlib
import copy
import json
import math
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from studio.audio.wav import wav_info
from studio.audio.analyzer import analyze_wav
from studio.core.utils import json_atomico, sha256_arquivo


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def validate_wav(path):
    info = wav_info(path)
    if not info or info.get('dur', 0) <= 0 or info.get('taxa', 0) <= 0:
        raise ValueError('Áudio ausente, vazio ou WAV inválido.')
    if info['fmt'] not in (1, 3) or info['canais'] < 1:
        raise ValueError('Formato de WAV não suportado para revisão.')
    analysis = analyze_wav(path)
    if not analysis['valid']:
        raise ValueError('WAV inválido: %s' % analysis['error'])
    return info


class ReviewStore:
    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.directory = self.root / 'workspace' / 'studio_review'
        self.path = self.directory / 'review.json'
        self.lock = threading.RLock()
        self.data = {'version': 1, 'lines': {}, 'voices': {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(data, dict) or data.get('version') != 1 or
                        not isinstance(data.get('lines'), dict) or not isinstance(data.get('voices'), dict)):
                    raise ValueError('Esquema desconhecido.')
                self.data = data
                self._validate_records()
            except (OSError, ValueError) as exc:
                raise ValueError('Dados de revisão inválidos; arquivo preservado: %s' % self.path) from exc

    def _save(self):
        json_atomico(self.path, self.data)

    def _validate_records(self):
        for line in self.data['lines'].values():
            if not isinstance(line, dict) or not isinstance(line.get('candidates'), list) or not isinstance(line.get('character'), str):
                raise ValueError('Registro de fala inválido.')
            for candidate in line['candidates']:
                if not isinstance(candidate, dict) or any(not isinstance(candidate.get(k), str)
                        for k in ('id', 'path', 'text', 'text_hash', 'sha256', 'origin')):
                    raise ValueError('Registro de take inválido.')
                if candidate.get('status') not in ('pending', 'approved', 'rejected'):
                    raise ValueError('Estado do take inválido.')
                self.resolve_path(candidate['path'])
        for voice in self.data['voices'].values():
            if not isinstance(voice, dict) or not isinstance(voice.get('references'), list):
                raise ValueError('Perfil de voz inválido.')
            for reference in voice['references']:
                if not isinstance(reference, dict) or any(not isinstance(reference.get(k), str)
                        for k in ('id', 'path', 'name', 'transcript', 'emotion', 'sha256')):
                    raise ValueError('Referência de voz inválida.')
                if not isinstance(reference.get('duration'), (int, float)) or reference['duration'] <= 0:
                    raise ValueError('Duração da referência inválida.')
                self.resolve_path(reference['path'])

    def resolve_path(self, path):
        p = Path(path)
        resolved = (p if p.is_absolute() else self.root / p).resolve()
        try:
            resolved.relative_to(self.directory)
        except ValueError as exc:
            raise ValueError('Arquivo de revisão fora da pasta do Studio.') from exc
        return resolved

    def _relative(self, path):
        return str(Path(path).resolve().relative_to(self.root))

    def _line(self, line_id):
        return self.data['lines'].setdefault(line_id, {'character': '', 'candidates': [], 'approved': None})

    def _copy(self, source, folder):
        source = Path(source).resolve()
        validate_wav(source)
        candidate_id = uuid.uuid4().hex
        dest = self.directory / folder / (candidate_id + '.wav')
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return candidate_id, dest

    def import_candidate(self, line_id, source, text, origin, metadata=None, analysis=None):
        with self.lock:
            cid, dest = self._copy(source, 'takes')
            record = {'id': cid, 'path': self._relative(dest), 'text': text,
                      'text_hash': text_hash(text), 'origin': origin, 'status': 'pending',
                      'sha256': sha256_arquivo(dest), 'created_at': datetime.now(timezone.utc).isoformat(),
                      'metadata': metadata or {}, 'analysis': analysis or {}}
            line = self._line(line_id)
            line['candidates'].append(record)
            try:
                self._save()
            except Exception:
                line['candidates'].remove(record)
                dest.unlink(missing_ok=True)
                raise
            return dict(record)

    def import_omnivoice_candidate(self, line_id, source, text, receipt_path):
        """Register one validated OmniVoice result, including duration-only quarantine."""
        source = Path(source).resolve()
        receipt_path = Path(receipt_path).resolve()
        receipt = self._validate_omnivoice_receipt(
            source, receipt_path, text, line_id=line_id, require_output_path=True)
        if receipt['status'] == 'QUARANTINED_SILENCE':
            raise ValueError('Take OmniVoice em quarentena por silêncio; não pode ser revisado ou aceito.')
        analysis = analyze_wav(source, receipt['window_seconds'])
        if not analysis.get('valid'):
            raise ValueError('Take OmniVoice inválido: %s' % analysis.get('error'))
        if analysis.get('silence_ratio', 1.0) > .98:
            raise ValueError('Take OmniVoice em quarentena por silêncio; não pode ser revisado ou aceito.')
        if analysis.get('clipping_ratio', 1.0) > .01:
            raise ValueError('Take OmniVoice com clipping excessivo; não pode ser revisado ou aceito.')
        duration = float(receipt['duration_seconds'])
        window = float(receipt['window_seconds'])
        if (analysis['sample_rate'] != int(receipt['sample_rate']) or
                abs(analysis['duration'] - duration) > (1 / analysis['sample_rate'])):
            raise ValueError('A duração do WAV ou a taxa de amostragem não corresponde ao receipt OmniVoice.')
        overflow = duration > window + (1 / 24000)
        if overflow != (receipt['status'] == 'QUARANTINED_DURATION'):
            raise ValueError('Estado de quarentena OmniVoice não corresponde à duração recebida.')

        with self.lock:
            cid, dest = self._copy(source, 'takes')
            receipt_dest = dest.with_suffix('.omnivoice.json')
            try:
                shutil.copy2(receipt_path, receipt_dest)
                metadata = {
                    'engine': 'OmniVoice', 'omnivoice_receipt': self._relative(receipt_dest),
                    'omnivoice_receipt_sha256': sha256_arquivo(receipt_dest),
                    'worker_status': receipt['status'], 'duration_seconds': duration,
                    'window_seconds': window, 'duration_override_required': overflow,
                    'reference_path': receipt['reference_path'],
                    'reference_sha256': receipt['reference_sha256'],
                    'source_path': receipt['provenance']['source_path'],
                    'source_sha256': receipt['provenance']['source_sha256'],
                    'provenance': receipt['provenance'], 'seed': receipt.get('seed'),
                    'speed': receipt.get('speed'),
                }
                record = {
                    'id': cid, 'path': self._relative(dest), 'text': text,
                    'text_hash': text_hash(text), 'origin': 'OmniVoice', 'status': 'pending',
                    'sha256': sha256_arquivo(dest), 'created_at': datetime.now(timezone.utc).isoformat(),
                    'metadata': metadata, 'analysis': analysis,
                }
                line = self._line(line_id)
                line['candidates'].append(record)
                try:
                    self._save()
                except Exception:
                    line['candidates'].remove(record)
                    raise
            except Exception:
                dest.unlink(missing_ok=True)
                receipt_dest.unlink(missing_ok=True)
                raise
            return dict(record)

    def _validate_omnivoice_receipt(self, audio_path, receipt_path, text, line_id, require_output_path=False):
        try:
            data = json.loads(receipt_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Receipt OmniVoice ausente ou inválido; take não pode ser aceito.') from exc
        if not isinstance(data, dict) or data.get('schema') != 'omnivoice-single-take-result/1':
            raise ValueError('Receipt OmniVoice com esquema desconhecido; take não pode ser aceito.')
        if data.get('status') not in ('UNREVIEWED_WITHIN_WINDOW', 'QUARANTINED_DURATION', 'QUARANTINED_SILENCE'):
            raise ValueError('Estado do receipt OmniVoice não é revisável.')
        # Older requests stripped outer whitespace; spoken words and GTA tags must still match.
        if (data.get('engine') != 'OmniVoice' or not isinstance(data.get('target_text'), str)
                or data['target_text'].strip() != text.strip()):
            raise ValueError('Texto ou engine não corresponde ao receipt OmniVoice desta fala.')
        if require_output_path and Path(data.get('output_path', '')).resolve() != audio_path:
            raise ValueError('O receipt OmniVoice aponta para outro WAV.')
        if not audio_path.is_file() or sha256_arquivo(audio_path) != data.get('output_sha256'):
            raise ValueError('Hash do WAV OmniVoice não corresponde ao receipt.')
        try:
            duration = float(data['duration_seconds'])
            window = float(data['window_seconds'])
            sample_rate = int(data['sample_rate'])
            rms = float(data['rms'])
            clipping = float(data['clipping_ratio'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Métricas incompletas no receipt OmniVoice.') from exc
        if (not all(math.isfinite(v) for v in (duration, window, rms, clipping)) or
                duration <= 0 or window <= 0 or sample_rate <= 0 or rms <= 0 or
                not 0 <= clipping <= 1):
            raise ValueError('Duração ou métricas inválidas no receipt OmniVoice.')
        provenance = data.get('provenance')
        if not isinstance(provenance, dict) or provenance.get('source_kind') not in {
                'original_game_saat_cache', 'accepted_original_cutscene_utterance',
                'verified_native_cutscene_window', 'verified_npc_catalog_original'}:
            raise ValueError('Fonte original inválida no receipt OmniVoice.')
        table, separator, key = str(line_id).partition('|')
        if not separator:
            raise ValueError('Identidade da fala de revisão inválida.')
        if table == 'NPC':
            from studio.tts.omnivoice import validate_npc_provenance
            if (data.get('scope') != 'NPC' or data.get('physical_id') != key or
                    data.get('mission') or data.get('jamcrc') or data.get('target_gxt_id')):
                raise ValueError('Receipt NPC pertence a outro slot físico ou contém identidade GXT.')
            npc_row = validate_npc_provenance(provenance, data.get('reference_text'), key, self.root)
            if window != float(npc_row['duration_seconds']):
                raise ValueError('Janela do receipt NPC difere do original verificado.')
            if (Path(data.get('reference_path', '')).resolve() != Path(provenance['source_path']).resolve() or
                    data.get('reference_sha256') != provenance['source_sha256']):
                raise ValueError('Receipt NPC não usa o WAV original exato.')
            return data
        if provenance.get('source_kind') == 'verified_npc_catalog_original' or data.get('scope') == 'NPC':
            raise ValueError('Receipt NPC não pode ser anexado a uma chave GXT.')
        expected_gxt_id = '%s:%s' % (table.upper(), key.upper())
        identities = [data.get('target_gxt_id'), provenance.get('gxt_stable_id'),
                      provenance.get('base_english_gxt_id')]
        if data.get('mission') or data.get('jamcrc'):
            if not data.get('mission') or not data.get('jamcrc'):
                raise ValueError('O vínculo de missão/hash do receipt OmniVoice está incompleto.')
            identities.append('%s:%s' % (str(data['mission']).upper(), str(data['jamcrc']).upper()))
        normalized_ids = []
        for value in identities:
            if not value:
                continue
            prefix, sep, suffix = str(value).partition(':')
            if not sep or len(suffix) != 8:
                raise ValueError('Identidade GXT inválida no receipt OmniVoice.')
            normalized_ids.append('%s:%s' % (prefix.upper(), suffix.upper()))
        if not normalized_ids or any(value != expected_gxt_id for value in normalized_ids):
            raise ValueError('O receipt OmniVoice pertence a outra fala; não pode ser anexado a este texto.')
        source_path = Path(provenance.get('source_path') or '')
        if (not source_path.is_file() or not provenance.get('source_sha256') or
                sha256_arquivo(source_path) != provenance.get('source_sha256')):
            raise ValueError('Fonte original inválida ou alterada desde a geração.')
        reference_path = Path(data.get('reference_path') or '')
        if (not reference_path.is_file() or not data.get('reference_sha256') or
                sha256_arquivo(reference_path) != data.get('reference_sha256')):
            raise ValueError('Hash da referência original mudou desde a geração.')
        return data

    def list_candidates(self, line_id, text):
        with self.lock:
            return [dict(c, stale=c['text_hash'] != text_hash(text))
                    for c in self.data['lines'].get(line_id, {}).get('candidates', [])]

    def validate_candidate_source(self, line_id, candidate_id):
        with self.lock:
            candidates = self.data['lines'].get(line_id, {}).get('candidates', [])
            candidate = next((c for c in candidates if c['id'] == candidate_id), None)
            if not candidate:
                raise ValueError('Take não encontrado.')
            path = self.resolve_path(candidate['path'])
            validate_wav(path)
            if sha256_arquivo(path) != candidate['sha256']:
                raise ValueError('O WAV foi alterado externamente. Importe-o como novo take.')
            return dict(candidate)

    def set_status(self, line_id, candidate_id, status, current_text, duration_override=False):
        if status not in ('pending', 'approved', 'rejected'):
            raise ValueError('Estado de revisão inválido.')
        with self.lock:
            line = self._line(line_id)
            candidate = next((c for c in line['candidates'] if c['id'] == candidate_id), None)
            if not candidate:
                raise ValueError('Take não encontrado.')
            previous = copy.deepcopy(line)
            if status == 'approved':
                if candidate['text_hash'] != text_hash(current_text):
                    raise ValueError('Texto alterado: gere ou importe um novo take antes de aprovar.')
                p = self.resolve_path(candidate['path'])
                validate_wav(p)
                if sha256_arquivo(p) != candidate['sha256']:
                    raise ValueError('O WAV foi alterado externamente. Importe-o como novo take.')
                metadata = candidate.get('metadata') or {}
                if metadata.get('engine') != 'OmniVoice' and metadata.get('duration_override_required'):
                    if not duration_override:
                        raise ValueError('Diferença de duração: ouça e marque o aceite manual antes de aprovar.')
                    candidate['metadata']['duration_override_accepted'] = True
                if metadata.get('engine') == 'OmniVoice':
                    receipt_path = self.resolve_path(metadata.get('omnivoice_receipt', ''))
                    if sha256_arquivo(receipt_path) != metadata.get('omnivoice_receipt_sha256'):
                        raise ValueError('Receipt OmniVoice foi alterado; aceite bloqueado.')
                    receipt = self._validate_omnivoice_receipt(p, receipt_path, current_text, line_id=line_id)
                    analysis = analyze_wav(p, receipt['window_seconds'])
                    if (not analysis.get('valid') or analysis.get('silence_ratio', 1) > .98 or
                            analysis.get('clipping_ratio', 1) > .01):
                        raise ValueError('Take OmniVoice inválido, silencioso ou com clipping excessivo.')
                    if (analysis['sample_rate'] != int(receipt['sample_rate']) or
                            abs(analysis['duration'] - float(receipt['duration_seconds'])) >
                            (1 / analysis['sample_rate'])):
                        raise ValueError('A duração do WAV ou a taxa de amostragem não corresponde ao receipt OmniVoice.')
                    over_window = float(receipt['duration_seconds']) > float(receipt['window_seconds']) + (1 / 24000)
                    if over_window and not duration_override:
                        raise ValueError('Take excede a janela original; marque “aceitar mesmo excedendo a janela” para aprovar explicitamente.')
                    if duration_override and not over_window:
                        raise ValueError('Override de duração só vale para um take OmniVoice que excedeu a janela.')
                    if over_window and receipt.get('status') != 'QUARANTINED_DURATION':
                        raise ValueError('Receipt não comprova quarentena exclusivamente por duração.')
                    if not over_window and receipt.get('status') != 'UNREVIEWED_WITHIN_WINDOW':
                        raise ValueError('Receipt OmniVoice não autoriza aceitar este take.')
                    candidate['metadata']['duration_override_accepted'] = bool(duration_override)
                    candidate['metadata']['accepted_duration_seconds'] = float(receipt['duration_seconds'])
                    candidate['metadata']['accepted_window_seconds'] = float(receipt['window_seconds'])
                for c in line['candidates']:
                    if c['status'] == 'approved':
                        c['status'] = 'pending'
                line['approved'] = candidate_id
            elif line.get('approved') == candidate_id:
                line['approved'] = None
            candidate['status'] = status
            try:
                self._save()
            except Exception:
                self.data['lines'][line_id] = previous
                raise

    def approved_path(self, line_id, current_text):
        with self.lock:
            line = self.data['lines'].get(line_id, {})
            for c in line.get('candidates', []):
                if c['id'] == line.get('approved') and c['status'] == 'approved' and c['text_hash'] == text_hash(current_text):
                    p = self.resolve_path(c['path'])
                    try:
                        if p.is_file() and sha256_arquivo(p) == c['sha256']:
                            return p
                    except OSError:
                        pass
            return None

    def character(self, line_id):
        return self.data['lines'].get(line_id, {}).get('character', '')

    def assign_character(self, line_id, character):
        with self.lock:
            self._line(line_id)['character'] = character.strip()
            self._save()

    def characters(self):
        return sorted(set(self.data['voices']) | {r.get('character', '') for r in self.data['lines'].values()} - {''})

    def references(self, character):
        return [dict(r) for r in self.data['voices'].get(character, {}).get('references', [])]

    def add_reference(self, character, source, transcript, provenance=None, emotion='neutral'):
        character = character.strip()
        if not character:
            raise ValueError('Informe o personagem antes de guardar uma referência.')
        with self.lock:
            rid, dest = self._copy(source, 'references')
            created_voice = character not in self.data['voices']
            try:
                record = {'id': rid, 'name': Path(source).stem, 'path': self._relative(dest),
                          'transcript': transcript, 'emotion': emotion, 'provenance': provenance or {},
                          'sha256': sha256_arquivo(dest), 'duration': validate_wav(dest)['dur']}
                voice = self.data['voices'].setdefault(character, {'references': []})
                voice['references'].append(record)
                self._save()
            except Exception:
                voice = self.data['voices'].get(character)
                if voice is not None:
                    voice['references'] = [r for r in voice.get('references', []) if r.get('id') != rid]
                if created_voice:
                    self.data['voices'].pop(character, None)
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return dict(record)

    def remove_reference(self, character, reference_id):
        with self.lock:
            voice = self.data['voices'].get(character, {})
            previous_references = voice.get('references', [])
            voice['references'] = [r for r in voice.get('references', []) if r['id'] != reference_id]
            try:
                self._save()  # Files are preserved for recovery.
            except Exception:
                voice['references'] = previous_references
                raise
