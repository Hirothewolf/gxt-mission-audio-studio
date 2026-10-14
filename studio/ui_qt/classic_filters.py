"""Classic text filters with explicit, cached audio evidence for Qt lists."""
import re

from studio.gxt.lint import lint_avisos
from studio.gxt.codec import decode_text
from studio.projects.review import text_hash

FILTERS = (
    'Todas', 'Diferentes da comparação', 'Iguais à comparação',
    'Editadas (não salvas)', 'Só no arquivo editado', 'Com aviso de lint',
    'Com áudio original', 'Sem áudio original', 'Com dublagem',
    'Sem dublagem (tem original)', 'Favoritas ★', 'Com nota', 'Revisadas',
    'Não revisadas', 'Instalada no ModLoader', 'Dublada, ainda não instalada',
    'Diferentes', 'Editadas', 'Com áudio', 'Sem áudio', 'Com take', 'Favoritas',
    'Avisos de texto',
)
_ALIASES = {
    'Diferentes': 'Diferentes da comparação', 'Editadas': 'Editadas (não salvas)',
    'Com áudio': 'Com áudio original', 'Sem áudio': 'Sem áudio original',
    'Favoritas': 'Favoritas ★', 'Avisos de texto': 'Com aviso de lint',
}
_TAGS = re.compile(r'~[^~]*~')


def normalized_text(text, ignore_tags=False):
    if not ignore_tags:
        return text
    return ' '.join(_TAGS.sub(' ', text).split())


def prepare_filter_snapshot(window):
    """Verify approved takes once per refresh, never during row predicates.

    Installation evidence is supplied by the installation verifier as a mapping
    of line identities to True/False. Missing evidence remains unknown. Merely
    approving a take or finding a whole SCRIPT bank is not per-slot proof.
    """
    store = window.context.review_store
    verified = {}
    cache = getattr(window, '_filter_approved_cache', {})
    next_cache = {}
    for line_id, state in store.data.get('lines', {}).items():
        approved = state.get('approved')
        candidate = next((item for item in state.get('candidates', [])
            if item.get('id') == approved and item.get('status') == 'approved'), None)
        if not candidate or not hasattr(store, 'resolve_path'):
            continue
        try:
            table, sep, raw_hash = line_id.rpartition('|')
            key_hash = int(raw_hash, 16)
            text = decode_text(window.document.gxt.t[table]['cur'][key_hash])
            path = store.resolve_path(candidate['path'])
            stat = path.stat()
            signature = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                         candidate.get('sha256'), candidate.get('text_hash'), approved,
                         text_hash(text))
        except (KeyError, OSError, ValueError, AttributeError):
            continue
        previous = cache.get(line_id)
        if previous and previous[0] == signature:
            valid = previous[1]
        else:
            valid = bool(store.approved_path(line_id, text)) if sep else False
        next_cache[line_id] = (signature, valid)
        if valid:
            verified[line_id] = candidate.get('text_hash')
    window._filter_approved_cache = next_cache
    window._filter_snapshot = {
        'approved': verified,
        'installation': dict(getattr(window.context, 'filter_installation_evidence', {})),
    }
    return window._filter_snapshot


def row_flags(window, row):
    """Compute row flags without filesystem access or audio scanning."""
    flags = set(row.get('flags', ()))
    for flag in ('different', 'dif', 'equal', 'igual', 'solo', 'lint', 'dub',
                 'inst', 'not_installed', 'installation_unknown'):
        flags.discard(flag)
    if 'edited' in flags or 'edit' in flags:
        flags.update(('edited', 'edit'))
    option = getattr(window, 'ignore_tags', None)
    ignore_tags = bool(option.isChecked()) if option is not None else False
    comparison = row.get('comparison')
    if comparison is None:
        flags.add('solo')
    elif normalized_text(row['text'], ignore_tags) == normalized_text(comparison, ignore_tags):
        flags.update(('equal', 'igual'))
    else:
        flags.update(('different', 'dif'))
    if lint_avisos(row['text'], comparison):
        flags.add('lint')
    record = row if 'line_id' in row and 'info' in row else window.enrich(row)
    if record.get('info'):
        flags.add('audio')
    line_id = record['line_id']
    state = window.context.review_store.data.get('lines', {}).get(line_id, {})
    if state.get('candidates'):
        flags.add('take')
    notes = window.workspace.get(line_id)
    for name in ('fav', 'rev', 'note'):
        if notes.get(name):
            flags.add(name)
        else:
            flags.discard(name)
    snapshot = getattr(window, '_filter_snapshot', {})
    if snapshot.get('approved', {}).get(line_id) == text_hash(row['text']):
        flags.add('dub')
    installation = snapshot.get('installation', {}).get(line_id)
    if installation is True:
        flags.add('inst')
    elif installation is False:
        flags.add('not_installed')
    else:
        flags.add('installation_unknown')
    return flags


def accepts(filter_name, row_or_flags):
    flags = set(row_or_flags.get('flags', ())) if isinstance(row_or_flags, dict) else set(row_or_flags)
    name = _ALIASES.get(filter_name, filter_name)
    audio = bool({'audio', 'stream_link'} & flags)
    predicates = {
        'Todas': True,
        'Diferentes da comparação': bool({'different', 'dif'} & flags),
        'Iguais à comparação': bool({'equal', 'igual'} & flags),
        'Editadas (não salvas)': bool({'edited', 'edit'} & flags),
        'Só no arquivo editado': 'solo' in flags,
        'Com aviso de lint': 'lint' in flags,
        'Com áudio original': audio, 'Sem áudio original': not audio,
        'Com dublagem': 'dub' in flags,
        'Sem dublagem (tem original)': audio and 'dub' not in flags,
        'Favoritas ★': 'fav' in flags, 'Com nota': 'note' in flags,
        'Revisadas': 'rev' in flags, 'Não revisadas': 'rev' not in flags,
        'Instalada no ModLoader': 'inst' in flags,
        'Dublada, ainda não instalada': 'dub' in flags and 'not_installed' in flags,
        'Com take': 'take' in flags,
    }
    return predicates.get(name, True)
