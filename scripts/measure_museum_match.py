"""Measure how PD12M's Met items match the Met's catalogue (#52), on a random sample, before a full harvest.

    uv run --no-sync python scripts/measure_museum_match.py \\
        --pd12m "$QUARRY_PD12M_DIR" --cache met-sample.jsonl --images /tmp/met-images \\
        [--sample 3000] [--precision 60] [--rate 10] [--out museum-match.json]

1. **Match rate.** A seeded random sample of the Met's object ids is fetched from its open API
   (answers cached in ``--cache``, so a re-run asks nothing twice). Among the public-domain objects
   with images, how many have an image PD12M holds (``uuid5(URL, image url)`` = its S3 filename)?
2. **Coverage.** Scaled up from the sample: how many of PD12M's Met items a full harvest would
   date, and how many matched objects carry a catalogue date.
3. **Precision.** For ``--precision`` matches, the Met's web-size image and PD12M's copy are both
   downloaded and compared: the same picture has a perceptual-hash distance <= 10 (of 64 bits)
   and the same aspect ratio within 2%. Byte-identical copies are counted too.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import duckdb

from argus_quarry.archives import museum as mu
from argus_quarry.config import QuarryConfig, SourceRate
from argus_quarry.net import NetClient, RemoteGone

IMAGE_RATE_SOURCE = "met-images"
PHASH_MAX = 10
ASPECT_TOL = 0.02


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    """95% Wilson score interval for k successes in n."""
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def pd12m_met(pd12m: Path) -> dict[str, tuple[str, int, int, str]]:
    """PD12M's Met rows by S3 uuid: (url, width, height, hash)."""
    files = str(pd12m / "metadata" / "**" / "*.parquet").replace("'", "''")
    rows = duckdb.execute(
        f"SELECT url, width, height, hash FROM read_parquet('{files}') WHERE source = ?", [mu.MET_COLLECTION]
    ).fetchall()
    return {r[0].rsplit("/", 1)[1].split(".")[0]: r for r in rows}


def same_picture(a: Path, b: Path) -> dict:
    import imagehash
    from PIL import Image

    with Image.open(a) as ia, Image.open(b) as ib:
        dist = imagehash.phash(ia) - imagehash.phash(ib)
        ra, rb = ia.width / ia.height, ib.width / ib.height
    aspect = abs(ra - rb) / max(ra, rb)
    return {
        "phash_distance": int(dist),
        "aspect_diff": round(aspect, 4),
        "same": dist <= PHASH_MAX and aspect <= ASPECT_TOL,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pd12m", type=Path, required=True, help="the archived PD12M metadata dir (read-only)")
    ap.add_argument("--cache", type=Path, required=True, help="JSONL of Met answers (appended; resumable)")
    ap.add_argument("--images", type=Path, required=True, help="where the precision check downloads images")
    ap.add_argument("--sample", type=int, default=3000, help="random Met object ids to ask about")
    ap.add_argument("--precision", type=int, default=60, help="matches to verify by comparing the images")
    ap.add_argument("--seed", type=int, default=52)
    ap.add_argument("--rate", type=float, default=mu.DEFAULT_RATE, help="requests per second to the Met")
    ap.add_argument("--block-wait", type=float, default=mu.DEFAULT_BLOCK_WAIT, help="pause after a firewall block")
    ap.add_argument("--out", type=Path, help="also write the summary JSON here")
    args = ap.parse_args(argv)

    config = QuarryConfig.from_env()
    for source in (mu.MET_RATE_SOURCE, IMAGE_RATE_SOURCE):
        config.rates.setdefault(source, SourceRate(rate_per_sec=args.rate, burst=1))
    held = pd12m_met(args.pd12m)
    print(f"PD12M holds {len(held):,} Met images", file=sys.stderr)

    with NetClient(config) as net:
        client = mu.MetClient(net)
        all_ids = client.object_ids()
        sample = sorted(random.Random(args.seed).sample(all_ids, min(args.sample, len(all_ids))))
        # Reuse the harvest's cache format: the cache file is the harvest file of a throwaway index dir.
        index_dir, archive = args.cache.parent, args.cache.name.removesuffix(".met.jsonl")
        if mu.cache_path(index_dir, archive) != args.cache:
            raise SystemExit("--cache must be named <something>.met.jsonl")
        counts = mu.harvest_patiently(
            client,
            index_dir,
            archive,
            sample,
            block_wait=args.block_wait,
            on_block=lambda n, w: print(f"blocked ({n}); pausing {w:.0f}s", file=sys.stderr),
        )
        print(f"asked {counts}", file=sys.stderr)
        if counts["blocked"]:
            raise SystemExit(
                "the Met refused requests part-way: re-run later (cached answers are kept) or lower --rate"
            )
        answers = mu.read_cache(args.cache)

        objs = [answers[i] for i in sample if i in answers]
        gone = sum(1 for o in objs if o.get("gone"))
        pd_img = [o for o in objs if o.get("pd") and o.get("uuids")]
        matched = [o for o in pd_img if any(u in held for u in o["uuids"])]
        matched_images = sum(1 for o in matched for u in o["uuids"] if u in held)
        dated = sum(1 for o in matched if o.get("begin") is not None)
        scale = len(all_ids) / len(objs) if objs else 0

        checks = []
        args.images.mkdir(parents=True, exist_ok=True)
        rng = random.Random(args.seed + 1)
        for o in rng.sample(matched, min(args.precision, len(matched))):
            i = next(k for k, u in enumerate(o["uuids"]) if u in held)
            museum_url, s3 = o["images"][i], held[o["uuids"][i]][0]
            web = museum_url.replace("/original/", "/web-large/")  # the Met's ~800px rendition of the same image
            a, b = args.images / f"{o['id']}-met.jpg", args.images / f"{o['id']}-pd12m.jpg"
            try:
                for url, dest in ((web, a), (s3, b)):
                    if not dest.exists():
                        net.download(url, dest, source=IMAGE_RATE_SOURCE, max_bytes=60 * 1024 * 1024)
            except (RemoteGone, RuntimeError) as exc:
                checks.append({"id": o["id"], "error": str(exc)[:120]})
                continue
            checks.append({"id": o["id"], **same_picture(a, b)})

    verified = [c for c in checks if "same" in c]
    same = sum(c["same"] for c in verified)
    summary = {
        "sampled_objects": len(objs),
        "collection_objects": len(all_ids),
        "gone": gone,
        "public_domain_with_images": len(pd_img),
        "matched_objects": len(matched),
        "match_rate_of_public_domain_with_images": round(len(matched) / len(pd_img), 4) if pd_img else None,
        "match_rate_ci95": wilson(len(matched), len(pd_img)),
        "matched_with_catalogue_date": round(dated / len(matched), 4) if matched else None,
        "estimated_pd12m_met_images_matched": round(matched_images * scale),
        "pd12m_met_images": len(held),
        "estimated_pd12m_coverage": round(matched_images * scale / len(held), 4) if held else None,
        "precision_checked": len(verified),
        "precision_download_errors": len(checks) - len(verified),
        "precision_same_picture": same,
        "precision": round(same / len(verified), 4) if verified else None,
        "precision_ci95": wilson(same, len(verified)),
        "mismatches": [c for c in verified if not c["same"]],
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
