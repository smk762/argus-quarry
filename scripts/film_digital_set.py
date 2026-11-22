"""A hand-labelled film-scan / digital set at the clip_era 1980-1999 / 2000-now boundary (#55).

    # 1. draw the set (stratified by clip_era x confidence; iNaturalist left out) and make thumbnails
    uv run --no-sync python scripts/film_digital_set.py sample \\
        --index $QUARRY_HOME/cache/archives/pd12m.duckdb --scores <dir>/pd12m.era.parquet \\
        --out docs/film-digital/sample.json --thumbs <dir>/thumbs

    # 2. label it (the labelling page), export the labels to docs/film-digital/labels-person.json
    #    and build the objective reference from each file's EXIF device (`exif`)

    # 3. report precision of clip_kind=photo&clip_era=2000-now at the documented thresholds
    uv run --no-sync python scripts/film_digital_set.py report \\
        --sample docs/film-digital/sample.json --labels docs/film-digital/exif.json \\
        --compare docs/film-digital/labels-person.json

Why stratified: the archive's 2000-now photos are 87% at conf >= 0.7, so a plain random draw
would say almost nothing about the low-confidence bands where film scans would hide. Each
(era, confidence band) stratum gets a fixed quota, and every rate reported is reweighted by the
stratum's real size in the scored rows, so the numbers describe the archive, not the sample.

iNaturalist is left out on purpose: its photos are digital by construction (the platform
started in 2008) and already serve as the known-digital reference in docs/era-calibration.md.
Including them would flatter precision. The caption's tone must not be ``bw``, and each drawn
image is checked for colour after download (mean saturation), since this is a colour-film question.

Labels are ``digital``, ``film`` (a scan of film or a print) or ``unsure``. Precision is digital /
(digital + film); ``unsure`` is reported separately and never counted either way.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import duckdb

from argus_quarry.archives import era as ze
from argus_quarry.exif import SCAN, capture_medium, exif_device

ERAS = ("1980-1999", "2000-now")
#: Confidence bands, split at the thresholds docs/era-calibration.md recommends (0.5, 0.7).
BANDS = (("lt0.5", 0.0, 0.5), ("0.5-0.7", 0.5, 0.7), ("ge0.7", 0.7, 1.01))
QUOTA = {"2000-now": 60, "1980-1999": 40}
OVERDRAW = 1.4  # extra draws per stratum, for images that turn out greyscale or unreadable
MIN_SATURATION = 0.10  # mean HSV saturation below this reads as black-and-white / sepia
EXCLUDED_COLLECTIONS = ("iNaturalist",)
THUMB_LONG_SIDE = 1280


def band_of(conf: float) -> str:
    return next(name for name, lo, hi in BANDS if lo <= conf < hi)


def draw(index: Path, scores: Path, seed: int) -> tuple[list[dict], dict[str, int]]:
    """Candidates per stratum (in a seeded order, OVERDRAW x quota) and every stratum's population."""
    con = duckdb.connect()
    con.execute(f"ATTACH '{index}' AS ix (READ_ONLY)")
    con.execute(f"CREATE TEMP VIEW e AS {ze.era_join_sql(scores)}")
    excluded = ", ".join(f"'{c}'" for c in EXCLUDED_COLLECTIONS)
    con.execute(
        f"""
        CREATE TEMP TABLE pool AS
        SELECT i.id, i.url, i.collection, e.clip_era, e.clip_era_conf,
               CASE WHEN e.clip_era_conf < 0.5 THEN 'lt0.5' WHEN e.clip_era_conf < 0.7 THEN '0.5-0.7' ELSE 'ge0.7' END
                   AS band
        FROM e JOIN ix.items i USING (pos)
        WHERE e.clip_kind = 'photo' AND e.clip_era IN ('1980-1999', '2000-now')
          AND i.tone IS DISTINCT FROM 'bw' AND i.collection NOT IN ({excluded})
        """
    )
    population = {
        f"{era}|{band}": n
        for era, band, n in con.execute("SELECT clip_era, band, count(*) FROM pool GROUP BY 1, 2").fetchall()
    }
    rows = con.execute(
        """
        SELECT id, url, collection, clip_era, clip_era_conf, band FROM pool
        QUALIFY row_number() OVER (PARTITION BY clip_era, band ORDER BY hash(id, ?::BIGINT))
                <= CASE clip_era WHEN '2000-now' THEN ? ELSE ? END
        """,
        [seed, math.ceil(QUOTA["2000-now"] * OVERDRAW), math.ceil(QUOTA["1980-1999"] * OVERDRAW)],
    ).fetchall()
    cols = ("id", "url", "collection", "clip_era", "clip_era_conf", "band")
    return [dict(zip(cols, r, strict=True)) for r in rows], population


