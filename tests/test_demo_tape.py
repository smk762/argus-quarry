"""The hosted demo's tape builder (scripts/demo_tape.py): what may and may not be published."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("demo_tape", ROOT / "scripts" / "demo_tape.py")
demo_tape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_tape)


def _entry(title: str, licence: str, status: str, *, category: str = "concept", stage: str = "ingest", **extra) -> dict:
    url = f"https://commons.wikimedia.org/wiki/File:{title}"
    landed = status == "complete"
    sha = (title.encode().hex() * 8)[:64]
    entry = {
        "stage": stage,
        "subject": "Aurora_borealis",
        "category": category,
        "query": "aurora",
        "rank": 1,
        "mime": "image/jpeg",
        "width": 2000,
        "height": 1000,
        "bytes": 12345,
        "title": title,
        "source_url": url,
        "remote_url": f"https://upload.wikimedia.org/{title}",
        "licence": licence,
        "attribution": "Someone",
        "photographer": "Someone",
        "year": 1900,
        "outcome": {"status": status, "reason": None, "fetched_from": None},
        "downloaded": {"width": 2000, "height": 1000, "bytes": 12345} if landed else None,
        "row": {
            "status": status,
            "licence": licence,
            "sha256": sha if landed else None,
            "width": 2000 if landed else None,
            "height": 1000 if landed else None,
            "file_size": 12345 if landed else None,
            "path": f"images/{category}/Aurora_borealis/a.jpg" if landed else None,
            "fetched_from": None,
            "downloaded_at": None,
        },
    }
    if landed:
        entry["thumb"] = {"width": 640, "height": 320}
    entry.update(extra)
    return entry


def _run(entries: list[dict], subjects: list[dict] | None = None) -> dict:
    return {
        "started_at": "2026-11-16T00:00:00Z",
        "finished_at": "2026-11-16T00:01:00Z",
        "seconds": 60,
        "versions": {"argus_quarry": "0.4.1", "commit": "abc", "src_modified": False, "python": "3.12", "packages": {}},
        "settings": {"source": "commons", "limit_per_subject": 10, "max_megapixels": 12.0},
        "subjects_file": "docs/demo/subjects.yaml",
        "subjects": subjects
        or [{"name": "Aurora borealis", "folder": "Aurora_borealis", "category": "concept", "search": "aurora"}],
        "entries": entries,
        "rerun": {"skipped": len(entries)},
    }


@pytest.mark.parametrize(
    ("raw", "canon", "matched"),
    [
        ("Public domain", "PD", "public domain"),
        ("CC0", "CC0", "cc0"),
        ("PD-US", "PD", "pd"),
        ("CC BY-SA 4.0", None, None),
        ("CC BY 2.0", None, None),
        ("No restrictions", None, None),
        ("unknown", None, None),
    ],
)
def test_licence_check_mirrors_the_ingest_gate(raw, canon, matched):
    check = demo_tape.licence_check(raw)
    assert check == {"raw": raw, "normalised": canon, "accepted": canon is not None, "matched": matched}


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("complete", "landed"),
        ("quarantined", "quarantined"),
        ("duplicate", "duplicate"),
        ("failed", "failed"),
        ("skipped", "skipped"),
    ],
)
def test_outcome_status_maps_to_a_verdict(status, verdict):
    assert demo_tape.verdict_for({"stage": "ingest", "outcome": {"status": status}}) == verdict


def test_search_filtered_files_are_their_own_verdict_and_unknown_statuses_fail():
    assert demo_tape.verdict_for({"stage": "search"}) == "not-jpeg-png"
    with pytest.raises(demo_tape.TapeError):
        demo_tape.verdict_for({"stage": "ingest", "outcome": {"status": "downloading"}})


def test_build_gives_images_to_landed_pd_and_cc0_only():
    entries = [
        _entry("pd.jpg", "Public domain", "complete"),
        _entry("cc0.jpg", "CC0", "complete"),
        _entry("bysa.jpg", "CC BY-SA 4.0", "quarantined"),
        _entry("tiff.tif", "Public domain", "", stage="search", mime="image/tiff", outcome=None),
    ]
    tape, files = demo_tape.build_tape(_run(entries))
    by_title = {r["title"]: r for r in tape["records"]}
    assert by_title["pd.jpg"]["image"] and by_title["cc0.jpg"]["image"]
    assert by_title["bysa.jpg"]["image"] is None and by_title["tiff.tif"]["image"] is None
    assert set(files) == {by_title["pd.jpg"]["id"], by_title["cc0.jpg"]["id"]}
    assert by_title["bysa.jpg"]["verdict"] == "quarantined"
    assert "CC BY-SA 4.0" in by_title["bysa.jpg"]["reason"] and "downloaded nothing" in by_title["bysa.jpg"]["reason"]
    assert by_title["tiff.tif"]["verdict"] == "not-jpeg-png"
    assert tape["summary"]["by_verdict"]["landed"] == 2
    assert tape["summary"]["images"] == 2


def test_a_landed_record_with_an_unaccepted_licence_is_refused():
    with pytest.raises(demo_tape.TapeError, match="does not accept"):
        demo_tape.build_tape(_run([_entry("odd.jpg", "CC BY-SA 4.0", "complete")]))


def test_check_tape_refuses_an_image_on_a_non_cleared_record():
    tape, files = demo_tape.build_tape(_run([_entry("bysa.jpg", "CC BY-SA 4.0", "quarantined")]))
    bad = copy.deepcopy(tape)
    bad["records"][0]["image"] = {"src": "thumbs/x.webp", "width": 1, "height": 1}
    with pytest.raises(demo_tape.TapeError, match="has an image"):
        demo_tape.check_tape(bad, files)
    with pytest.raises(demo_tape.TapeError, match="has an image"):
        demo_tape.check_tape(tape, {tape["records"][0]["id"]: "x.webp"})


def test_check_tape_refuses_an_image_with_no_record():
    tape, files = demo_tape.build_tape(_run([_entry("pd.jpg", "Public domain", "complete")]))
    with pytest.raises(demo_tape.TapeError, match="no record"):
        demo_tape.check_tape(tape, {**files, "stale-id": "x.webp"})


def test_identity_subjects_and_entries_are_refused():
    identity = [{"name": "Someone", "folder": "Someone", "category": "identity", "search": None}]
    with pytest.raises(demo_tape.TapeError, match="identity"):
        demo_tape.build_tape(_run([], identity))
    with pytest.raises(demo_tape.TapeError, match="identity"):
        demo_tape.build_tape(_run([_entry("p.jpg", "Public domain", "complete", category="identity")]))


def test_every_candidate_is_kept_quarantined_included():
    entries = [_entry(f"q{i}.jpg", "CC BY-SA 4.0", "quarantined") for i in range(3)] + [
        _entry("pd.jpg", "Public domain", "complete")
    ]
    tape, _ = demo_tape.build_tape(_run(entries))
    assert len(tape["records"]) == 4
    assert tape["summary"]["by_verdict"]["quarantined"] == 3
    assert "excluded" not in tape["recording"]


def test_strip_paths_replaces_prefixes_longest_first():
    run = {"a": ["/data/x/cache/home/images/a.jpg", {"b": "decode: /data/x/cache/tmp.jpg"}], "n": 3}
    out = demo_tape.strip_paths(run, {"/data/x/cache": "$QUARRY_DEMO_CACHE", "/data/x/cache/home": "$QUARRY_HOME"})
    assert out == {"a": ["$QUARRY_HOME/images/a.jpg", {"b": "decode: $QUARRY_DEMO_CACHE/tmp.jpg"}], "n": 3}


def test_email_addresses_in_credit_lines_are_redacted_not_dropped():
    entry = _entry(
        "pd.jpg", "CC0", "complete", attribution="Jane Doe &lt;jane@example.org&gt;", photographer="jane@example.org"
    )
    tape, files = demo_tape.build_tape(_run([entry]))
    prov = tape["records"][0]["provenance"]
    assert prov["attribution"] == "Jane Doe <[email address removed]>"
    assert prov["photographer"] == "[email address removed]"
    assert len(files) == 1


def test_pool_paths_are_relative_to_quarry_home():
    tape, _ = demo_tape.build_tape(_run([_entry("pd.jpg", "Public domain", "complete")]))
    assert tape["records"][0]["provenance"]["path"] == "$QUARRY_HOME/images/concept/Aurora_borealis/a.jpg"


@pytest.mark.parametrize(
    "leak",
    ["/home/someone/quarry", "/Users/someone/x", "192.168.1.20", "10.0.0.5", "someone@example.com", "/mnt/nas/pool"],
)
def test_a_leak_anywhere_fails_the_build(leak):
    entry = _entry("pd.jpg", "Public domain", "failed")
    entry["outcome"]["reason"] = f"decode: cannot open {leak}"
    with pytest.raises(demo_tape.TapeError, match="would publish"):
        demo_tape.build_tape(_run([entry]))


def test_the_committed_tape_keeps_the_rules():
    tape_dir = ROOT / "web" / "public" / "tape"
    if not (tape_dir / "tape.json").exists():
        pytest.skip("no committed tape")
    tape = json.loads((tape_dir / "tape.json").read_text(encoding="utf-8"))
    files = {r["id"]: r["image"]["src"] for r in tape["records"] if r["image"]}
    demo_tape.check_tape(tape, files)
    on_disk = {p.name for p in (tape_dir / "thumbs").iterdir()}
    assert on_disk == {f"{rid}.webp" for rid in files}
    assert all(r["category"] != "identity" for r in tape["records"])
