"""Build a compact, offline GTA SA speaker index from GTA Wiki facts.

The artifact stores speaker/context claims and SHA-256 fingerprints only. It
does not retain the dialogue text quoted on the source page.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio.script.speakers import dialogue_fingerprint, normalize_dialogue  # noqa: E402

PAGE = "Dialogues_in_GTA_San_Andreas"
PAGE_URL = "https://gta.fandom.com/wiki/Dialogues_in_GTA_San_Andreas"
API_URL = "https://gta.fandom.com/api.php"
DEFAULT_OUTPUT = ROOT / "studio" / "data" / "speakers" / "speaker_database.json"
_HEADING = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_TAG = re.compile(r"<[^>]*>")
_REF = re.compile(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", re.I)
_SPEAKER_LABEL = re.compile(r"'''([^']{1,80}):'''\s*")
_GENERIC_CONTEXT = re.compile(r"^(when\b|variant\b|male$|female$|serving\b|getting\b|fighting$|talking\b|carjacking$|clothing shops$|bone county(?: \(3d universe\))?$|countryside$|\s*$)", re.I)
_ROOT_VOICE_CATEGORIES = {"Clerks", "Rednecks"}

_NAMED_CHARACTERS = {
    "Carl Johnson", "Sweet Johnson", "Big Smoke", "Ryder", "Wu Zi Mu",
    "Cesar Vialpando", "The Truth", "T-Bone Mendez", "Katie Zhan",
    "Denise Robinson", "Michelle Cannes", "Barbara Schternvart",
    "Helena Wankstein", "Millie Perkins",
}
_GROUP_IDENTITIES = {
    "Los Santos Police Department (3D Universe)", "San Fierro Police Department",
    "Las Venturas Police Department", "San Andreas Sheriff", "San Andreas Coast Guard",
    "Motor Officer", "Special Weapons and Tactics", "Special Weapons and Tactics (3D Universe)",
    "Federal Bureau of Investigation", "Military (3D Universe)",
    "Grove Street Families", "Ballas (3D Universe)", "San Fierro Triads",
    "Da Nang Boys", "Los Santos Vagos (3D Universe)",
    "Varrios Los Aztecas (3D Universe)", "Crack Dealers", "Russian Mafia (3D Universe)",
}


def _speaker_kind(identity: str, explicit: bool = False) -> str:
    if identity in _NAMED_CHARACTERS:
        return "named_character"
    if identity in _GROUP_IDENTITIES:
        return "group_or_ensemble"
    if explicit:
        return "role_label"
    return "voice_archetype"


def fetch_page() -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "action": "parse", "page": PAGE, "prop": "wikitext|revid", "format": "json",
        "formatversion": "2",
    })
    request = urllib.request.Request(API_URL + "?" + query,
                                     headers={"User-Agent": "GTA-SASpeakerDB/1.0 (offline factual index)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    parse = payload.get("parse")
    if not parse or not isinstance(parse.get("wikitext"), str):
        raise RuntimeError("MediaWiki API response did not contain page wikitext")
    return {"wikitext": parse["wikitext"], "revision_id": parse.get("revid"),
            "page_title": parse.get("title", "Dialogues in GTA San Andreas")}


def _plain(value: str) -> str:
    value = _REF.sub(" ", value)
    value = _LINK.sub(lambda m: (m.group(1).split("|", 1)[-1]).replace("_", " "), value)
    value = re.sub(r"\{\{[^{}]*\}\}", " ", value)
    value = re.sub(r"'{2,5}", "", value)
    value = _TAG.sub(" ", value)
    value = html.unescape(value)
    value = re.sub(r"\[https?://[^ ]+\s+([^\]]+)\]", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _heading_identity(raw: str) -> str:
    links = list(_LINK.finditer(raw))
    if not links:
        return _plain(raw)
    # A standalone linked entity uses its canonical article title. Embedded
    # links use their visible label and preserve adjacent suffixes (e.g. the
    # wiki heading [[Samoa]]n pedestrian -> Samoan pedestrian).
    if len(links) == 1 and not _plain(raw[:links[0].start()]) and not _plain(raw[links[0].end():]):
        value = links[0].group(1).split("|", 1)[0].split("#", 1)[0]
        return value.replace("_", " ")
    def replacement(match: re.Match[str]) -> str:
        target, separator, label = match.group(1).partition("|")
        return label if separator else target.split("#", 1)[0].replace("_", " ")
    return _plain(_LINK.sub(replacement, raw))


def _anchor(raw: str) -> str:
    heading = _heading_identity(raw).lower().replace(" ", "_")
    heading = re.sub(r"[^\w\-]", "", heading, flags=re.UNICODE)
    return heading or "dialogue"


def extract_records(wikitext: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    records: list[dict[str, Any]] = []
    headings: list[tuple[int, str, str]] = []
    stats: Counter[str] = Counter()

    for line in wikitext.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            level = len(match.group(1))
            raw_heading = match.group(2)
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, raw_heading, _heading_identity(raw_heading)))
            stats["headings"] += 1
            continue
        if not re.match(r"^\s*[*#]\s*", line):
            continue
        stats["bullet_lines"] += 1

        body = re.sub(r"^\s*[*#]+\s*", "", line).strip()
        explicit = _SPEAKER_LABEL.findall(body)
        if len(explicit) > 1:
            stats["excluded_multi_speaker_bullets"] += 1
            continue

        identity = None
        speaker_heading_level = 2 if explicit else None
        confidence = "medium"
        evidence = "speaker is the section identity stated by the source heading"
        if explicit:
            identity = _plain(explicit[0]).strip()
            body = _SPEAKER_LABEL.sub("", body, count=1)
            confidence = "high"
            evidence = "speaker is explicitly labeled in the source dialogue line"
        else:
            # The deepest heading that names a speaker/group remains in effect
            # under nested headings such as "When bumped into".
            for level, raw_heading, heading_identity in reversed(headings):
                root_category = level == 2 and heading_identity in _ROOT_VOICE_CATEGORIES
                if level < 2 or (level == 2 and "[[" not in raw_heading and not root_category):
                    break
                if _GENERIC_CONTEXT.match(heading_identity):
                    continue
                identity = heading_identity
                speaker_heading_level = level
                if not _GENERIC_CONTEXT.match(heading_identity):
                    confidence = "high" if ("[" in raw_heading or "[[" in raw_heading) else "medium"
                break
        if not identity:
            stats["excluded_no_speaker_context"] += 1
            continue

        text = _plain(body)
        normalized = normalize_dialogue(text)
        if not normalized or len(normalized) < 3:
            stats["excluded_empty_or_short"] += 1
            continue
        context = [h for level, _, h in headings if level > (speaker_heading_level or 2)]
        anchor_raw = next((raw for level, raw, _ in reversed(headings) if level >= 2), "")
        records.append({
            "fingerprint": dialogue_fingerprint(text),
            "speaker_identity": identity,
            "speaker_kind": _speaker_kind(identity, explicit=bool(explicit)),
            "confidence": confidence,
            "evidence": evidence,
            "source_url": PAGE_URL + "#" + _anchor(anchor_raw),
            "source_heading": identity,
            "source_context": " > ".join(context) if context else None,
        })

    # Repeated identical text under the same identity is redundant. Preserve
    # multiple identities so lookup can correctly return ambiguity.
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["fingerprint"], record["speaker_identity"], record["source_url"])
        dedup.setdefault(key, record)
    records = sorted(dedup.values(), key=lambda row: (row["fingerprint"], row["speaker_identity"], row["source_url"]))
    stats["fingerprinted_records"] = len(records)
    stats["unique_fingerprints"] = len({r["fingerprint"] for r in records})
    stats["ambiguous_fingerprints"] = sum(
        1 for speakers in _speakers_by_fingerprint(records).values() if len(speakers) > 1)
    return records, stats


def _speakers_by_fingerprint(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for record in records:
        result[record["fingerprint"]].add(record["speaker_identity"])
    return result


def build_database(output: Path = DEFAULT_OUTPUT, manifest_path: Path | None = None) -> dict[str, Any]:
    source = fetch_page()
    records, stats = extract_records(source["wikitext"])
    database = {
        "schema_version": 1,
        "description": "Offline GTA SA speaker claims indexed by normalized dialogue SHA-256; no dialogue text is stored.",
        "source": {
            "page_title": source["page_title"],
            "url": PAGE_URL,
            "api_url": API_URL + "?action=parse&page=" + PAGE,
            "revision_id": source["revision_id"],
            "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "attribution": "GTA Wiki contributors, Dialogues in GTA San Andreas (Fandom).",
            "license_note": "Source footer identifies community content as CC BY-SA unless otherwise noted; dialogue quotations may have separate Rockstar Games copyright. This artifact retains hashes and factual speaker/context claims only.",
            "license_reference": "https://gta.fandom.com/wiki/GTA_Wiki:Copyright",
        },
        "normalization": "NFC; GXT tilde tags removed; smart quote/dash/ellipsis forms mapped to ASCII; casefold; whitespace collapsed; spelling and punctuation otherwise remain significant.",
        "coverage": dict(stats),
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(database, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if manifest_path:
        write_manifest_overlay(manifest_path, output.with_name("speaker_matches.jsonl"), database)
    return database


def write_manifest_overlay(manifest_path: Path, output_path: Path, database: dict[str, Any]) -> dict[str, int]:
    by_fingerprint = _speakers_by_fingerprint(database["records"])
    rec_by_fp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in database["records"]:
        rec_by_fp[record["fingerprint"]].append(record)
    stats = Counter()
    with manifest_path.open("r", encoding="utf-8") as source, output_path.open("w", encoding="utf-8", newline="\n") as dest:
        for line in source:
            if not line.strip():
                continue
            manifest = json.loads(line)
            text = manifest.get("source_text")
            if not text:
                continue
            fingerprint = dialogue_fingerprint(text)
            identities = by_fingerprint.get(fingerprint, set())
            if len(identities) == 1:
                identity = next(iter(identities))
                evidence = next(r for r in rec_by_fp[fingerprint] if r["speaker_identity"] == identity)
                dest.write(json.dumps({
                    "stable_audio_id": manifest.get("stable_audio_id"),
                    "gxt_stable_id": manifest.get("gxt_stable_id"),
                    "speaker_identity": identity,
                    "speaker_kind": evidence["speaker_kind"],
                    "confidence": evidence["confidence"],
                    "fingerprint": fingerprint,
                    "source_url": evidence["source_url"],
                    "source_evidence": evidence["evidence"],
                    "mission_context_verified": False,
                }, ensure_ascii=False) + "\n")
                stats["matched_unique"] += 1
            elif identities:
                stats["ambiguous"] += 1
            else:
                stats["unmatched"] += 1
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, help="Optional SCRIPT mapping JSONL; only source_text is fingerprinted.")
    args = parser.parse_args()
    db = build_database(args.output, args.manifest)
    print(json.dumps({"output": str(args.output), "revision_id": db["source"]["revision_id"],
                      "coverage": db["coverage"]}, indent=2))


if __name__ == "__main__":
    main()
