"""Explicit bulk review of existing takes; synthesis and GXT writes are never performed."""
from collections import defaultdict
from pathlib import Path

from studio.core.utils import sha256_arquivo
from studio.projects.review import validate_wav
from studio.saat.audio import PAKS


def collect_ai_rows(store, contexts):
    rows = []
    for context in contexts:
        row = dict(context)
        candidates = store.list_candidates(row['line_id'], row['text'])
        approved = store.approved_path(row['line_id'], row['text'])
        current = [c for c in candidates if not c['stale'] and c['status'] != 'rejected']
        chosen = next((c for c in current if approved and
                       store.resolve_path(c['path']) == approved), None)
        chosen = chosen or (current[-1] if current else None)
        legacy = row.get('legacy_path')
        if chosen:
            path = store.resolve_path(chosen['path'])
            row.update(candidate_id=chosen['id'], path=str(path), sha256=chosen['sha256'],
                       state='APPROVED' if approved and path == approved else 'PENDING',
                       duration_override_required=bool(chosen.get('metadata', {}).get('duration_override_required')),
                       reason='Aprovado no projeto' if approved and path == approved else 'Aguardando confirmação')
        elif candidates:
            row.update(state='STALE' if any(c['stale'] for c in candidates) else 'REJECTED',
                       reason='Texto mudou; take preservado' if any(c['stale'] for c in candidates) else 'Takes recusados; preservados',
                       path='', candidate_id=None)
        elif legacy and Path(legacy).is_file():
            row.update(state='LEGACY', reason='AI existente sem vínculo de texto; requer confirmação explícita',
                       path=str(legacy), candidate_id=None, sha256=sha256_arquivo(legacy))
        else:
            continue
        rows.append(row)
    return rows


def approve_rows(store, rows, allow_legacy=False, duration_override=False, progress=None):
    result = {'approved': [], 'skipped': [], 'errors': []}
    for index, row in enumerate(rows, 1):
        line_id = row['line_id']
        try:
            if row['state'] not in ('APPROVED', 'PENDING', 'LEGACY'):
                result['skipped'].append({'line_id': line_id, 'reason': row['reason']})
                continue
            if row['state'] == 'LEGACY' and not allow_legacy:
                result['skipped'].append({'line_id': line_id, 'reason': 'Vínculo manual dos AI antigos não autorizado'})
                continue
            path = Path(row['path'])
            if sha256_arquivo(path) != row['sha256']:
                raise ValueError('O WAV mudou desde a listagem; atualize a lista.')
            validate_wav(path)
            cid = row.get('candidate_id')
            current_approved = store.approved_path(line_id, row['text'])
            if current_approved and row['state'] != 'APPROVED':
                if row['state'] == 'LEGACY' and sha256_arquivo(current_approved) == row['sha256']:
                    result['approved'].append({'line_id': line_id, 'already_approved': True})
                    continue
                if row['state'] == 'PENDING' and store.resolve_path(
                        store.validate_candidate_source(line_id, cid)['path']) == current_approved:
                    result['approved'].append({'line_id': line_id, 'candidate_id': cid, 'already_approved': True})
                    continue
                raise ValueError('Outra aprovação foi salva desde a listagem; atualize a lista para escolher.')
            if row['state'] == 'LEGACY':
                # Deliberate manual binding, never presented as verified generation provenance.
                candidate = store.import_candidate(line_id, path, row['text'], 'AI existente · aceite manual em lote',
                    metadata={'manual_text_binding': True, 'prior_text_unverified': True,
                              'original_file': str(path), 'original_sha256': row['sha256']})
                cid = candidate['id']
            if row['state'] != 'APPROVED':
                store.set_status(line_id, cid, 'approved', row['text'],
                                 duration_override=bool(duration_override and row.get('duration_override_required')))
            elif store.approved_path(line_id, row['text']) != path:
                raise ValueError('A aprovação mudou desde a listagem; atualize a lista.')
            result['approved'].append({'line_id': line_id, 'candidate_id': cid})
        except (OSError, ValueError) as exc:
            result['errors'].append({'line_id': line_id, 'reason': str(exc)})
        finally:
            if progress:
                progress(index, len(rows), line_id)
    return result


def install_rows(store, rows, game_audio, mod, progress=None):
    """Repack once per PAK from its current mod contents, preserving partial successes."""
    result = {'installed': [], 'skipped': [], 'errors': [], 'banks': []}
    by_slot = defaultdict(list)
    stream_rows = []
    from studio.audio.source_routing import is_stream_record
    for row in rows:
        if is_stream_record(row.get("info")):
            stream_rows.append(row)
            continue
        ref = row.get('ref')
        if not ref or len(ref) != 3 or ref[0] not in PAKS:
            result['skipped'].append({'line_id': row['line_id'],
                'reason': 'CUTSCENE/BEATS ou slot não identificado: requer montagem de faixa, não SFX'})
            continue
        path = store.approved_path(row['line_id'], row['text'])
        if not path:
            result['skipped'].append({'line_id': row['line_id'], 'reason': 'Sem take aprovado para o texto atual'})
            continue
        if str(path) != row.get('path') or sha256_arquivo(path) != row.get('sha256'):
            result['skipped'].append({'line_id': row['line_id'], 'reason': 'O take escolhido mudou; atualize a lista'})
            continue
        by_slot[tuple(ref)].append((row, path))
    groups = defaultdict(list)
    for ref, members in by_slot.items():
        if len({row['sha256'] for row, _ in members}) > 1:
            result['skipped'].extend({'line_id': row['line_id'], 'reason': 'Dois takes diferentes apontam para o mesmo slot'}
                                     for row, _ in members)
        else:
            groups[ref[0]].append((ref, members))
    for index, (pak, slots) in enumerate(sorted(groups.items()), 1):
        try:
            if progress:
                progress(index, len(groups), 'Reconstruindo e conferindo ' + pak)
            items = [dict(rotulo=members[0][0]['label'], ref=ref, dub=str(members[0][1]))
                     for ref, members in slots]
            plan = game_audio.planejar(items, mod)
            invalid = [p for p in plan if p['problemas']]
            if invalid:
                raise ValueError('; '.join(p['rotulo'] + ': ' + ', '.join(p['problemas']) for p in invalid))
            response = game_audio.instalar(plan, mod)
            result['banks'].append(dict(pak=pak, result=response))
            if not response.get('ok'):
                raise ValueError(response.get('motivo', 'O banco não foi aplicado'))
            result['installed'].extend(row['line_id'] for _, members in slots for row, _ in members)
        except (OSError, ValueError, RuntimeError) as exc:
            result['errors'].append({'bank': pak, 'reason': str(exc)})
    if stream_rows:
        from studio.audio.stream_install import install_stream_rows
        stream_result = install_stream_rows(store, stream_rows, game_audio, mod, progress)
        for key in ('installed', 'skipped', 'errors', 'banks'):
            result[key].extend(stream_result.get(key, []))
    return result
