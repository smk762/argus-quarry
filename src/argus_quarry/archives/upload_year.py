"""Approximate Flickr **upload** year from a photo id (#53).

Flickr assigns photo ids in increasing order as photos are uploaded, so a short table of
``(photo_id, posted)`` breakpoints maps any id onto an approximate upload time. Megalith
publishes no embeddings (no ``clip_era``), but every item has a Flickr id, so this dates all
9.58M items at index time without a request.

It is the **upload** date, never the capture date: Flickr Commons institutions upload
19th-century photographs today, and scanned film is uploaded long after it was shot. Every
name that carries it says ``upload`` (``upload_year``, ``date_sources: flickr-upload``).

The table is committed (``data/flickr_upload_breakpoints.json``) with where its dates came
from and the error measured on held-out ids; ``scripts/calibrate_flickr_upload.py`` rebuilds it.
Ids outside the measured range get no year rather than an extrapolated one.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

BREAKPOINTS_FILE = "flickr_upload_breakpoints.json"
#: Bumped when the file's shape changes.
BREAKPOINTS_SCHEMA = 1
#: Where the column says its date came from (facets' ``date_sources``).
DATE_SOURCE = "flickr-upload"

Sample = tuple[int, int]  # (photo_id, posted as unix seconds, UTC)


def breakpoints_path() -> Path:
    return Path(__file__).parent / "data" / BREAKPOINTS_FILE


@cache
def load_breakpoints(path: Path | None = None) -> dict[str, Any] | None:
    """The committed table, or None when it is absent or from another schema."""
    try:
        data = json.loads((path or breakpoints_path()).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != BREAKPOINTS_SCHEMA or len(data.get("points") or []) < 2:
        return None
    return data


# ── fitting (calibration time) ──────────────────────────────────────────
def fit_breakpoints(samples: Sequence[Sample], *, bin_size: int = 9) -> list[Sample]:
    """Monotone breakpoints from ``(id, posted)`` samples, robust to backdated outliers.

    Sorted by id, consecutive runs of ``bin_size`` samples collapse to their median id and
    median time, so one photo whose owner edited its posted date can't drag a breakpoint.
    The times are then made non-decreasing (ids only grow with upload time). The first and
    last ids are kept as the ends, so the table covers the full measured id range, but a raw
    end's time is not trusted: it is pinned to the neighbouring bin median, so a backdated
    first sample cannot pull every id below that median toward an earlier year. Each id
    appears once.
    """
    pts = _one_time_per_id(samples)
    if len(pts) < 2:
        raise ValueError("need at least two samples to fit breakpoints")
    step = max(1, bin_size)
    bins = [pts[i : i + step] for i in range(0, len(pts), step)]
    medians: list[Sample] = [
        (int(statistics.median(p[0] for p in chunk)), int(statistics.median(p[1] for p in chunk))) for chunk in bins
    ]
    # A truncated median id can land on an end id. Keep the median time, not the raw sample.
    by_id: dict[int, int] = {}
    for pid, posted in medians:
        by_id.setdefault(pid, posted)
    median_ids = set(by_id)
    start_id, start_t = pts[0]
    end_id, end_t = pts[-1]
    if start_id not in by_id:
        by_id[start_id] = start_t
    if end_id not in by_id:
        by_id[end_id] = end_t
    out = sorted(by_id.items())
    # Pin a raw end to the neighbouring median. An end that is itself a median already holds that time.
    if len(out) >= 2 and out[0][0] not in median_ids:
        out[0] = (out[0][0], out[1][1])
    if len(out) >= 2 and out[-1][0] not in median_ids:
        out[-1] = (out[-1][0], out[-2][1])
    fixed: list[Sample] = []
    for pid, t in out:
        fixed.append((pid, max(t, fixed[-1][1]) if fixed else t))
    return fixed


def _one_time_per_id(samples: Sequence[Sample]) -> list[Sample]:
    """One posted time per photo id (the median when the same id was sampled more than once)."""
    by_id: dict[int, list[int]] = {}
    for pid, posted in samples:
        by_id.setdefault(int(pid), []).append(int(posted))
    return [(pid, int(statistics.median(times))) for pid, times in sorted(by_id.items())]


def posted_at(points: Sequence[Sample], photo_id: int) -> float | None:
    """Interpolated upload time (unix seconds) for ``photo_id``; None outside the table's id range."""
    if not points or photo_id < points[0][0] or photo_id > points[-1][0]:
        return None
    ids = [p[0] for p in points]
    i = bisect.bisect_right(ids, photo_id)
    if i >= len(points):
        return float(points[-1][1])
    (a_id, a_t), (b_id, b_t) = points[i - 1], points[i]
    if b_id == a_id:
        return float(a_t)
    return a_t + (b_t - a_t) * (photo_id - a_id) / (b_id - a_id)


