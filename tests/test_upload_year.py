"""Flickr id -> approximate upload year (#53): the fit, the measurement, and the index column."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from argus_quarry.archives import upload_year as uy


def _t(year: int, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


#: A clean, linear-ish id/time line: id 1000*k uploaded at the start of 2005+k.
LINE = [(1000 * k, _t(2005 + k)) for k in range(0, 11)]


def test_interpolation_between_breakpoints_and_nothing_outside_them():
    assert uy.upload_year(LINE, 0) == 2005
    assert uy.upload_year(LINE, 1500) == 2006  # halfway through 2006
    assert uy.upload_year(LINE, 9999) == 2014
    assert uy.upload_year(LINE, 10_000) == 2015
    assert uy.upload_year(LINE, -1) is None and uy.upload_year(LINE, 10_001) is None  # never extrapolated


def test_a_backdated_photo_cannot_bend_the_table():
    samples = [(i * 100, _t(2005) + i * 86400 * 30) for i in range(60)]
    outlier = (3050, _t(1999))  # an owner set its posted date years back
    points = uy.fit_breakpoints([*samples, outlier], bin_size=9)
    assert [t for _, t in points] == sorted(t for _, t in points)  # monotone
    assert uy.upload_year(points, 3050) == uy.upload_year(uy.fit_breakpoints(samples, bin_size=9), 3050)


def test_an_outlier_at_either_end_is_clamped_into_the_curve():
    samples = [(i * 100, _t(2010) + i * 86400 * 30) for i in range(1, 40)]
    points = uy.fit_breakpoints([(0, _t(2030)), *samples, (10_000, _t(2001))], bin_size=5)
    assert [t for _, t in points] == sorted(t for _, t in points)
    assert points[0][0] == 0 and points[-1][0] == 10_000  # the ends still bound the covered range
    assert uy.upload_year(points, 0) <= 2010 and uy.upload_year(points, 10_000) >= 2013


def test_measure_reports_days_and_year_accuracy_on_held_out_samples():
    held_out = [(500, _t(2005, 7, 2)), (2500, _t(2007, 7, 2) + 86400 * 10), (99_999, _t(2020))]
    m = uy.measure(LINE, held_out)
    assert m["n"] == 2 and m["outside_range"] == 1
    assert m["median_abs_days"] <= 10.5 and m["max_abs_days"] == pytest.approx(10, abs=1)
    assert m["year_exact"] == 1.0 and m["year_within_1"] == 1.0


#: Year boundaries that fall between whole ids, as real tables' do.
SKEWED = [(9000, _t(2010, 6)), (9002, _t(2011, 6)), (9004, _t(2012, 6)), (9_900_017, _t(2019, 3, 7))]


@pytest.mark.parametrize("points", [LINE, SKEWED], ids=["line", "skewed"])
def test_the_sql_ladder_agrees_with_the_python_lookup(points):
    duckdb = pytest.importorskip("duckdb")
    expr = uy.upload_year_sql("i", points)
    lo, hi = points[0][0], points[-1][0]
    probe = sorted({lo - 1, lo, hi, hi + 1, *range(lo, min(hi, lo + 3000)), *range(lo, hi, max(1, (hi - lo) // 5000))})
    got = dict(duckdb.connect().sql(f"SELECT i, {expr} FROM unnest({probe}::BIGINT[]) t(i)").fetchall())
    assert got == {i: uy.upload_year(points, i) for i in probe}


def test_no_table_means_no_upload_year():
    assert uy.upload_year_sql("i", []) == "CAST(NULL AS INTEGER)"


def test_a_foreign_or_short_table_is_not_loaded(tmp_path: Path):
    uy.load_breakpoints.cache_clear()
    bad = tmp_path / "t.json"
    for payload in ({"schema": 99, "points": LINE}, {"schema": uy.BREAKPOINTS_SCHEMA, "points": [[1, 2]]}, []):
        bad.write_text(json.dumps(payload))
        assert uy.load_breakpoints(bad) is None
        uy.load_breakpoints.cache_clear()
    assert uy.load_breakpoints(tmp_path / "missing.json") is None


def test_the_committed_table_is_monotone_measured_and_says_upload():
    table = uy.load_breakpoints()
    if table is None:
        pytest.skip("no committed breakpoint table")
    points = [tuple(p) for p in table["points"]]
    assert [p[0] for p in points] == sorted(p[0] for p in points)
    assert [p[1] for p in points] == sorted(p[1] for p in points)
    assert "UPLOAD" in table["what"] and table["source"]
    held = table["held_out"]
    assert held["n"] >= 100 and 0 <= held["year_exact"] <= held["year_within_1"] <= 1  # measured, not asserted


def test_megalith_items_carry_an_upload_year_and_filter_on_it(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from argus_quarry.archives import ArchiveQuery, get_archive
    from argus_quarry.config import QuarryConfig

    points = [[9000, _t(2010, 6)], [9002, _t(2011, 6)], [9004, _t(2012, 6)]]
    monkeypatch.setattr(uy, "load_breakpoints", lambda path=None: {"schema": 1, "points": points})
    root = _megalith(tmp_path)
    mg = get_archive("megalith", QuarryConfig(home=tmp_path / "q", archive_dirs={"megalith": root}))
    mg.build_index()
    years = {i["id"]: i["upload_year"] for i in mg.search(ArchiveQuery(), limit=10)["items"]}
    assert years == {"9001": 2010, "9002": 2011, "9003": 2011}
    assert {i["id"] for i in mg.search(ArchiveQuery(upload_year_min=2011), limit=10)["items"]} == {"9002", "9003"}
    assert mg.count(ArchiveQuery(upload_year_max=2010)) == 1
    facets = mg.facets()
    assert facets["date_sources"]["upload_year"] == "flickr-upload"
    assert facets["upload_dated"] == 3 and facets["upload_year_range"] == [2010, 2011]


def _megalith(tmp_path: Path) -> Path:
    """A Megalith archive of three links (ids 9001-9003), no captions or sizes."""
    import duckdb

    root = tmp_path / "megalith"
    links = [
        {
            "url": f"https://live.staticflickr.com/1/{pid}_s.jpg",
            "url_highres": f"https://live.staticflickr.com/1/{pid}_b.jpg",
            "url_source": f"https://www.flickr.com/photos/{owner}/{pid}",
        }
        for owner, pid in (("111@N01", "9001"), ("111@N01", "9002"), ("222@N02", "9003"))
    ]
    out = root / "links" / "data" / "megalith-00001-of-00010.parquet"
    out.parent.mkdir(parents=True)
    values = ", ".join("(?, ?, ?)" for _ in links)
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {values}) t(url, url_highres, url_source)) TO '{out}' (FORMAT parquet)",
        [v for row in links for v in row.values()],
    )
    return root
