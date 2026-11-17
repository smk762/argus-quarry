"""The demo recorder (scripts/record_tape.py): budget stops, skip attribution, thumbs and --out safety."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("record_tape", ROOT / "scripts" / "record_tape.py")
record_tape = importlib.util.module_from_spec(_spec)
sys.modules["record_tape"] = record_tape
_spec.loader.exec_module(record_tape)

from argus_quarry.ingest import BudgetReached, IngestOutcome  # noqa: E402


def _skip(title: str) -> dict:
    return {"query": "q", "rank": 1, "mime": "image/tiff", "title": title, "licence": "CC0"}


def _rec(name: str, category: str = "concept") -> SimpleNamespace:
    return SimpleNamespace(
        remote_url=f"https://example.org/{name}.jpg",
        subject=name,
        category=category,
        title=name,
        source_url=f"https://example.org/{name}",
        licence="CC0",
        attribution=None,
        photographer=None,
        year=None,
    )


def test_record_stops_at_the_budget_and_files_skips_under_their_own_subject(tmp_path, monkeypatch):
    subjects = tmp_path / "subjects.yaml"  # outside the repo, too
    subjects.write_text(
        "subjects:\n" + "".join(f"  - {{name: {n}, category: concept}}\n" for n in ("Alpha", "Beta", "Gamma", "Delta"))
    )
    harvested: list[str] = []

    def harvest(self, subject, limit):
        harvested.append(subject.name)
        if subject.name == "Alpha":
            self.skipped.append(_skip("a1"))
            yield _rec("Alpha")
            self.skipped.append(_skip("a2"))  # after the subject's last record
        elif subject.name == "Beta":
            self.skipped.append(_skip("b1"))  # a subject that yields nothing
        else:
            yield _rec(subject.name)

    def ingest(self, rec):
        if rec.subject == "Gamma":
            raise BudgetReached
        return IngestOutcome(status="quarantined", reason="test")

    monkeypatch.setattr(record_tape.ObservedCommons, "harvest", harvest)
    monkeypatch.setattr(record_tape.ObservedEngine, "ingest_record", ingest)
    monkeypatch.setattr(record_tape, "_versions", lambda: {})
    cache = tmp_path / "cache"
    record_tape.record(argparse.Namespace(cache=cache, subjects=subjects, limit=5, fresh=False))

    run = json.loads((cache / "run.json").read_text())
    assert "Delta" not in harvested  # the budget stopped the run, not just Gamma
    by_title = {e["title"]: e["subject"] for e in run["entries"] if e["stage"] == "search"}
    assert by_title == {"a1": "Alpha", "a2": "Alpha", "b1": "Beta"}
    assert run["rerun"].get("budget_reached") == 1
    assert run["subjects_file"] == str(subjects)


def test_thumb_applies_exif_orientation(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW to display
    buf = io.BytesIO()
    Image.new("RGB", (200, 100)).save(buf, "JPEG", exif=exif)
    src = tmp_path / "sideways.jpg"
    src.write_bytes(buf.getvalue())
    assert record_tape._thumb(src, tmp_path / "t.webp") == {"width": 100, "height": 200}


@pytest.mark.parametrize("which", ["cache", "parent"])
def test_build_refuses_an_out_that_would_wipe_the_cache(tmp_path, which):
    cache = tmp_path / "cache"
    (cache / "thumbs").mkdir(parents=True)
    (cache / "run.json").write_text("{}")
    out = cache if which == "cache" else tmp_path
    with pytest.raises(SystemExit):
        record_tape.build(argparse.Namespace(cache=cache, out=out))
    assert (cache / "run.json").exists()
