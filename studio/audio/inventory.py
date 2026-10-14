"""Offline inventory of extracted GTA audio and its GXT caption evidence.

The inventory records physical WAV files even when no caption can be linked.
Rebuilds are written to a temporary SQLite database in the destination folder
and published with ``os.replace`` only after a complete successful scan.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from studio.audio.wav import wav_info


KNOWN_PAKS = frozenset((
    "FEET", "GENRL", "PAIN_A", "SCRIPT", "SPC_EA", "SPC_FA", "SPC_GA",
    "SPC_NA", "SPC_PA",
))
UNLINKED_STATUSES = ("no_gxt_match", "missing_gxt_entry", "ambiguous_caption", "unclassified")
_BANK_RE = re.compile(r"^bank_(\d+)$", re.IGNORECASE)
_SOUND_RE = re.compile(r"^sound_(\d+)$", re.IGNORECASE)
_GXT_ID_RE = re.compile(r"^\s*([^:\s]+)\s*:\s*(?:0x)?([0-9a-f]+)\s*$", re.IGNORECASE)
_AUDIO_ID_RE = re.compile(r"^\s*([a-z0-9_]+)\s*:\s*b0*(\d+)\s*:\s*s0*(\d+)\s*$", re.IGNORECASE)


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("audio inventory build cancelled")


def _canonical_audio_id(pak, bank_num, sound_num):
    if pak not in KNOWN_PAKS or bank_num < 1 or sound_num < 1:
        return None
    return f"{pak}:B{bank_num - 1:03d}:S{sound_num - 1:03d}"


def _coordinates(path):
    """Return (PAK, one-based bank, one-based sound) from an unambiguous path."""
    parts = Path(path).parts
    if len(parts) < 3:
        return None, None, None
    bank = _BANK_RE.fullmatch(parts[-2])
    sound = _SOUND_RE.fullmatch(Path(parts[-1]).stem)
    if not bank or not sound:
        return None, None, None
    pak_parts = [part.upper() for part in parts[:-2] if part.upper() in KNOWN_PAKS]
    if len(pak_parts) != 1 or pak_parts[0] != parts[-3].upper():
        return None, None, None
    pak = pak_parts[0]
    bank_num, sound_num = int(bank.group(1)), int(sound.group(1))
    if bank_num < 1 or sound_num < 1:
        return None, None, None
    return pak, bank_num, sound_num


def _parse_gxt_id(value):
    if not value:
        return None
    match = _GXT_ID_RE.fullmatch(str(value))
    if not match:
        return None
    try:
        return match.group(1).upper(), int(match.group(2), 16)
    except ValueError:
        return None


def _parse_audio_id(value):
    if not value:
        return None
    match = _AUDIO_ID_RE.fullmatch(str(value))
    if not match:
        return None
    pak = match.group(1).upper()
    if pak not in KNOWN_PAKS:
        return None
    return f"{pak}:B{int(match.group(2)):03d}:S{int(match.group(3)):03d}"


def _entry_hash(entry):
    value = entry.get("hash")
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, int):
            return value
        raw = str(value).strip()
        return int(raw, 16 if raw.lower().startswith("0x") else 10)
    except (TypeError, ValueError):
        return None


def _entry_text(entry):
    for key in ("text", "value", "decoded_text", "current_text", "cur"):
        if entry.get(key) is not None:
            return str(entry[key])
    return ""


def _prepare_entries(entries, source):
    index = defaultdict(list)
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        table = str(entry.get("table") or "").strip().upper()
        hash_value = _entry_hash(entry)
        if not table or hash_value is None:
            continue
        item = {
            "table": table,
            "hash": hash_value,
            "text": _entry_text(entry),
            "source": source,
            "gxt_stable_id": f"{table}:{hash_value:08X}",
        }
        index[(table, hash_value)].append(item)
    return index


def context_fingerprints(base_entries=(), mod_entries=()):
    """Return stable hashes for exact base/mod GXT entry snapshots.

    Entry order does not affect the fingerprint, but duplicate entries and
    exact text do. These values identify the supplied snapshots, including
    unsaved in-memory edits; they are not inferred from the inventory path.
    """
    def fingerprint(entries):
        index = _prepare_entries(entries, "")
        rows = sorted(
            (table, hash_value, item["text"])
            for (table, hash_value), items in index.items()
            for item in items
        )
        canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    base_fingerprint = fingerprint(base_entries)
    mod_fingerprint = fingerprint(mod_entries)
    context = json.dumps(
        {"base": base_fingerprint, "mod": mod_fingerprint},
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "base_entries_fingerprint": base_fingerprint,
        "mod_entries_fingerprint": mod_fingerprint,
        "context_fingerprint": hashlib.sha256(context.encode("utf-8")).hexdigest(),
    }


def update_inventory_context_metadata(path, base_entries=(), mod_entries=()):
    """Atomically attach explicitly supplied snapshot hashes to a published DB.

    Callers must pass the snapshots known to have been used for that inventory
    (or intentionally declare the current snapshot). This helper does not
    inspect paths or claim that the supplied entries produced the audio rows.
    """
    fingerprints = context_fingerprints(base_entries, mod_entries)
    connection = sqlite3.connect(str(Path(path).absolute()), timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        meta_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inventory_meta'"
        ).fetchone()
        if not meta_exists:
            raise RuntimeError("audio inventory has no metadata table")
        connection.executemany(
            "INSERT INTO inventory_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, json.dumps(value, ensure_ascii=False)) for key, value in fingerprints.items()],
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return fingerprints


def _record_caption_candidates(record, base_index, mod_index):
    """Return evidence links and flags without using event names as captions."""
    stable = _parse_gxt_id(record.get("gxt_stable_id"))
    method = "gxt_stable_id" if stable else None
    if stable is None:
        table = str(record.get("table") or record.get("gxt_table") or "").strip().upper()
        jamcrc = record.get("jamcrc")
        try:
            if isinstance(jamcrc, bool) or jamcrc is None:
                raise ValueError
            hash_value = int(jamcrc, 16) if isinstance(jamcrc, str) else int(jamcrc)
        except (TypeError, ValueError):
            hash_value = None
        if table and hash_value is not None:
            stable = (table, hash_value)
            method = "jamcrc"

    if stable is None:
        return [], False, False

    table_hash = stable
    found = []
    ambiguous = False
    for index in (base_index, mod_index):
        candidates = index.get(table_hash, [])
        if not candidates:
            continue
        is_ambiguous = len(candidates) != 1
        ambiguous = ambiguous or is_ambiguous
        for candidate in candidates:
            link = dict(candidate)
            if is_ambiguous:
                link["method"] = "jamcrc_ambiguous" if method == "jamcrc" else "gxt_stable_id_ambiguous"
            else:
                link["method"] = "jamcrc_unique" if method == "jamcrc" else method
            link["caption_cooccurrence_verified"] = False
            link["manifest_event_name"] = str(record.get("event_name") or "")
            link["manifest_jamcrc"] = str(record.get("jamcrc") or "")
            found.append(link)
    missing = not found and method == "gxt_stable_id"
    return found, ambiguous, missing


def _build_audio_links(manifest_records, base_index, mod_index):
    by_audio = defaultdict(list)
    flags = defaultdict(lambda: {"ambiguous": False, "missing": False})
    for record in manifest_records or ():
        if not isinstance(record, dict):
            continue
        audio_id = _parse_audio_id(record.get("stable_audio_id"))
        if audio_id is None:
            continue
        links, ambiguous, missing = _record_caption_candidates(record, base_index, mod_index)
        by_audio[audio_id].extend(links)
        flags[audio_id]["ambiguous"] = flags[audio_id]["ambiguous"] or ambiguous
        flags[audio_id]["missing"] = flags[audio_id]["missing"] or missing
        if links:
            identities = flags[audio_id].setdefault("caption_ids", set())
            identities.update((link["table"], link["hash"]) for link in links)
            if len(identities) > 1:
                flags[audio_id]["ambiguous"] = True
    return by_audio, flags


def _merge_links(links):
    """Group base/mod variants together while preserving duplicate entry text."""
    grouped = {}
    for link in links:
        key = (link["table"], link["hash"], link["method"], link["manifest_event_name"], link["manifest_jamcrc"])
        target = grouped.setdefault(key, {
            "table": link["table"], "hash": link["hash"],
            "gxt_stable_id": link["gxt_stable_id"], "method": link["method"],
            "caption_cooccurrence_verified": False,
            "manifest_event_name": link["manifest_event_name"],
            "manifest_jamcrc": link["manifest_jamcrc"],
            "sources": [],
            "base_texts": [], "mod_texts": [], "base_text": None, "mod_text": None,
        })
        if link["source"] not in target["sources"]:
            target["sources"].append(link["source"])
        bucket = target["base_texts"] if link["source"] == "base" else target["mod_texts"]
        if link["text"] not in bucket:
            bucket.append(link["text"])
        if link["source"] == "base" and target["base_text"] is None:
            target["base_text"] = link["text"]
        if link["source"] == "mod" and target["mod_text"] is None:
            target["mod_text"] = link["text"]
    return list(grouped.values())


def _root_specs(roots):
    specs = []
    for root in roots or ():
        if isinstance(root, (str, os.PathLike)):
            path, source, label = Path(root), "unknown", ""
        elif isinstance(root, dict):
            path = Path(root.get("path") or "")
            source = str(root.get("source") or "unknown")
            label = str(root.get("label") or "")
        else:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        specs.append({"path": resolved, "source": source, "label": label})
    return specs


def _discover_wavs(roots, cancel_event=None, on_progress=None):
    """Yield each physical WAV once, assigning the most-specific root."""
    discovered = {}
    visited_directories = 0
    visited_files = 0
    visited_wavs = 0
    for root_index, spec in enumerate(roots):
        _check_cancelled(cancel_event)
        path = spec["path"]
        if path.is_file():
            candidates = [path] if path.suffix.lower() == ".wav" else []
        elif path.is_dir():
            candidates = []
            for directory, dirnames, filenames in os.walk(path):
                _check_cancelled(cancel_event)
                visited_directories += 1
                dirnames.sort(key=str.casefold)
                for name in filenames:
                    _check_cancelled(cancel_event)
                    visited_files += 1
                    if Path(name).suffix.lower() == ".wav":
                        visited_wavs += 1
                        candidates.append(Path(directory) / name)
                    if on_progress and visited_files % 100 == 0:
                        on_progress(visited_wavs, None, "Localizando arquivos")
                if on_progress and visited_directories % 100 == 0:
                    on_progress(visited_wavs, None, "Localizando arquivos")
        else:
            continue
        for candidate in candidates:
            _check_cancelled(cancel_event)
            try:
                physical = candidate.resolve()
                stat = physical.stat()
            except OSError:
                continue
            key = os.path.normcase(str(physical)).casefold()
            previous = discovered.get(key)
            choice = (len(str(path)), -root_index, spec, physical, stat)
            if previous is None or choice[:2] > previous[:2]:
                discovered[key] = choice
    for _specificity, _root_order, spec, path, stat in discovered.values():
        yield spec, path, stat


_SCHEMA = """
CREATE TABLE inventory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE audio_items (
    id INTEGER PRIMARY KEY,
    stable_audio_id TEXT,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    duration REAL,
    sample_rate INTEGER,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    pak TEXT,
    bank_num INTEGER,
    sound_num INTEGER,
    status TEXT NOT NULL,
    links_json TEXT NOT NULL,
    search_text TEXT NOT NULL,
    UNIQUE(source, pak, bank_num, sound_num, path)
);
CREATE INDEX audio_pak_status_idx ON audio_items(pak, status);
CREATE INDEX audio_source_status_idx ON audio_items(source, status);
CREATE INDEX audio_stable_idx ON audio_items(stable_audio_id);
CREATE INDEX audio_path_idx ON audio_items(path);
"""


def build_inventory(path, roots, manifest_records=(), base_entries=(), mod_entries=(), cancel_event=None, on_progress=None, scope_paks=None):
    """Build and atomically publish an offline inventory database.

    ``on_progress`` receives ``(processed, total, detail)``. It first reports
    ``(0, None, "Localizando arquivos")``, then discovery updates periodically,
    and finally file indexing progress every hundred WAVs. Cancellation or any
    failed rebuild leaves the previously published database untouched.
    """
    destination = Path(path).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    base_entries = tuple(base_entries or ())
    mod_entries = tuple(mod_entries or ())
    if scope_paks is None:
        normalized_scope = None
    else:
        normalized_scope = sorted({str(pak).upper() for pak in scope_paks})
        unknown_scope = set(normalized_scope) - KNOWN_PAKS
        if unknown_scope:
            raise ValueError("unknown inventory PAK scope: " + ", ".join(sorted(unknown_scope)))
    root_specs = _root_specs(roots)
    if on_progress:
        on_progress(0, None, "Localizando arquivos")
    _check_cancelled(cancel_event)
    files = list(_discover_wavs(root_specs, cancel_event, on_progress))
    base_index = _prepare_entries(base_entries, "base")
    mod_index = _prepare_entries(mod_entries, "mod")
    fingerprints = context_fingerprints(base_entries, mod_entries)
    links_by_audio, flags_by_audio = _build_audio_links(manifest_records, base_index, mod_index)

    temp_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent), delete=False
    )
    temp_path = Path(temp_handle.name)
    temp_handle.close()
    processed = 0
    try:
        _check_cancelled(cancel_event)
        connection = sqlite3.connect(str(temp_path))
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN")
            if on_progress:
                on_progress(0, len(files), "Indexando arquivos")
            for spec, wav_path, stat in files:
                _check_cancelled(cancel_event)
                pak, bank_num, sound_num = _coordinates(wav_path)
                stable_audio_id = _canonical_audio_id(pak, bank_num, sound_num) if pak else None
                info = wav_info(wav_path)
                invalid = info is None
                raw_links = links_by_audio.get(stable_audio_id, []) if stable_audio_id else []
                links = _merge_links(raw_links)
                flags = flags_by_audio.get(stable_audio_id, {}) if stable_audio_id else {}
                if invalid:
                    status = "invalid"
                elif stable_audio_id is None:
                    status = "unclassified"
                elif flags.get("ambiguous"):
                    status = "ambiguous_caption"
                elif links:
                    status = "mapped_caption"
                elif flags.get("missing"):
                    status = "missing_gxt_entry"
                else:
                    status = "no_gxt_match"
                link_json = json.dumps(links, ensure_ascii=False, separators=(",", ":"))
                path_text = str(wav_path)
                search_parts = [path_text, spec["label"], stable_audio_id or ""]
                for link in links:
                    search_parts.extend(link["base_texts"])
                    search_parts.extend(link["mod_texts"])
                    search_parts.append(link["manifest_event_name"])
                search_text = " ".join(search_parts).casefold()
                connection.execute(
                    """INSERT INTO audio_items
                    (stable_audio_id, source, label, path, duration, sample_rate, size_bytes, mtime_ns,
                     pak, bank_num, sound_num, status, links_json, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (stable_audio_id, spec["source"], spec["label"], path_text,
                     info["dur"] if info else None, info["taxa"] if info else None,
                     stat.st_size, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
                     pak, bank_num, sound_num, status, link_json, search_text),
                )
                processed += 1
                if on_progress and (processed % 100 == 0 or processed == len(files)):
                    on_progress(processed, len(files), path_text)
            _check_cancelled(cancel_event)
            connection.commit()
            row_count = connection.execute("SELECT COUNT(*) FROM audio_items").fetchone()[0]
            status_counts = dict(connection.execute("SELECT status, COUNT(*) FROM audio_items GROUP BY status").fetchall())
            collision_count = status_counts.get("ambiguous_caption", 0)
            mapped_count = connection.execute(
                "SELECT COUNT(DISTINCT stable_audio_id) FROM audio_items WHERE status = 'mapped_caption'"
            ).fetchone()[0]
            invalid_count = status_counts.get("invalid", 0)
            no_link_count = connection.execute(
                "SELECT COUNT(*) FROM audio_items WHERE status != 'mapped_caption'"
            ).fetchone()[0]
            summary = {
                "sound_count": row_count,
                "mapped_unique": mapped_count,
                "no_link": no_link_count,
                "invalid": invalid_count,
                "collisions": collision_count,
                "status_counts": status_counts,
                "built_at": datetime.now(timezone.utc).isoformat(),
                "scope_paks": normalized_scope,
                **fingerprints,
            }
            connection.executemany(
                "INSERT INTO inventory_meta(key, value) VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in summary.items()],
            )
            connection.commit()
        finally:
            connection.close()
        _check_cancelled(cancel_event)
        os.replace(str(temp_path), str(destination))
        if on_progress and not files:
            on_progress(0, 0, "")
        return summary
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class Inventory:
    """Short-lived-connection query facade for an inventory SQLite file."""

    def __init__(self, path):
        self.path = Path(path).absolute()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _where(pak=None, source=None, status=None, search=""):
        clauses = []
        values = []
        if pak:
            clauses.append("pak = ?")
            values.append(str(pak).upper())
        if source:
            clauses.append("source = ?")
            values.append(str(source))
        if status:
            if str(status).lower() == "unlinked":
                marks = ",".join("?" for _ in UNLINKED_STATUSES)
                clauses.append(f"status IN ({marks})")
                values.extend(UNLINKED_STATUSES)
            else:
                clauses.append("status = ?")
                values.append(str(status).lower())
        if search:
            clauses.append("search_text LIKE ?")
            values.append(f"%{str(search).casefold()}%")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    @staticmethod
    def _row_dict(row):
        result = dict(row)
        result["links"] = json.loads(result.pop("links_json"))
        result.pop("search_text", None)
        return result

    def query(self, pak=None, source=None, status=None, search="", limit=200, offset=0):
        where, values = self._where(pak, source, status, search)
        limit = max(0, min(int(limit), 10000))
        offset = max(0, int(offset))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM audio_items" + where + " ORDER BY pak, bank_num, sound_num, source, path LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def get(self, item_id):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM audio_items WHERE id = ?", (int(item_id),)).fetchone()
        return self._row_dict(row) if row else None

    def count(self, pak=None, source=None, status=None, search=""):
        where, values = self._where(pak, source, status, search)
        with closing(self._connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM audio_items" + where, values).fetchone()[0]

    def summary(self):
        with closing(self._connect()) as connection:
            values = connection.execute("SELECT key, value FROM inventory_meta").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in values}

    def context_matches(self, base_entries=(), mod_entries=()):
        """Compare current in-memory GXT snapshots with stored inventory IDs."""
        stored = self.summary()
        current = context_fingerprints(base_entries, mod_entries)
        return {
            "matches": all(stored.get(key) == value for key, value in current.items()),
            "stored": {key: stored.get(key) for key in current},
            "current": current,
        }

    def scope_matches(self, paks):
        """Whether this index was built for exactly the requested PAK set."""
        requested = sorted({str(pak).upper() for pak in (paks or ())})
        unknown = set(requested) - KNOWN_PAKS
        if unknown:
            return False
        return self.summary().get("scope_paks") == requested

    def close(self):
        """Compatibility no-op; every public method closes its own connection."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