def colourful(path: Path) -> tuple[bool, float]:
    from PIL import Image, ImageStat

    with Image.open(path) as im:
        im.draft("RGB", (256, 256))
        hsv = im.convert("RGB").resize((128, 128)).convert("HSV")
        sat = ImageStat.Stat(hsv).mean[1] / 255
    return sat >= MIN_SATURATION, round(sat, 3)


def thumbnail(src: Path, dest: Path) -> None:
    from PIL import Image

    with Image.open(src) as im:
        im.draft("RGB", (THUMB_LONG_SIDE, THUMB_LONG_SIDE))
        im = im.convert("RGB")
        im.thumbnail((THUMB_LONG_SIDE, THUMB_LONG_SIDE))
        im.save(dest, "JPEG", quality=88, optimize=True)


def cmd_sample(args: argparse.Namespace) -> int:
    from argus_quarry.config import QuarryConfig, SourceRate
    from argus_quarry.net import NetClient

    candidates, population = draw(args.index, args.scores, args.seed)
    config = QuarryConfig.from_env()
    config.rates.setdefault("pd12m-s3", SourceRate(rate_per_sec=4, burst=4))
    originals, thumbs = args.thumbs / "originals", args.thumbs
    originals.mkdir(parents=True, exist_ok=True)
    kept: dict[str, list[dict]] = {}
    rejected = {"greyscale": 0, "unreadable": 0}
    with NetClient(config) as net:
        for c in candidates:
            stratum = f"{c['clip_era']}|{c['band']}"
            if len(kept.setdefault(stratum, [])) >= QUOTA[c["clip_era"]]:
                continue
            src = originals / f"{c['id']}.img"
            thumb = thumbs / f"{c['id']}.jpg"
            try:
                if not thumb.exists():
                    if not src.exists():
                        net.download(c["url"], src, source="pd12m-s3", max_bytes=80 * 1024 * 1024)
                    ok, sat = colourful(src)
                    if not ok:
                        rejected["greyscale"] += 1
                        continue
                    thumbnail(src, thumb)
                    src.unlink(missing_ok=True)
                else:
                    sat = colourful(thumb)[1]
            except Exception as exc:  # a dead link or an undecodable file: draw the next candidate
                print(f"  {c['id']}: {exc}", file=sys.stderr)
                rejected["unreadable"] += 1
                continue
            kept[stratum].append({**c, "saturation": sat})
            print(f"  {stratum}: {len(kept[stratum])}", file=sys.stderr) if len(kept[stratum]) % 10 == 0 else None
    short = {s: QUOTA[s.split("|")[0]] - len(v) for s, v in kept.items() if len(v) < QUOTA[s.split("|")[0]]}
    items = [it for s in sorted(kept) for it in kept[s]]
    out = {
        "what": "PD12M colour photos at the clip_era 1980-1999 / 2000-now boundary, for hand labels (#55)",
        "seed": args.seed,
        "prompts_sha256": ze.PROMPTS_SHA256,
        "scored_rows": duckdb.execute(f"SELECT count(*) FROM read_parquet('{args.scores}')").fetchone()[0],
        "excluded_collections": list(EXCLUDED_COLLECTIONS),
        "quota": QUOTA,
        "population": population,
        "rejected": rejected,
        "short": short,
        "items": items,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(items)} items -> {args.out}; rejected {rejected}; short {short or 'none'}", file=sys.stderr)
    return 0


