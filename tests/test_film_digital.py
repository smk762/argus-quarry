"""The film/digital set's report (#55): stratum reweighting, unsure handling, and reading the page's store."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("film_digital_set", ROOT / "scripts" / "film_digital_set.py")
fds = importlib.util.module_from_spec(_spec)
sys.modules["film_digital_set"] = fds
_spec.loader.exec_module(fds)


def _sample(strata: dict[str, int], population: dict[str, int]) -> dict:
    items = []
    for stratum, n in strata.items():
        era, band = stratum.split("|")
        items += [{"id": f"{stratum}#{i}", "clip_era": era, "band": band} for i in range(n)]
    return {"items": items, "population": population}


def test_precision_is_reweighted_by_each_bands_real_size():
    # 10 labelled per band; the confident band is 8x the size of the others in the archive.
    sample = _sample(
        {"2000-now|lt0.5": 10, "2000-now|0.5-0.7": 10, "2000-now|ge0.7": 10},
        {"2000-now|lt0.5": 100, "2000-now|0.5-0.7": 100, "2000-now|ge0.7": 800},
    )
    labels = {}
    for stratum, digital in (("2000-now|lt0.5", 5), ("2000-now|0.5-0.7", 8), ("2000-now|ge0.7", 10)):
        for i in range(10):
            labels[f"{stratum}#{i}"] = "digital" if i < digital else "film"
    rep = fds.report(sample, labels)
    p = rep["precision_2000_now"]
    assert p["conf>=0.7"]["digital_share"] == 1.0
    assert p["conf>=0.5"]["digital_share"] == pytest.approx((0.8 * 100 + 1.0 * 800) / 900, abs=1e-4)
    assert p["conf>=0"]["digital_share"] == pytest.approx((0.5 * 100 + 0.8 * 100 + 800) / 1000, abs=1e-4)
    assert p["conf>=0"]["labelled"] == 30 and rep["labelled"] == 30


def test_unsure_is_reported_apart_and_never_counted_either_way():
    sample = _sample({"2000-now|ge0.7": 4}, {"2000-now|ge0.7": 1000})
    labels = {"2000-now|ge0.7#0": "digital", "2000-now|ge0.7#1": "film", "2000-now|ge0.7#2": "unsure"}  # #3 unlabelled
    p = fds.report(sample, labels)["precision_2000_now"]["conf>=0.7"]
    assert p["digital_share"] == 0.5 and p["decided"] == 2 and p["labelled"] == 3
    assert p["unsure_share"] == pytest.approx(1 / 3, abs=1e-4)


def test_no_labels_yet_reports_nothing_rather_than_zero():
    sample = _sample({"2000-now|ge0.7": 2}, {"2000-now|ge0.7": 10})
    p = fds.report(sample, {})["precision_2000_now"]["conf>=0.7"]
    assert p["digital_share"] is None and p["labelled"] == 0


def test_the_store_export_keeps_only_this_sets_valid_labels(tmp_path):
    sample = _sample({"2000-now|ge0.7": 2}, {"2000-now|ge0.7": 10})
    (tmp_path / "s.json").write_text(json.dumps(sample))
    export = tmp_path / "export" / "labels"
    export.mkdir(parents=True)
    (export / "2000-now|ge0.7#0.json").write_text(json.dumps({"label": "film", "by": "u", "at": "t"}))
    (export / "probe-check.json").write_text(json.dumps({"label": "unsure"}))  # not in the set
    (export / "2000-now|ge0.7#1.json").write_text(json.dumps({"label": "maybe"}))  # not a label
    out = tmp_path / "labels.json"
    args = type("A", (), {"sample": tmp_path / "s.json", "export": tmp_path / "export", "out": out})
    fds.cmd_labels(args)
    assert json.loads(out.read_text()) == {"labels": {"2000-now|ge0.7#0": "film"}}
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"id": "2000-now|ge0.7#1", "data": {"label": "digital"}}]))
    assert fds.read_store_export(rows) == {"2000-now|ge0.7#1": "digital"}


def test_the_committed_sample_is_blind_balanced_and_iNaturalist_free():
    path = ROOT / "docs" / "film-digital" / "sample.json"
    if not path.is_file():
        pytest.skip("no committed sample")
    sample = json.loads(path.read_text())
    assert all(it["collection"] != "iNaturalist" for it in sample["items"])
    assert len({it["id"] for it in sample["items"]}) == len(sample["items"]) >= 280
    template = (ROOT / "scripts" / "film_digital_labeller.template.html").read_text()
    assert "clip_era" not in template and "conf" not in template.lower().replace(
        "confidence", ""
    )  # the page stays blind
