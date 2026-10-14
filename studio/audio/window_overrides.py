"""Manual, listening-based windows for stream (CUTSCENE/BEATS) lines.

Two clocks exist for every stream line and they are edited separately:

* ``reference``   – the English span cloned as voice reference (ASR clock).
* ``destination`` – the span replaced in the game track on install
                    (defaults to the native .cut caption window).

A manual entry is pinned to the original track SHA-256, so a changed source
silently invalidates nothing: lookups reject it with an explicit error.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from studio.core.utils import json_atomico

SCHEMA = 'gta-manual-stream-windows/1'
AUTHORITY = 'MANUAL_LISTENING_SELECTION'


def path_for(project_root):
    return Path(project_root) / 'workspace/stream_audio_catalog/manual_windows.json'


def load(project_root):
    path = path_for(project_root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA:
        raise ValueError('Arquivo de janelas manuais com formato desconhecido: %s' % path)
    return data.get('windows', {})


def get(project_root, gid):
    if not gid:
        return None
    return load(project_root).get(gid)


def _span(start, end, label):
    start, end = float(start), float(end)
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
        raise ValueError('Intervalo %s inválido: %.3f–%.3f s.' % (label, start, end))
    return {'start': round(start, 6), 'end': round(end, 6)}


def save(project_root, gid, *, container, track, source_sha256, reference=None, destination=None,
         reference_text=None):
    """Create/update the manual windows of one line. Unspecified parts are kept."""
    if not gid or ':' not in gid:
        raise ValueError('Fala sem identificador GXT estável.')
    if not container or not track or not source_sha256:
        raise ValueError('A janela manual precisa da faixa e da assinatura do original.')
    windows = load(project_root)
    entry = dict(windows.get(gid) or {})
    if entry and (entry.get('container'), entry.get('track'), entry.get('source_sha256')) != (container, track, source_sha256):
        entry = {}  # a different source invalidates previous manual spans
    entry.update(container=container, track=track, source_sha256=source_sha256, authority=AUTHORITY,
                 updated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    if reference is not None:
        entry['reference'] = _span(*reference, label='de referência')
        if reference_text is not None:
            text = str(reference_text).strip()
            if not text:
                raise ValueError('Informe a transcrição em inglês do trecho de referência.')
            entry['reference']['text'] = text
    if destination is not None:
        entry['destination'] = _span(*destination, label='de destino')
    if 'reference' not in entry and 'destination' not in entry:
        raise ValueError('Nada para gravar: escolha a janela de referência ou a de destino.')
    windows[gid] = entry
    json_atomico(path_for(project_root), {'schema': SCHEMA, 'windows': windows})
    return entry


def clear(project_root, gid, part=None):
    """Remove a manual part ('reference'/'destination') or the whole entry."""
    windows = load(project_root)
    if gid not in windows:
        return False
    if part:
        windows[gid].pop(part, None)
        if 'reference' not in windows[gid] and 'destination' not in windows[gid]:
            windows.pop(gid)
    else:
        windows.pop(gid)
    json_atomico(path_for(project_root), {'schema': SCHEMA, 'windows': windows})
    return True
