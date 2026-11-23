"""The film/digital set's report (#55): stratum reweighting, unsure handling, and reading the page's store."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
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
    # Each (era, band) stratum fills its quota, minus a recorded shortfall. A sample that is
    # entirely one band would still pass the checks above.
    counts = Counter(f"{it['clip_era']}|{it['band']}" for it in sample["items"])
    quota, short = sample["quota"], sample.get("short") or {}
    expected = {f"{era}|{band}" for era in quota for band, _, _ in fds.BANDS}
    assert set(counts) == expected
    for stratum in expected:
        era = stratum.split("|", 1)[0]
        assert counts[stratum] == quota[era] - short.get(stratum, 0)
    template = (ROOT / "scripts" / "film_digital_labeller.template.html").read_text()
    assert "clip_era" not in template and "conf" not in template.lower().replace(
        "confidence", ""
    )  # the page stays blind


@pytest.mark.parametrize(
    ("make", "model", "verdict"),
    [
        ("Nikon", "Nikon SUPER COOLSCAN 9000 ED", "film"),
        ("EPSON", "Perfection V800/V850", "film"),
        ("NORITSU KOKI", "EZ Controller", "film"),
        ("HP", "HP Scanjet G2710", "film"),
        ("NIKON", "COOLPIX AW100", "digital"),
        ("Apple", "iPhone 8", "digital"),
        ("", "", None),  # no device named: no reference, never a guess
    ],
)
def test_the_exif_device_decides_film_or_digital(make, model, verdict):
    assert fds.exif_verdict(make, model) == verdict


def test_the_script_and_ingest_share_one_scanner_rule():
    """#64: the device rule lives in the package; the #55 script only renames ``scan`` to ``film``."""
    from argus_quarry import exif

    assert not hasattr(fds, "SCANNER")
    assert fds.capture_medium is exif.capture_medium


def test_agreement_scores_a_rater_on_the_items_the_reference_covers():
    reference = {"a": "digital", "b": "film", "c": "digital"}
    rater = {"a": "film", "b": "film", "c": "unsure", "d": "digital"}  # d: not in the reference
    got = fds.agreement(reference, rater)
    assert got["compared"] == 2 and got["agree"] == 1 and got["rater_unsure"] == 1
    assert got["pairs"] == {"reference digital / rater film": 1, "reference film / rater film": 1}


def test_worst_case_counts_uncovered_items_as_film(tmp_path):
    sample = _sample({"2000-now|ge0.7": 4}, {"2000-now|ge0.7": 100})
    (tmp_path / "s.json").write_text(json.dumps(sample))
    (tmp_path / "ref.json").write_text(
        json.dumps({"labels": {"2000-now|ge0.7#0": "digital", "2000-now|ge0.7#1": "digital"}})
    )
    args = type(
        "A", (), {"sample": tmp_path / "s.json", "labels": tmp_path / "ref.json", "worst_case": True, "compare": None}
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fds.cmd_report(args)
    assert json.loads(buf.getvalue())["precision_2000_now"]["conf>=0.7"]["digital_share"] == 0.5


def test_rejected_and_failed_originals_are_deleted(tmp_path, monkeypatch):
    """A greyscale reject and an unreadable file must not leave the downloaded original on disk."""
    from PIL import Image

    grey = tmp_path / "grey.jpg"
    Image.new("RGB", (8, 8), (40, 40, 40)).save(grey)
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not an image")
    blobs = {"https://x/grey": grey.read_bytes(), "https://x/bad": bad.read_bytes()}
    candidates = [
        {"id": "grey", "url": "https://x/grey", "clip_era": "2000-now", "band": "ge0.7", "collection": "c"},
        {"id": "bad", "url": "https://x/bad", "clip_era": "2000-now", "band": "ge0.7", "collection": "c"},
    ]
    monkeypatch.setattr(fds, "draw", lambda *_a, **_k: (candidates, {"2000-now|ge0.7": 10}))

    class _Count:
        def fetchone(self):
            return (0,)

    monkeypatch.setattr(fds.duckdb, "execute", lambda *_a, **_k: _Count())

    class _Net:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def download(self, url, dest, **_k):
            dest.write_bytes(blobs[url])

    monkeypatch.setattr("argus_quarry.net.NetClient", lambda _config: _Net())
    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "q"))
    thumbs = tmp_path / "thumbs"
    out = tmp_path / "sample.json"
    args = type(
        "A", (), {"index": tmp_path / "idx", "scores": tmp_path / "s.parquet", "seed": 1, "thumbs": thumbs, "out": out}
    )
    fds.cmd_sample(args)
    originals = thumbs / "originals"
    assert not (originals / "grey.img").exists()
    assert not (originals / "bad.img").exists()
    assert json.loads(out.read_text())["rejected"] == {"greyscale": 1, "unreadable": 1}


def test_thumbnails_apply_exif_orientation(tmp_path):
    from PIL import Image

    src = tmp_path / "sideways.jpg"
    dest = tmp_path / "thumb.jpg"
    im = Image.new("RGB", (40, 20), (180, 20, 20))
    exif = Image.Exif()
    exif[0x0112] = 6  # the stored pixels are rotated; display is the transpose
    im.save(src, exif=exif)
    fds.thumbnail(src, dest)
    with Image.open(dest) as out:
        assert out.size[1] > out.size[0]


def test_worst_case_agreement_uses_the_real_reference_labels(tmp_path):
    """--worst-case fills uncovered items as film for precision only, not for rater agreement."""
    import contextlib
    import io

    sample = _sample({"2000-now|ge0.7": 2}, {"2000-now|ge0.7": 10})
    (tmp_path / "s.json").write_text(json.dumps(sample))
    (tmp_path / "ref.json").write_text(json.dumps({"labels": {"2000-now|ge0.7#0": "digital"}}))
    other = tmp_path / "rater.json"
    other.write_text(json.dumps({"labels": {"2000-now|ge0.7#0": "digital", "2000-now|ge0.7#1": "digital"}}))
    args = type(
        "A",
        (),
        {
            "sample": tmp_path / "s.json",
            "labels": tmp_path / "ref.json",
            "worst_case": True,
            "compare": [other],
        },
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fds.cmd_report(args)
    got = json.loads(buf.getvalue())
    assert got["precision_2000_now"]["conf>=0.7"]["digital_share"] == 0.5  # the uncovered item counted as film
    agreement = got["agreement"]["rater.json"]
    assert agreement["compared"] == 1 and agreement["agree"] == 1  # item #1 was never a reference label