def report(sample: dict, labels: dict[str, str]) -> dict:
    """Stratum-weighted precision of photo+2000-now at each threshold, plus what lands in 1980-1999."""
    pop = sample["population"]
    by_stratum: dict[str, dict[str, int]] = {}
    for it in sample["items"]:
        lab = labels.get(it["id"])
        if lab is None:
            continue
        s = by_stratum.setdefault(f"{it['clip_era']}|{it['band']}", {"digital": 0, "film": 0, "unsure": 0})
        s[lab] += 1

    def weighted(era: str, bands: list[str]) -> dict:
        digital = film = unsure = 0.0
        n_dec = n_all = 0
        for band in bands:
            s = by_stratum.get(f"{era}|{band}")
            if not s:
                continue
            labelled = s["digital"] + s["film"] + s["unsure"]
            w = pop.get(f"{era}|{band}", 0) / labelled
            digital, film, unsure = digital + s["digital"] * w, film + s["film"] * w, unsure + s["unsure"] * w
            n_dec, n_all = n_dec + s["digital"] + s["film"], n_all + labelled
        decided = digital + film
        return {
            "labelled": n_all,
            "decided": n_dec,
            "digital_share": round(digital / decided, 4) if decided else None,
            "film_share": round(film / decided, 4) if decided else None,
            "unsure_share": round(unsure / (decided + unsure), 4) if decided + unsure else None,
        }

    names = [b[0] for b in BANDS]
    return {
        "labelled": sum(sum(s.values()) for s in by_stratum.values()),
        "per_stratum": by_stratum,
        # clip_era_min_conf=t keeps the bands at or above t.
        "precision_2000_now": {
            "conf>=0": weighted("2000-now", names),
            "conf>=0.5": weighted("2000-now", names[1:]),
            "conf>=0.7": weighted("2000-now", names[2:]),
        },
        "digital_in_1980_1999": weighted("1980-1999", names),
    }


def exif_verdict(make: str, model: str) -> str | None:
    """``film`` for a film scanner, ``digital`` for a camera or phone, None when the file names no device.

    The device rule is the package's :func:`argus_quarry.exif.capture_medium`, the one ingest records.
    """
    medium = capture_medium(make, model)
    return "film" if medium == SCAN else medium


def cmd_exif(args: argparse.Namespace) -> int:
    """An objective reference: the device each sampled file's EXIF names (PD12M's S3 copies keep it)."""
    import io

    from PIL import Image

    from argus_quarry.config import QuarryConfig, SourceRate
    from argus_quarry.net import NetClient

    sample = json.loads(args.sample.read_text())
    config = QuarryConfig.from_env()
    config.rates.setdefault("pd12m-s3", SourceRate(rate_per_sec=4, burst=4))
    out: dict[str, dict] = {}
    with NetClient(config) as net:
        for it in sample["items"]:
            dest = args.cache / f"{it['id']}.img"
            try:
                if not dest.exists():
                    net.download(it["url"], dest, source="pd12m-s3", max_bytes=80 * 1024 * 1024)
                with Image.open(io.BytesIO(dest.read_bytes())) as im:
                    make, model = (v or "" for v in exif_device(im))
            except Exception as exc:  # unreadable: no reference for this item
                print(f"  {it['id']}: {exc}", file=sys.stderr)
                make = model = ""
            out[it["id"]] = {"make": make, "model": model, "verdict": exif_verdict(make, model)}
    labels = {i: v["verdict"] for i, v in out.items() if v["verdict"]}
    args.out.write_text(json.dumps({"labels": dict(sorted(labels.items())), "devices": out}, indent=1) + "\n")
    print(f"{len(labels)} of {len(out)} name a device -> {args.out}", file=sys.stderr)
    return 0


def cmd_page(args: argparse.Namespace) -> int:
    """The labelling page: blind (ids and image paths only), in a fixed shuffled order, beside its images."""
    import random
    import shutil

    sample = json.loads(args.sample.read_text())
    ids = [it["id"] for it in sample["items"]]
    random.Random(args.seed).shuffle(ids)
    template = (Path(__file__).parent / "film_digital_labeller.template.html").read_text()
    items = json.dumps([{"id": i, "src": f"img/{i}.jpg"} for i in ids], separators=(",", ":"))
    (args.out / "img").mkdir(parents=True, exist_ok=True)
    (args.out / "film-or-digital.html").write_text(template.replace("__ITEMS__", items))
    for i in ids:
        shutil.copy(args.thumbs / f"{i}.jpg", args.out / "img" / f"{i}.jpg")
    print(f"{len(ids)} photos -> {args.out}", file=sys.stderr)
    return 0


