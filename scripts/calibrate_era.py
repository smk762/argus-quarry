"""Calibrate the zero-shot CLIP era (#19) against PD12M's caption-dated items.

    uv run --no-sync python scripts/calibrate_era.py \\
        --index $QUARRY_HOME/cache/archives/pd12m.duckdb \\
        --vectors $QUARRY_HOME/cache/archives/pd12m.semantic.<token>/vectors.f16.npy \\
        --out era-calibration.json [--write-bias]

``--vectors`` takes the semantic build's local float16 copy (fast) or the archived
``embeddings/**/*.npy`` files in row order (slow over a NAS share); both are read
memory-mapped, a chunk of rows at a time, so memory stays at one chunk.

Ground truth is weak by construction: a caption's year is the first year it
*mentions*, which for a photo is often a date on a sign or a plaque, so it is a
lower bound on the capture date more than the capture date itself. Items split
by ``hash(id)`` into a fit half and a held-out half; per-label biases are fitted
on the fit half only, with every era weighted equally (the dated set is mostly
pre-1946, the archive is not), and every number reported is from the held-out
half. Biases are fitted on artworks and documents only (a photo's caption year is
usually its subject's date) and photo labels keep a zero bias. A second,
collection-level check uses iNaturalist (founded 2008: digital photos) as
known-digital and historic-photo archives (:data:`FILM_ARCHIVES`) as known-film.

``--write-bias`` stores the fitted biases in ``archives/data/era_prompts.json`` and
prints the new sha256 to pin as ``era.PROMPTS_SHA256``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import numpy as np

from argus_quarry.archives import era as E

CHUNK = 8192
#: Caption medium -> zero-shot kind. "object" and unlabelled captions only check the era.
MEDIUM_KIND = {"photo": "photo", "painting": "artwork", "drawing": "artwork", "print": "artwork", "map": "document",
               "text": "document"}  # fmt: skip
#: Historic-photo archives in PD12M whose photos are mostly digitised film / glass plates.
FILM_ARCHIVES = ("Swedish Railway Museum", "Deutsche Fotothek", "Netherlands Institute for Military History",
                 "Estonian Sports and Olympic Museum", "The Norwegian Museum of Cultural History", "Bohuslän Museum",
                 "The Utrecht Archives", "Regional Archive Nijmegen")  # fmt: skip
THRESHOLDS = (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def era_of_year(year: int) -> int:
    bounds = (1800, 1900, 1946, 1980, 2000)
    return sum(year >= b for b in bounds)


class Rows:
    """Rows of one or more row-aligned .npy matrices, memory-mapped, normalised per chunk."""

    def __init__(self, files: list[Path]) -> None:
        self.mats = [np.load(f, mmap_mode="r") for f in files]
        self.starts = np.cumsum([0] + [m.shape[0] for m in self.mats])

    def __len__(self) -> int:
        return int(self.starts[-1])

    def take(self, positions: np.ndarray) -> np.ndarray:
        out = np.empty((len(positions), self.mats[0].shape[1]), dtype=np.float32)
        which = np.searchsorted(self.starts, positions, side="right") - 1
        for f in np.unique(which):
            sel = which == f
            out[sel] = self.mats[f][positions[sel] - self.starts[f]]
        # A zero row is a missing embedding, not a direction. Dividing by its norm would be NaN.
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
        return out


def probs_for(rows: Rows, positions: np.ndarray, prompts: E.PromptSet) -> np.ndarray:
    order = np.argsort(positions)
    out = np.empty((len(positions), len(prompts.labels)), dtype=np.float32)
    for lo in range(0, len(order), CHUNK):
        idx = order[lo : lo + CHUNK]
        out[idx] = prompts.probs(rows.take(positions[idx]))
    return out


def fit_bias(logits: np.ndarray, era: np.ndarray, kind: np.ndarray, prompts: E.PromptSet, steps: int = 400):
    """Per-label logit biases maximising the era (+ kind, where known) likelihood, eras weighted equally."""
    era_of, kind_of = prompts.era_of, prompts.kind_of
    counts = np.bincount(era, minlength=len(E.ERAS)).astype(np.float64)
    w = (len(era) / (len(E.ERAS) * np.maximum(counts, 1)))[era][:, None]
    b = np.zeros(logits.shape[1], dtype=np.float64)
    m = v = np.zeros_like(b)
    has_kind = kind >= 0
    for t in range(1, steps + 1):
        z = logits + b
        z -= z.max(1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(1, keepdims=True)
        in_era = era_of[:, era].T  # (n, labels)
        q = p * in_era
        q /= q.sum(1, keepdims=True)
        g = (p - q) * w
        if has_kind.any():
            in_kind = kind_of[:, np.where(has_kind, kind, 0)].T
            qk = p * in_kind
            qk /= qk.sum(1, keepdims=True)
            g[has_kind] += ((p - qk) * w)[has_kind]
        grad = g.mean(0) + 1e-3 * b
        m = 0.9 * m + 0.1 * grad
        v = 0.999 * v + 0.001 * grad**2
        b -= 0.05 * (m / (1 - 0.9**t)) / (np.sqrt(v / (1 - 0.999**t)) + 1e-8)
    return b.astype(np.float32)


def assess(p: np.ndarray, era: np.ndarray, kind: np.ndarray, prompts: E.PromptSet) -> dict:
    era_p, kind_p = p @ prompts.era_of, p @ prompts.kind_of
    pe, pk = era_p.argmax(1), kind_p.argmax(1)
    conf = era_p.max(1)
    per_era = {}
    for i, name in enumerate(E.ERAS):
        sel = era == i
        predicted = pe == i
        per_era[name] = {
            "n": int(sel.sum()),
            "recall": round(float((pe[sel] == i).mean()), 3) if sel.any() else None,
            "precision": round(float((era[predicted] == i).mean()), 3) if predicted.any() else None,
        }
    known = kind >= 0
    out = {
        "n": int(len(era)),
        "accuracy": round(float((pe == era).mean()), 3),
        "balanced_accuracy": round(float(np.nanmean([v["recall"] or np.nan for v in per_era.values()])), 3),
        "within_one_bucket": round(float((np.abs(pe.astype(int) - era) <= 1).mean()), 3),
        "per_era": per_era,
        "kind_accuracy": round(float((pk[known] == kind[known]).mean()), 3) if known.any() else None,
        "confusion": np.stack([np.bincount(pe[era == i], minlength=len(E.ERAS)) for i in range(len(E.ERAS))]).tolist(),
        "by_threshold": [],
    }
    for t in THRESHOLDS:
        sel = conf >= t
        out["by_threshold"].append(
            {"min_conf": t, "kept": round(float(sel.mean()), 3), "accuracy": round(float((pe[sel] == era[sel]).mean()), 3)
             if sel.any() else None}
        )  # fmt: skip
    return out


def boundary(p: np.ndarray, era: np.ndarray, kind: np.ndarray, prompts: E.PromptSet) -> dict:
    """Photos only (caption medium photo): true era vs predicted, and "photo, 2000-now" precision by threshold."""
    era_p, kind_p = p @ prompts.era_of, p @ prompts.kind_of
    pe, pk = era_p.argmax(1), kind_p.argmax(1)
    photo = kind == E.KINDS.index("photo")
    digital = len(E.ERAS) - 1
    cols = ["<=1945", "1946-1979", "1980-1999", "2000-now"]

    def col(e: np.ndarray) -> np.ndarray:
        return np.clip(e.astype(int) - 2, 0, 3)

    matrix = {cols[r]: np.bincount(col(pe[photo & (col(era) == r)]), minlength=4).tolist() for r in range(4)}
    rows = []
    for t in THRESHOLDS:
        chosen = (pk == E.KINDS.index("photo")) & (pe == digital) & (era_p[:, digital] >= t)
        pre = chosen & (era < digital)
        rows.append({
            "min_conf": t,
            "selected": int(chosen.sum()),
            "pre_digital_among_selected": int(pre.sum()),
            "digital_recall": round(float(chosen[photo & (era == digital)].mean()), 3) if (photo & (era == digital)).any() else None,
        })  # fmt: skip
    return {"columns_predicted": cols, "rows_true": matrix, "photo_digital_query": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--index", type=Path, required=True, help="the archive's DuckDB index (read-only)")
    ap.add_argument("--vectors", type=Path, nargs="+", required=True, help=".npy matrices in row order")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ref-sample", type=int, default=20000, help="items per collection-level reference set")
    ap.add_argument("--write-bias", action="store_true", help="store the fitted biases in the prompts file")
    args = ap.parse_args(argv)

    raw_prompts = E.load_prompts(expected_sha256=None)
    raw_prompts.bias[:] = 0  # calibrate from the raw zero-shot scores, whatever the file holds
    con = duckdb.connect(str(args.index), read_only=True)
    dated = con.execute(
        "SELECT pos, year, coalesce(medium, '') AS medium, hash(id || 'era-split') % 2 = 0 AS fit FROM items "
        "WHERE year IS NOT NULL AND pos IS NOT NULL"
    ).fetchnumpy()
    # Collection-level references (not caption years): iNaturalist (founded 2008) is digital-native;
    # the historic-photo archives below are mostly digitised film and glass plates, and their colour
    # photos are the hard case for #113 (scanned colour film vs digital).
    refs = {
        "inaturalist": con.execute(
            "SELECT pos FROM items WHERE collection = 'iNaturalist' ORDER BY rnd LIMIT ?", [args.ref_sample]
        ).fetchnumpy()["pos"]
    }
    archive_list = ", ".join("?" * len(FILM_ARCHIVES))
    for tone in ("bw", "color"):
        refs[f"film_archives_{tone}"] = con.execute(
            f"SELECT pos FROM items WHERE collection IN ({archive_list}) AND medium = 'photo' AND tone = ? "
            "ORDER BY rnd LIMIT ?",
            [*FILM_ARCHIVES, tone, args.ref_sample],
        ).fetchnumpy()["pos"]
    con.close()
    rows = Rows(args.vectors)
    positions = np.asarray(dated["pos"], dtype=np.int64)
    era = np.array([era_of_year(int(y)) for y in dated["year"]])
    kind = np.array([E.KINDS.index(MEDIUM_KIND[m]) if m in MEDIUM_KIND else -1 for m in dated["medium"]])
    fit = np.asarray(dated["fit"], dtype=bool)
    print(f"{len(positions):,} caption-dated items ({fit.sum():,} fit / {(~fit).sum():,} held out)", file=sys.stderr)

    p = probs_for(rows, positions, raw_prompts)
    logits = np.log(np.maximum(p, 1e-12))  # log-softmax: a bias adds to these exactly as to the logits
    # Fit only on artworks and documents: a photo's caption year is usually its subject's date
    # (a 1929 Ford at a car show, a plaque reading 1887), which would drag photo eras early.
    # Photo labels keep a zero bias.
    non_photo = fit & np.isin(kind, [E.KINDS.index("artwork"), E.KINDS.index("document")])
    bias = fit_bias(logits[non_photo], era[non_photo], kind[non_photo], raw_prompts)
    bias[[i for i, k in enumerate(raw_prompts.kinds) if k == "photo"]] = 0

    def apply(pp: np.ndarray) -> np.ndarray:
        out = np.exp(np.log(np.maximum(pp, 1e-12)) + bias)
        return out / out.sum(1, keepdims=True)

    cal = apply(p)
    ref_p = {name: probs_for(rows, np.asarray(pos, dtype=np.int64), raw_prompts) for name, pos in refs.items()}

    def ref_check(pp: np.ndarray) -> dict:
        ep, kp = pp @ raw_prompts.era_of, pp @ raw_prompts.kind_of
        photo, digital = kp.argmax(1) == 0, ep.argmax(1) == len(E.ERAS) - 1
        out = {
            "n": len(pp),
            "photo": round(float(photo.mean()), 3),
            "photo_and_2000_now": round(float((photo & digital).mean()), 3),
            "era_share": {e: round(float((ep.argmax(1) == i).mean()), 3) for i, e in enumerate(E.ERAS)},
        }
        for t in (0.5, 0.7):
            out[f"photo_and_2000_now_conf{t}"] = round(float((photo & digital & (ep[:, -1] >= t)).mean()), 3)
        return out

    held = ~fit
    report = {
        "eras": list(E.ERAS),
        "kinds": list(E.KINDS),
        "film_archives": list(FILM_ARCHIVES),
        "bias": {lb: round(float(b), 4) for lb, b in zip(raw_prompts.labels, bias, strict=True)},
    }
    for arm, probs, refp in (("zero_shot", p, ref_p), ("calibrated", cal, {k: apply(v) for k, v in ref_p.items()})):
        report[arm] = {
            "held_out": assess(probs[held], era[held], kind[held], raw_prompts),
            "boundary": boundary(probs[held], era[held], kind[held], raw_prompts),
            "references": {name: ref_check(v) for name, v in refp.items()},
        }
    args.out.write_text(json.dumps(report, indent=2))
    for arm in ("zero_shot", "calibrated"):
        h = report[arm]["held_out"]
        print(
            f"{arm:<10} acc {h['accuracy']} balanced {h['balanced_accuracy']} kind {h['kind_accuracy']} "
            f"refs {[(k, v['photo_and_2000_now']) for k, v in report[arm]['references'].items()]}",
            file=sys.stderr,
        )
    if args.write_bias:
        path = Path(E.__file__).parent / "data" / E.PROMPTS_FILE
        doc = json.loads(path.read_text())
        for lb, b in zip(raw_prompts.labels, bias, strict=True):
            doc["labels"][lb]["bias"] = round(float(b), 4)
        raw = (json.dumps(doc, separators=(",", ":")) + "\n").encode()
        path.write_bytes(raw)
        import hashlib

        print(f"wrote biases; pin PROMPTS_SHA256 = {hashlib.sha256(raw).hexdigest()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
