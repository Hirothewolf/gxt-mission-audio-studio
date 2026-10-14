"""Explicit access to reviewed native CUTSCENE reference crops."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def bank_path(project_root):
    return Path(project_root).resolve() / 'workspace' / 'studio_review' / 'native_reference_bank.json'


def _read_bank(project_root):
    path = bank_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Banco de referências nativas indisponível: %s' % exc)
    if not isinstance(payload, dict) or payload.get('schema') != 1:
        raise ValueError('Schema do banco de referências nativas não reconhecido.')
    actors = payload.get('actors')
    if not isinstance(actors, list):
        raise ValueError('Banco de referências nativas sem lista de personagens.')
    return path, payload, actors


def _validated_actor(actor, bank_file):
    if not isinstance(actor, dict):
        raise ValueError('Registro de personagem inválido no banco de referências.')
    actor_id = actor.get('actor_id')
    reference_path = actor.get('reference_path')
    reference_sha256 = actor.get('reference_sha256')
    reference_text = actor.get('reference_text')
    if not all(isinstance(value, str) and value.strip() for value in
               (actor_id, reference_path, reference_sha256, reference_text)):
        raise ValueError('Registro de referência sem personagem, arquivo, hash ou transcrição.')
    review = actor.get('voice_crop_review') or {}
    if (actor.get('reference_kind') != 'NATIVE_GAME_CROP' or
            review.get('suitable') is not True or review.get('single_speaker') is not True or
            review.get('starts_clipped') is not False or review.get('ends_clipped') is not False):
        raise ValueError('Referência recusada: a revisão de voz não a marcou como adequada.')
    if (actor.get('identity_status') != 'MODEL_AND_SCRIPT_CONTEXT_PROPOSAL_NOT_BIOMETRIC_CONFIRMATION' or
            not actor.get('source_native_path') or not actor.get('source_native_sha256') or
            not review.get('review_path') or not review.get('review_sha256')):
        raise ValueError('Proveniência ou limite de identidade ausente no banco de referências.')
    path = Path(reference_path).resolve()
    allowed = (bank_file.parent / 'native_references').resolve()
    try:
        path.relative_to(allowed)
    except ValueError:
        raise ValueError('Referência do banco fora da pasta nativa preservada do projeto.')
    return actor, path


def load_reference_bank(project_root):
    """Return explicitly selectable actors from the project's schema-1 bank."""
    bank_file, payload, actors = _read_bank(project_root)
    if payload.get('source_policy') != 'ROOT_VANILLA_GAME_ONLY_NO_SYNTHETIC_REFERENCES':
        raise ValueError('Política de origem do banco de referências não reconhecida.')
    result = []
    seen = set()
    for actor in actors:
        validated, _path = _validated_actor(actor, bank_file)
        actor_id = validated['actor_id'].strip()
        if actor_id in seen:
            raise ValueError('O banco contém actor_id duplicado: ' + actor_id)
        seen.add(actor_id)
        result.append({'actor_id': actor_id,
                       'reference_text': validated['reference_text'].strip(),
                       'reference_kind': validated['reference_kind']})
    return result


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_source(path_value, expected_hash, label):
    if not isinstance(path_value, str) or not path_value or not isinstance(expected_hash, str):
        raise ValueError('Proveniência incompleta da referência: ' + label)
    path = Path(path_value)
    if not path.is_file() or _digest(path) != expected_hash:
        raise ValueError('Hash da proveniência nativa mudou: ' + label)
    return str(path.resolve())


def reference_for_actor(project_root, actor_id):
    """Create a pinned owned reference only for the actor explicitly selected by the user."""
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError('Escolha explicitamente uma referência/personagem do banco.')
    bank_file, payload, actors = _read_bank(project_root)
    if payload.get('source_policy') != 'ROOT_VANILLA_GAME_ONLY_NO_SYNTHETIC_REFERENCES':
        raise ValueError('Política de origem do banco de referências não reconhecida.')
    actor = next((item for item in actors if isinstance(item, dict) and
                  item.get('actor_id') == actor_id), None)
    if actor is None:
        raise ValueError('A referência exige escolha explícita de um personagem do banco.')
    actor, path = _validated_actor(actor, bank_file)
    if not path.is_file() or _digest(path) != actor['reference_sha256']:
        raise ValueError('Hash do áudio de referência mudou desde a revisão.')
    source_path = _verify_source(actor.get('source_native_path'),
                                 actor.get('source_native_sha256'), 'áudio nativo')
    review = actor['voice_crop_review']
    review_path = _verify_source(review.get('review_path'), review.get('review_sha256'),
                                 'revisão do recorte')
    return {
        'path': str(path),
        'sha256': actor['reference_sha256'],
        'reference_transcript': actor['reference_text'].strip(),
        'actor_id': actor['actor_id'],
        'reference_kind': actor['reference_kind'],
        'provenance': {
            'bank_path': str(bank_file.resolve()),
            'bank_sha256': _digest(bank_file),
            'source_policy': payload.get('source_policy'),
            'source_native_path': source_path,
            'source_native_sha256': actor['source_native_sha256'],
            'review_path': review_path,
            'review_sha256': review['review_sha256'],
            'identity_status': actor.get('identity_status'),
            'bank_origin_path': payload.get('bank_origin_path'),
            'bank_origin_sha256': payload.get('bank_origin_sha256'),
        },
    }
