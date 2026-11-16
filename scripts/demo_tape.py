"""Build the hosted demo's tape from a recorded run (pure functions; scripts/record_tape.py does the I/O).

The tape (web/public/tape/tape.json) is what quarry.dragonhound.dev replays. Its rules:

- No identity subject. A run that has one is refused, not filtered.
- Image files only for records that landed with a licence quarry accepts (PD or CC0). A
  quarantined, failed, duplicate or skipped candidate is text and a link to its Commons page.
- Every candidate the run saw is on the tape: nothing is dropped to improve the numbers.
- No absolute path, private address or email address anywhere: pool paths are relative to
  $QUARRY_HOME, an email address in a Commons credit line is replaced by "[email address removed]",
  and anything that still looks like one fails the build.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections import Counter

from argus_quarry.models import _ACCEPTED_TOKENS, normalise_licence

FORMAT = "argus-quarry-tape"
VERSION = 1
ACCEPTED = ("PD", "CC0")

#: Ingest's outcome status -> the verdict the site shows. "search" is the Commons downloader's own
#: filter (not a JPEG or PNG), which drops a file before ingest sees it.
VERDICTS = {
    "complete": "landed",
    "quarantined": "quarantined",
    "duplicate": "duplicate",
    "failed": "failed",
    "skipped": "skipped",
    "search": "not-jpeg-png",
}
VERDICT_ORDER = ("landed", "quarantined", "not-jpeg-png", "duplicate", "failed", "skipped")

LEAKS = [
    ("home directory path", re.compile(r"(?:/home/|/Users/|/root/|~/)[\w.-]+")),
    ("server path", re.compile(r"/(?:srv|mnt|media|var/www|tmp)/[\w.-]+")),
    ("private IPv4 address", re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")),
    ("email address", re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)),
]


class TapeError(ValueError):
    """The run cannot become a tape (a rule above would be broken)."""


def leaks(text: str) -> list[str]:
    """Every path, private address or email address left in ``text``."""
    return [f"{kind}: {m.group(0)}" for kind, pattern in LEAKS for m in pattern.finditer(text)]


EMAIL = LEAKS[3][1]
EMAIL_REDACTED = "[email address removed]"


def credit_text(text: str | None) -> str | None:
    """A Commons credit line as plain text: HTML entities decoded (the downloader strips only tags), and an
    author's email address, which some credit lines carry, replaced: the tape does not publish it."""
    return EMAIL.sub(EMAIL_REDACTED, html.unescape(text)).strip() if text else text


def strip_paths(value, prefixes: dict[str, str]):
    """``value`` with each absolute prefix replaced by its placeholder (longest first), recursively."""
    if isinstance(value, str):
        for prefix in sorted(prefixes, key=len, reverse=True):
            value = value.replace(prefix, prefixes[prefix])
        return value
    if isinstance(value, list):
        return [strip_paths(v, prefixes) for v in value]
    if isinstance(value, dict):
        return {k: strip_paths(v, prefixes) for k, v in value.items()}
    return value


def licence_check(raw: str | None) -> dict:
    """What quarry's licence gate made of ``raw``: its canonical token and the text that matched."""
    canon = normalise_licence(raw)
    text = (raw or "").strip().lower()
    matched = None
    if canon == "CC0":
        matched = "cc0" if "cc0" in text else "creative commons zero"
    elif canon == "PD":
        matched = next(token for token in _ACCEPTED_TOKENS if token in text)
    return {"raw": raw, "normalised": canon, "accepted": canon is not None, "matched": matched}


def verdict_for(entry: dict) -> str:
    status = "search" if entry["stage"] == "search" else entry["outcome"]["status"]
    if status not in VERDICTS:
        raise TapeError(f"unknown outcome status {status!r}")
    return VERDICTS[status]


def record_id(entry: dict) -> str:
    return hashlib.sha1((entry.get("source_url") or entry["remote_url"]).encode()).hexdigest()[:10]


def _mp(width: int | None, height: int | None) -> str:
    return f"{(width or 0) * (height or 0) / 1e6:.1f} MP"


def reason_for(entry: dict, verdict: str, licence: dict, max_mp: float) -> str:
    """One or two plain sentences: what happened to this candidate and why."""
    raw = licence["raw"]
    if verdict == "not-jpeg-png":
        return (
            f"Commons lists this file as {entry.get('mime') or 'an unknown type'}. "
            "Quarry's Commons downloader only takes JPEG and PNG, so it never reached the licence check."
        )
    if verdict == "quarantined":
        return (
            f'The licence is "{raw}", which is not public domain or CC0. '
            "Quarry recorded the candidate as quarantined and downloaded nothing."
        )
    if verdict == "landed":
        why = f'The licence is "{raw}", which quarry reads as {licence["normalised"]}'
        why += f' (it contains "{licence["matched"]}").' if licence["matched"] else "."
        row = entry.get("row") or {}
        orig = entry.get("downloaded") or {}
        text = f"{why} The file downloaded, decoded and its SHA256 was new to the pool, so it landed."
        if orig.get("width") and row.get("width") and orig["width"] * orig["height"] > row["width"] * row["height"]:
            text += (
                f" It was {orig['width']}×{orig['height']} ({_mp(orig['width'], orig['height'])}), over the "
                f"{max_mp:g} MP cap, so quarry stored a {row['width']}×{row['height']} copy and kept the "
                "full-resolution URL."
            )
        return text
    if verdict == "duplicate":
        return "The licence passed and the file downloaded, but its SHA256 matched a file already in the pool. The copy was deleted."
    if verdict == "failed":
        return f"The licence passed, but the download or decode failed: {entry['outcome'].get('reason')}."
    return f"Quarry already had a row for this file ({entry['outcome'].get('reason')}), so it did nothing."