def read_store_export(path: Path) -> dict[str, str]:
    """Labels from the page's store: a directory of ``labels/<id>.json`` documents (``read_db`` with
    ``out_dir``), or one JSON list of ``{"id": ..., "data": {"label": ...}}`` rows."""
    docs = (
        [{"id": p.stem, "data": json.loads(p.read_text())} for p in sorted(path.rglob("*.json"))]
        if path.is_dir()
        else json.loads(path.read_text())
    )
    out: dict[str, str] = {}
    for doc in docs:
        data = doc.get("data", doc)
        label = data.get("label") if isinstance(data, dict) else None
        if label in ("digital", "film", "unsure"):
            out[str(doc["id"])] = label
    return out


def cmd_labels(args: argparse.Namespace) -> int:
    sample = json.loads(args.sample.read_text())
    wanted = {it["id"] for it in sample["items"]}
    labels = {k: v for k, v in read_store_export(args.export).items() if k in wanted}  # drops probes and strays
    args.out.write_text(json.dumps({"labels": dict(sorted(labels.items()))}, indent=1) + "\n")
    print(f"{len(labels)} of {len(wanted)} labelled -> {args.out}", file=sys.stderr)
    return 0


def agreement(reference: dict[str, str], labels: dict[str, str]) -> dict:
    """How a rater's decided labels compare with a reference, on the items both cover."""
    both = [i for i in labels if i in reference and labels[i] != "unsure"]
    pairs: dict[str, int] = {}
    for i in both:
        key = f"reference {reference[i]} / rater {labels[i]}"
        pairs[key] = pairs.get(key, 0) + 1
    return {
        "compared": len(both),
        "agree": sum(reference[i] == labels[i] for i in both),
        "rater_unsure": sum(1 for i in labels if i in reference and labels[i] == "unsure"),
        "pairs": pairs,
    }


def cmd_report(args: argparse.Namespace) -> int:
    sample = json.loads(args.sample.read_text())
    labels = json.loads(args.labels.read_text())
    labels = labels.get("labels", labels)
    if args.worst_case:  # every item the reference doesn't cover counted against precision
        labels = {**{it["id"]: "film" for it in sample["items"]}, **labels}
    unknown = sorted(set(labels.values()) - {"digital", "film", "unsure"})
    if unknown:
        raise SystemExit(f"unknown label value(s): {unknown}")
    out = report(sample, labels)
    for path in args.compare or []:
        rater = json.loads(path.read_text())
        out.setdefault("agreement", {})[path.name] = agreement(labels, rater.get("labels", rater))
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="draw the stratified set and make thumbnails")
    s.add_argument("--index", type=Path, required=True, help="pd12m.duckdb (read-only)")
    s.add_argument("--scores", type=Path, required=True, help="pd12m.era.parquet from `archives era`")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--thumbs", type=Path, required=True, help="where the labelling thumbnails go")
    s.add_argument("--seed", type=int, default=55)
    p = sub.add_parser("page", help="build the blind labelling page and stage its images")
    p.add_argument("--sample", type=Path, required=True)
    p.add_argument("--thumbs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=5555, help="the page's presentation order")
    lb = sub.add_parser("labels", help="the page's stored labels -> a labels file")
    lb.add_argument("--sample", type=Path, required=True)
    lb.add_argument("--export", type=Path, required=True, help="read_db export: a directory or a JSON list")
    lb.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report", help="precision from the labels")
    r.add_argument("--sample", type=Path, required=True)
    r.add_argument("--labels", type=Path, required=True, help='{"labels": {id: "digital"|"film"|"unsure"}}')
    r.add_argument("--worst-case", action="store_true", help="count items the labels don't cover as film")
    r.add_argument("--compare", type=Path, nargs="*", help="other raters' labels, scored against --labels")
    e = sub.add_parser("exif", help="the objective reference: each file's EXIF device")
    e.add_argument("--sample", type=Path, required=True)
    e.add_argument("--cache", type=Path, required=True, help="where the downloaded originals are kept")
    e.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    commands = {"sample": cmd_sample, "page": cmd_page, "labels": cmd_labels, "exif": cmd_exif, "report": cmd_report}
    return commands[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
