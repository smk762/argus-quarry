"""Build and measure the Flickr id -> upload-year table (#53) from real upload dates.

    uv run --no-sync python scripts/calibrate_flickr_upload.py \\
        --links "$QUARRY_MEGALITH_DIR/links/data/*.parquet" \\
        --cache flickr-upload-samples.jsonl [--samples 700] [--rate 1.0] [--write]

Samples Megalith photo ids evenly across the id range (one per equal-count stratum, plus the
smallest and largest id), and asks Flickr when each was uploaded:

- with ``QUARRY_FLICKR_API_KEY`` set, through ``flickr.photos.getInfo`` (``dates.posted``);
- without it, from the public photo page's embedded ``datePosted``.

Answers are appended to ``--cache`` as they come, so a run that stops resumes where it left
off and a re-run re-fits without re-asking Flickr. Sorted by id, alternate samples go to the
fit half and the held-out half. The table is fitted on the fit half only, and every error
reported is from the held-out half, so the committed error is the committed table's.

``--write`` stores the table in ``archives/data/flickr_upload_breakpoints.json``; then rebuild
the Megalith index (``argus-quarry archives index megalith``) to fill ``upload_year``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from argus_quarry.archives import upload_year as uy
from argus_quarry.archives.flickr_dates import API_RATE_SOURCE, PAGE_RATE_SOURCE, FlickrDateError, FlickrDates
from argus_quarry.config import QuarryConfig, SourceRate
from argus_quarry.net import NetClient

ID_RE = r"/photos/[^/]+/([0-9]+)"
OWNER_RE = r"/photos/([^/]+)/"


def sample_low_ids(links: str, n: int, seed: int, quantile: float = 0.05) -> list[tuple[int, str]]:
    """``n`` more ids from log-spaced bands below the id ``quantile``.

    Equal-count strata put few samples where Flickr's early ids are sparse, and ids grew
    roughly exponentially then, so one linear segment would misdate 2004-2009. Log-spaced
    bands give those years breakpoints of their own.
    """
    if n <= 0:
        return []
    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'")
    rows = con.execute(
        f"""
        WITH ids AS (
            SELECT TRY_CAST(regexp_extract(url_source, '{ID_RE}', 1) AS BIGINT) AS id,
                   regexp_extract(url_source, '{OWNER_RE}', 1) AS owner
            FROM read_parquet(?)
        ), bounds AS (
            SELECT ln(greatest(min(id), 1)) AS lo, ln(quantile_cont(id, ?)) AS hi FROM ids WHERE id IS NOT NULL
        ), banded AS (
            SELECT id, owner, least(?::INTEGER - 1, floor(? * (ln(greatest(id, 1)) - lo) / (hi - lo)))::INTEGER AS band
            FROM ids, bounds WHERE id IS NOT NULL AND ln(greatest(id, 1)) <= hi
        )
        SELECT id, owner FROM banded QUALIFY row_number() OVER (PARTITION BY band ORDER BY hash(id, ?::BIGINT)) = 1
        """,
        [links, quantile, n, n, seed],
    ).fetchall()
    return sorted((int(i), o) for i, o in rows)


def sample_ids(links: str, n: int, seed: int) -> list[tuple[int, str]]:
    """``n`` ``(photo_id, owner)`` pairs, one per equal-count id stratum (picked by ``hash(id, seed)``), plus both ends."""
    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'")
    rows = con.execute(
        f"""
        WITH ids AS (
            SELECT TRY_CAST(regexp_extract(url_source, '{ID_RE}', 1) AS BIGINT) AS id,
                   regexp_extract(url_source, '{OWNER_RE}', 1) AS owner
            FROM read_parquet(?)
        ), strata AS (
            SELECT id, owner, ntile(?) OVER (ORDER BY id) AS s FROM ids WHERE id IS NOT NULL
        )
        SELECT id, owner FROM strata QUALIFY row_number() OVER (PARTITION BY s ORDER BY hash(id, ?::BIGINT)) = 1
        UNION SELECT id, owner FROM (SELECT * FROM ids WHERE id IS NOT NULL ORDER BY id LIMIT 1)
        UNION SELECT id, owner FROM (SELECT * FROM ids WHERE id IS NOT NULL ORDER BY id DESC LIMIT 1)
        """,
        [links, n, seed],
    ).fetchall()
    return sorted((int(i), o) for i, o in rows)


#: Bin sizes ``--bin-size auto`` chooses between, by cross-validation on the fit half only.
BIN_SIZES = (5, 9, 15, 25, 35)


def choose_bin_size(fit: list[tuple[int, int]]) -> tuple[int, dict[int, float]]:
    """The bin size with the best exact-year rate under 2-fold CV within ``fit`` (ties: the smaller p90)."""
    folds = (fit[0::2], fit[1::2])
    scores: dict[int, tuple[float, float]] = {}
    for size in BIN_SIZES:
        exact, p90 = [], []
        for k in (0, 1):
            train, test = folds[k], folds[1 - k]
            m = uy.measure(uy.fit_breakpoints(train, bin_size=size), test)
            if m["n"]:
                exact.append(m["year_exact"])
                p90.append(m["p90_abs_days"])
        scores[size] = (sum(exact) / len(exact), sum(p90) / len(p90)) if exact else (0.0, float("inf"))
    best = max(scores, key=lambda b: (scores[b][0], -scores[b][1]))
    return best, {b: round(v[0], 4) for b, v in scores.items()}


def read_cache(path: Path) -> dict[int, dict]:
    done: dict[int, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                done[int(rec["id"])] = rec
    return done


def fetch(ids: list[tuple[int, str]], cache: Path, dates: FlickrDates) -> dict[int, dict]:
    done = read_cache(cache)
    todo = [(i, o) for i, o in ids if i not in done]
    print(f"{len(done)} cached, {len(todo)} to ask Flickr ({dates.via})", file=sys.stderr)
    with cache.open("a") as fh:
        for n, (pid, owner) in enumerate(todo, 1):
            rec: dict = {"id": pid, "owner": owner}
            try:
                got = dates.upload_date(pid, owner)
                rec |= {"posted": got.posted, "via": got.via} if got else {"gone": True}
            except FlickrDateError as exc:
                rec["error"] = str(exc)
            except RuntimeError as exc:  # retries exhausted: leave it out of the cache, so a re-run asks again
                print(f"  {pid}: {exc}", file=sys.stderr)
                continue
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            done[pid] = rec
            if n % 25 == 0:
                print(f"  {n}/{len(todo)}", file=sys.stderr)
    return done


#: How each fetch path is named in the table's provenance.
SOURCE_LABELS = {"api": "flickr.photos.getInfo dates.posted", "page": "Flickr photo pages (datePosted)"}


def source_label(vias: Counter[str]) -> str:
    """Every fetch path that dated a sample, API first: a mixed run names both, an empty one says so."""
    if not vias:
        return "nothing dated"
    order = sorted(vias, key=lambda v: (list(SOURCE_LABELS).index(v) if v in SOURCE_LABELS else len(SOURCE_LABELS), v))
    return " + ".join(SOURCE_LABELS.get(v, v) for v in order)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--links", required=True, help="Megalith links parquet glob (read-only)")
    ap.add_argument("--cache", type=Path, required=True, help="JSONL of answers so far (appended; resumable)")
    ap.add_argument("--samples", type=int, default=700, help="id strata to sample (some photos are gone)")
    ap.add_argument("--low-samples", type=int, default=60, help="extra log-spaced ids below the 5th percentile id")
    ap.add_argument("--seed", type=int, default=53, help="picks the id within each stratum (reproducible)")
    ap.add_argument("--rate", type=float, default=1.0, help="requests per second to Flickr")
    ap.add_argument(
        "--bin-size", default="auto", help="fit samples per breakpoint (median), or auto: chosen by CV on the fit half"
    )
    ap.add_argument("--write", action="store_true", help="store the table in archives/data/")
    args = ap.parse_args(argv)

    config = QuarryConfig.from_env()
    rate = SourceRate(rate_per_sec=args.rate, burst=1)
    config.rates.setdefault(PAGE_RATE_SOURCE, rate)
    config.rates.setdefault(API_RATE_SOURCE, rate)
    ids = sorted(
        set(sample_ids(args.links, args.samples, args.seed))
        | set(sample_low_ids(args.links, args.low_samples, args.seed))
    )
    with NetClient(config) as net:
        dates = FlickrDates(net, config.flickr_api_key)
        answers = fetch(ids, args.cache, dates)

    wanted = {i for i, _ in ids}
    dated = sorted((r["id"], r["posted"]) for r in answers.values() if "posted" in r and r["id"] in wanted)
    gone = sum(1 for r in answers.values() if r.get("gone") and r["id"] in wanted)
    errors = sum(1 for r in answers.values() if "error" in r and r["id"] in wanted)
    fit, held_out = dated[0::2], dated[1::2]
    if dated and dated[-1] not in fit:  # both ends belong to the table, so it covers the whole id range
        fit.append(dated[-1])
        held_out.remove(dated[-1])
    cv: dict[int, float] | None = None
    if args.bin_size == "auto":
        bin_size, cv = choose_bin_size(fit)
    else:
        bin_size = int(args.bin_size)
    points = uy.fit_breakpoints(fit, bin_size=bin_size)
    error = uy.measure(points, held_out)
    vias = Counter(str(r.get("via")) for r in answers.values() if "posted" in r and r["id"] in wanted)
    table = {
        "schema": uy.BREAKPOINTS_SCHEMA,
        "what": "Flickr photo id -> UPLOAD time (dates.posted / datePosted), never the capture date",
        "source": source_label(vias),
        "sources": dict(sorted(vias.items())),
        "sampled_from": "Megalith-10m photo ids: one per equal-count id stratum, both ends, and "
        f"{args.low_samples} log-spaced bands below the 5th-percentile id",
        "sampled_at": datetime.now(UTC).date().isoformat(),
        "requested": len(ids),
        "dated": len(dated),
        "gone": gone,
        "errors": errors,
        "fit": len(fit),
        "bin_size": bin_size,
        "bin_size_cv_year_exact": cv,
        "held_out": error,
        "years": [y for y, _ in uy.year_thresholds(points)],
        "points": [list(p) for p in points],
    }
    print(json.dumps({k: v for k, v in table.items() if k != "points"}, indent=2))
    if args.write:
        uy.breakpoints_path().write_text(json.dumps(table, indent=1) + "\n")
        print(f"wrote {uy.breakpoints_path()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
