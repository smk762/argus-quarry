"""The Flickr upload-year calibration's provenance label follows this run's sample."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "calibrate_flickr_upload", ROOT / "scripts" / "calibrate_flickr_upload.py"
)
calibrate_flickr_upload = importlib.util.module_from_spec(_spec)
sys.modules["calibrate_flickr_upload"] = calibrate_flickr_upload
_spec.loader.exec_module(calibrate_flickr_upload)


def _run(tmp_path, monkeypatch, capsys, answers: dict[int, dict]) -> dict:
    from argus_quarry.config import QuarryConfig

    script = calibrate_flickr_upload
    monkeypatch.setattr(script, "sample_ids", lambda *_a, **_k: [(1, "alice"), (2, "bob")])
    monkeypatch.setattr(script, "sample_low_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(script, "fetch", lambda _ids, _cache, _dates: answers)

    class _Net:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

    monkeypatch.setattr(script, "NetClient", lambda *_a, **_k: _Net())
    monkeypatch.setattr(QuarryConfig, "from_env", staticmethod(lambda: QuarryConfig(home=tmp_path)))
    script.main(
        [
            "--links",
            "unused",
            "--cache",
            str(tmp_path / "cache.jsonl"),
            "--samples",
            "2",
            "--low-samples",
            "0",
            "--bin-size",
            "1",
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_source_label_uses_only_this_runs_sample(tmp_path, monkeypatch, capsys):
    table = _run(
        tmp_path,
        monkeypatch,
        capsys,
        {
            1: {"id": 1, "posted": 1_300_000_000, "via": "api"},
            2: {"id": 2, "posted": 1_400_000_000, "via": "api"},
            99: {"id": 99, "posted": 1_000_000_000, "via": "page"},  # a leftover row from an earlier cache
        },
    )
    assert table["source"] == "flickr.photos.getInfo dates.posted"
    assert table["sources"] == {"api": 2}


def test_a_run_dated_through_both_paths_names_both(tmp_path, monkeypatch, capsys):
    table = _run(
        tmp_path,
        monkeypatch,
        capsys,
        {
            1: {"id": 1, "posted": 1_300_000_000, "via": "api"},
            2: {"id": 2, "posted": 1_400_000_000, "via": "page"},  # the API failed over to the page for this one
        },
    )
    assert table["source"] == "flickr.photos.getInfo dates.posted + Flickr photo pages (datePosted)"
    assert table["sources"] == {"api": 1, "page": 1}


def test_the_label_never_names_a_path_that_dated_nothing():
    from collections import Counter

    label = calibrate_flickr_upload.source_label
    assert label(Counter()) == "nothing dated"
    assert label(Counter({"page": 3})) == "Flickr photo pages (datePosted)"