def upload_year(points: Sequence[Sample], photo_id: int) -> int | None:
    t = posted_at(points, photo_id)
    return None if t is None else datetime.fromtimestamp(t, UTC).year


def measure(points: Sequence[Sample], held_out: Sequence[Sample]) -> dict[str, Any]:
    """Error of the table on samples it was not fitted on: days off, and how often the year is exact."""
    errors: list[float] = []
    exact = within_one = 0
    for pid, t in held_out:
        est = posted_at(points, pid)
        if est is None:
            continue
        errors.append(abs(est - t) / 86400)
        y_est, y_true = datetime.fromtimestamp(est, UTC).year, datetime.fromtimestamp(t, UTC).year
        exact += y_est == y_true
        within_one += abs(y_est - y_true) <= 1
    n = len(errors)
    if not n:
        return {"n": 0}
    errors.sort()

    def q(p: float) -> float:
        return round(errors[min(n - 1, int(p * n))], 1)

    return {
        "n": n,
        "outside_range": len(held_out) - n,
        "median_abs_days": q(0.5),
        "p90_abs_days": q(0.9),
        "p99_abs_days": q(0.99),
        "max_abs_days": round(errors[-1], 1),
        "year_exact": round(exact / n, 4),
        "year_within_1": round(within_one / n, 4),
    }


# ── index time ──────────────────────────────────────────────────────────
def year_thresholds(points: Sequence[Sample]) -> list[tuple[int, int]]:
    """``(year, first id uploaded in that year)`` for each year the table spans, by inverse interpolation."""
    if len(points) < 2:
        return []
    first = datetime.fromtimestamp(points[0][1], UTC).year
    last = datetime.fromtimestamp(points[-1][1], UTC).year
    out = [(first, points[0][0])]
    for year in range(first + 1, last + 1):
        boundary = datetime(year, 1, 1, tzinfo=UTC).timestamp()
        for (a_id, a_t), (b_id, b_t) in zip(points, points[1:], strict=False):
            if a_t < boundary <= b_t:
                # The first whole id at or past the boundary: truncating would date the id before it into this year.
                out.append((year, math.ceil(a_id + (b_id - a_id) * (boundary - a_t) / (b_t - a_t))))
                break
    return out


def upload_year_sql(id_expr: str, points: Sequence[Sample] | None = None) -> str:
    """A DuckDB expression giving the upload year of the Flickr photo id ``id_expr`` (a BIGINT), or NULL.

    Built from the committed table's per-year thresholds, so it is a short CASE ladder with no
    join. Without a table, or for an id outside the measured range, it is NULL.
    """
    if points is None:
        table = load_breakpoints()
        points = [tuple(p) for p in table["points"]] if table else []
    thresholds = year_thresholds(points)
    if not thresholds:
        return "CAST(NULL AS INTEGER)"
    lo, hi = points[0][0], points[-1][0]
    whens = " ".join(f"WHEN {id_expr} >= {first_id} THEN {year}" for year, first_id in reversed(thresholds))
    return f"CASE WHEN {id_expr} IS NULL OR {id_expr} < {lo} OR {id_expr} > {hi} THEN NULL {whens} END"
