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


def test_source_label_uses_only_this_runs_sample(tmp_path, monkeypatch, capsys):
    from argus_quarry.config import QuarryConfig

    script = calibrate_flickr_upload
    monkeypatch.setattr(script, "sample_ids", lambda *_a, **_k: [(1, "alice"), (2, "bob")])
    monkeypatch.setattr(script, "sample_low_ids", lambda *_a, **_k: [])

    def fetch(_ids, _cache, _dates):
        return {
            1: {"id": 1, "posted": 1_300_000_000, "via": "api"},
            2: {"id": 2, "posted": 1_400_000_000, "via": "api"},
            99: {"id": 99, "posted": 1_000_000_000, "via": "page"},  # a leftover row from an earlier cache
        }

    monkeypatch.setattr(script, "fetch", fetch)

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
    table = json.loads(capsys.readouterr().out)
    assert table["source"] == "flickr.photos.getInfo dates.posted"
