"""Offline, evidence-backed speaker lookup for GTA San Andreas dialogue."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "speakers" / "speaker_database.json"
_TAGS = re.compile(r"~[^~\r\n]+~")
_SPACE = re.compile(r"\s+")
_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_PUNCT = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
    "\u00ab": '"', "\u00bb": '"',
})


def normalize_dialogue(text: str) -> str:
    """Canonical form for exact dialogue matching; no fuzzy/spelling edits.

    Removes GTA tilde control tags, folds case, maps typographic punctuation to
    ASCII equivalents, normalizes Unicode, and collapses whitespace. Word
    spelling and punctuation marks remain significant.
    """
    value = unicodedata.normalize("NFC", str(text)).translate(_PUNCT)
    value = _TAGS.sub(" ", value).casefold()
    value = _BEFORE_PUNCT.sub(r"\1", value)
    return _SPACE.sub(" ", value).strip()


def dialogue_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_dialogue(text).encode("utf-8")).hexdigest()


@lru_cache(maxsize=4)
def _read_database(path: Path = DATA_PATH) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {"records": []}


@lru_cache(maxsize=4)
def _index_database(path: Path = DATA_PATH) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for record in _read_database(path).get("records", []):
        index.setdefault(record.get("fingerprint", ""), []).append(record)
    return index


def resolve_speaker(
    mission: str | None,
    text: str,
    *,
    overrides: dict[str, str] | None = None,
    database_path: Path = DATA_PATH,
) -> dict[str, Any]:
    """Resolve only a unique normalized exact source-text fingerprint.

    ``mission`` is retained in the result for caller context. The supplied wiki
    covers non-mission dialogue, so it does not prove mission-specific context.
    An explicit caller override keyed by normalized text takes precedence.
    """
    normalized = normalize_dialogue(text)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    override = (overrides or {}).get(normalized)
    if override:
        return {
            "status": "override", "identity": override, "confidence": "user",
            "speaker_kind": "user_override",
            "evidence": "explicit user-provided speaker override",
            "fingerprint": fingerprint, "mission": mission,
        }

    matches = _index_database(database_path).get(fingerprint, [])
    identities = {r.get("speaker_identity") for r in matches}
    identities.discard(None)
    if len(identities) == 1:
        identity = next(iter(identities))
        supporting = [r for r in matches if r.get("speaker_identity") == identity]
        confidence = "high" if all(r.get("confidence") == "high" for r in supporting) else "medium"
        first = supporting[0]
        return {
            "status": "matched", "identity": identity, "confidence": confidence,
            "speaker_kind": first.get("speaker_kind", "voice_archetype"),
            "evidence": first.get("evidence"), "source_url": first.get("source_url"),
            "source_anchor": first.get("source_anchor"),
            "fingerprint": fingerprint, "mission": mission,
            "mission_context_verified": False,
        }
    if len(identities) > 1:
        return {"status": "ambiguous", "identity": None, "confidence": "none",
                "evidence": "same normalized dialogue is attributed to multiple speakers",
                "candidates": sorted(identities), "fingerprint": fingerprint, "mission": mission}
    return {"status": "unknown", "identity": None, "confidence": "none",
            "evidence": "no exact normalized source-text match", "fingerprint": fingerprint,
            "mission": mission}