def build_tape(run: dict) -> tuple[dict, dict[str, str]]:
    """The tape, and the image files to publish (record id -> thumbnail name in the recording)."""
    for subject in run["subjects"]:
        if subject["category"] == "identity":
            raise TapeError(f"{subject['name']} is an identity subject; the demo records none")
    max_mp = run["settings"]["max_megapixels"]

    records: list[dict] = []
    files: dict[str, str] = {}
    for entry in run["entries"]:
        if entry["category"] == "identity":
            raise TapeError(f"{entry['title']} is in the identity category")
        verdict = verdict_for(entry)
        licence = licence_check(entry["licence"])
        if verdict == "landed" and not licence["accepted"]:
            raise TapeError(f"{entry['title']} landed with licence {entry['licence']!r}, which quarry does not accept")
        row = entry.get("row") or {}
        orig = entry.get("downloaded") or {}
        rid = record_id(entry)
        image = None
        if verdict == "landed" and licence["normalised"] in ACCEPTED and entry.get("thumb") and row.get("sha256"):
            image = {"src": f"thumbs/{rid}.webp", **entry["thumb"]}
            files[rid] = f"{row['sha256'][:16]}.webp"
        records.append(
            {
                "id": rid,
                "subject": entry["subject"],
                "category": entry["category"],
                "query": entry.get("query"),
                "rank": entry.get("rank"),
                "title": entry["title"],
                "verdict": verdict,
                "status": "search" if entry["stage"] == "search" else entry["outcome"]["status"],
                "reason": reason_for(entry, verdict, licence, max_mp),
                "licence": licence,
                "provenance": {
                    "source": "commons",
                    "source_url": entry.get("source_url"),
                    "remote_url": entry.get("remote_url"),
                    "attribution": credit_text(entry.get("attribution")),
                    "photographer": credit_text(entry.get("photographer")),
                    "year": entry.get("year"),
                    "mime": entry.get("mime"),
                    "commons_width": entry.get("width"),
                    "commons_height": entry.get("height"),
                    "commons_bytes": entry.get("bytes"),
                    "downloaded_bytes": orig.get("bytes"),
                    "sha256": row.get("sha256"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "file_size": row.get("file_size"),
                    "path": f"$QUARRY_HOME/{row['path']}" if row.get("path") else None,
                    "fetched_from": row.get("fetched_from"),
                    "db_status": row.get("status"),
                    "db_licence": row.get("licence"),
                    "downloaded_at": row.get("downloaded_at"),
                },
                "image": image,
            }
        )

    by_licence: Counter[str] = Counter(r["licence"]["raw"] for r in records)
    accepted = {r["licence"]["raw"]: r["licence"]["accepted"] for r in records}
    by_subject = []
    for subject in run["subjects"]:
        mine = [r for r in records if r["subject"] == subject["folder"]]
        by_subject.append(
            {
                "name": subject["name"],
                "folder": subject["folder"],
                "category": subject["category"],
                "search": subject.get("search") or subject["name"],
                "total": len(mine),
                "by_verdict": dict(Counter(r["verdict"] for r in mine)),
            }
        )

    tape = {
        "format": FORMAT,
        "version": VERSION,
        "recording": {
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "seconds": run["seconds"],
            "versions": run["versions"],
            "settings": {**run["settings"], "accepted_tokens": list(_ACCEPTED_TOKENS)},
            "subjects_file": run["subjects_file"],
            "commands": [
                f"uv run --no-sync python scripts/record_tape.py record --subjects {run['subjects_file']} "
                f"--limit {run['settings']['limit_per_subject']}",
                "uv run --no-sync python scripts/record_tape.py build",
            ],
            "equivalent_cli": f"argus-quarry fetch --source commons --limit {run['settings']['limit_per_subject']}",
            "rerun": run["rerun"],
        },
        "summary": {
            "total": len(records),
            "images": len(files),
            "by_verdict": {v: sum(r["verdict"] == v for r in records) for v in VERDICT_ORDER},
            "by_category": dict(Counter(r["category"] for r in records)),
            "by_licence": [
                {"licence": name, "count": n, "accepted": accepted[name]} for name, n in by_licence.most_common()
            ],
            "by_subject": by_subject,
        },
        "records": records,
    }
    check_tape(tape, files)
    return tape, files


def check_tape(tape: dict, files: dict[str, str]) -> None:
    """Fail if the tape breaks a rule: identity, an image of a non-cleared record, or a leak."""
    import json

    for record in tape["records"]:
        if record["category"] == "identity":
            raise TapeError(f"{record['id']} is in the identity category")
        has_image = record["image"] is not None or record["id"] in files
        if has_image and not (record["verdict"] == "landed" and record["licence"]["normalised"] in ACCEPTED):
            raise TapeError(f"{record['id']} is {record['verdict']} ({record['licence']['raw']}) but has an image")
    found = leaks(json.dumps(tape, ensure_ascii=False))
    if found:
        raise TapeError("the tape would publish: " + "; ".join(found))
