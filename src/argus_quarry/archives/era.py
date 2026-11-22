"""Zero-shot era (and picture kind) from an archive's published CLIP image embeddings (#19).

Neither archive records a capture or creation date, and only ~0.5% of PD12M's
captions mention a year. PD12M does publish a CLIP ViT-L/14 embedding for every
image, so each image is scored against a fixed set of text prompts ("a photograph
from the 1950s", "a 17th-century painting", ...) in that **same embedding space**,
and the softmax over the labels is summed two ways:

- ``clip_era``: the likeliest period (:data:`ERAS`), with ``clip_era_conf`` its probability;
- ``clip_kind``: photo, artwork or document (:data:`KINDS`), with ``clip_kind_conf``.

The prompt vectors come from the matching text tower (``openai/clip-vit-large-patch14``),
computed once offline (``argus-quarry archives era-prompts``) and committed as
``data/era_prompts.json``, pinned by :data:`PROMPTS_SHA256`. Scoring is a matrix
product with that file: no text model, no torch, at index or query time.

The scores land in ``$QUARRY_HOME/cache/archives/<name>.era.parquet`` (row ``pos``
of the embeddings), written by a resumable chunked pass (``archives era``); the
metadata index joins it on ``pos`` at build time, so filtering is plain SQL. The
names say where the value came from: ``year`` is caption-mentioned, ``clip_*`` is
this zero-shot estimate. Calibration against the caption-dated items is in
``docs/era-calibration.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

#: Coarse periods, oldest first. ``2000-now`` is the digital-camera era (consumer digital
#: overtook film around 2003-2005, so the boundary is approximate by design).
ERAS: tuple[str, ...] = ("pre1800", "1800s", "1900-1945", "1946-1979", "1980-1999", "2000-now")
#: Eras before digital photography: a "photo, digital era" query excludes all of these.
PRE_DIGITAL_ERAS: tuple[str, ...] = ERAS[:-1]
KINDS: tuple[str, ...] = ("photo", "artwork", "document")

PROMPTS_FILE = "era_prompts.json"
#: sha256 of ``data/era_prompts.json``. Regenerate the file with ``argus-quarry archives
#: era-prompts`` (needs the [search] extra and the model weights) and update this with it.
PROMPTS_SHA256 = "838913006bde10cf4bd5b46751aa00043908d174148f8f67b254c88646084745"
#: The text tower the prompts were encoded with: the space PD12M published its embeddings in.
MODEL_REPO = "openai/clip-vit-large-patch14"
#: Rows scored per chunk: 65,536 x 768 float32 is ~200 MB, whatever the archive's size.
DEFAULT_CHUNK_ROWS = 65_536
#: Bumped when the scores file's columns or meaning change; an older file is ignored.
ERA_SCHEMA = 1

# Each label's vector is the normalised mean of its prompts (prompt ensembling, as in the
# CLIP paper). Photography starts c. 1839, so there is no pre-1800 photo label.
_P = "a photograph"
LABEL_PROMPTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("photo", "1800s"): (
        "a 19th-century photograph.",
        "a daguerreotype.",
        "an albumen print photograph from the 1800s.",
        "a Victorian-era photograph.",
    ),
    ("photo", "1900-1945"): (
        f"{_P} from the 1910s.",
        f"{_P} from the 1920s.",
        f"{_P} from the 1930s.",
        f"{_P} from the 1940s.",
        "an early 20th-century photograph.",
    ),
    ("photo", "1946-1979"): (
        f"{_P} from the 1950s.",
        f"{_P} from the 1960s.",
        f"{_P} from the 1970s.",
        "a mid-century photograph.",
    ),
    ("photo", "1980-1999"): (
        f"{_P} from the 1980s.",
        f"{_P} from the 1990s.",
        "a 35mm color film photograph from the 1990s.",
        "a scanned film photo from the 1980s.",
    ),
    ("photo", "2000-now"): (
        "a digital photograph.",
        "a modern digital photo taken in the 2010s.",
        "a smartphone photo.",
        f"{_P} from the 2020s.",
        "a high-resolution digital camera photo.",
    ),
    ("artwork", "pre1800"): (
        "a 17th-century painting.",
        "a Renaissance painting.",
        "an 18th-century engraving.",
        "a medieval illustration.",
        "an old master drawing.",
    ),
    ("artwork", "1800s"): (
        "a 19th-century painting.",
        "a 19th-century lithograph.",
        "a Victorian illustration.",
        "an impressionist painting.",
    ),
    ("artwork", "1900-1945"): (
        "an early 20th-century painting.",
        "an art deco poster.",
        "a painting from the 1920s.",
        "an illustration from the 1930s.",
    ),
    ("artwork", "1946-1979"): (
        "a mid-century modern painting.",
        "a painting from the 1960s.",
        "an illustration from the 1950s.",
        "a poster from the 1970s.",
    ),
    ("artwork", "1980-1999"): (
        "a painting from the 1980s.",
        "an illustration from the 1990s.",
        "a poster from the 1980s.",
    ),
    ("artwork", "2000-now"): (
        "a contemporary digital artwork.",
        "a modern painting from the 2010s.",
        "a digital illustration.",
    ),
    ("document", "pre1800"): (
        "an 18th-century map.",
        "a 17th-century printed page.",
        "a medieval manuscript page.",
    ),
    ("document", "1800s"): (
        "a 19th-century map.",
        "a page from a 19th-century book.",
        "a handwritten letter from the 1800s.",
    ),
    ("document", "1900-1945"): (
        "a map from the 1920s.",
        "a typewritten document from the 1930s.",
        "a newspaper page from the 1940s.",
    ),
    ("document", "1946-1979"): (
        "a map from the 1960s.",
        "a typed document from the 1950s.",
        "a magazine page from the 1970s.",
    ),
    ("document", "1980-1999"): (
        "a printed document from the 1980s.",
        "a map from the 1990s.",
    ),
    ("document", "2000-now"): (
        "a modern printed document.",
        "a screenshot of a web page.",
        "a modern digital map.",
    ),
}


class EraUnavailable(RuntimeError):
    """No usable prompt set or scores (raised as ArchiveUnavailable by the archive)."""


def _np():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise EraUnavailable("era scoring requires: pip install argus-quarry[embeddings]") from exc
    return np


def label_key(kind: str, era: str) -> str:
    return f"{kind}:{era}"


# ── the prompt set ───────────────────────────────────────────────────────
@dataclass
class PromptSet:
    """Label vectors (unit length, one row per label) plus each label's kind and era."""

    labels: list[str]
    matrix: Any  # (labels, dim) float32
    bias: Any  # (labels,) float32 added to the logits; zeros unless calibrated
    logit_scale: float
    model: str = MODEL_REPO
    sha256: str | None = None

    def __post_init__(self) -> None:
        np = _np()
        self.kinds = [lb.split(":", 1)[0] for lb in self.labels]
        self.eras = [lb.split(":", 1)[1] for lb in self.labels]
        unknown = {*self.kinds} - {*KINDS} | {*self.eras} - {*ERAS}
        if unknown:
            raise EraUnavailable(f"prompt set names unknown kind(s)/era(s): {', '.join(sorted(unknown))}")
        # label -> bucket indicator matrices, so marginals are one matrix product each.
        self.era_of = np.zeros((len(self.labels), len(ERAS)), dtype=np.float32)
        self.kind_of = np.zeros((len(self.labels), len(KINDS)), dtype=np.float32)
        for i, (k, e) in enumerate(zip(self.kinds, self.eras, strict=True)):
            self.era_of[i, ERAS.index(e)] = 1
            self.kind_of[i, KINDS.index(k)] = 1

    def probs(self, x) -> Any:
        """Per-label softmax probabilities for unit-length image vectors ``x`` (rows)."""
        np = _np()
        logits = self.logit_scale * (np.asarray(x, dtype=np.float32) @ self.matrix.T) + self.bias
        logits -= logits.max(axis=1, keepdims=True)
        np.exp(logits, out=logits)
        logits /= logits.sum(axis=1, keepdims=True)
        return logits

    def score(self, x) -> dict[str, Any]:
        """Era and kind for each row of ``x``: index into :data:`ERAS` / :data:`KINDS`, and its probability."""
        np = _np()
        p = self.probs(x)
        era_p = p @ self.era_of
        kind_p = p @ self.kind_of
        era = era_p.argmax(axis=1)
        kind = kind_p.argmax(axis=1)
        rows = np.arange(len(p))
        return {
            "era": era.astype(np.uint8),
            "era_conf": era_p[rows, era].astype(np.float32),
            "kind": kind.astype(np.uint8),
            "kind_conf": kind_p[rows, kind].astype(np.float32),
        }


def _prompts_bytes() -> bytes:
    return resources.files("argus_quarry.archives").joinpath("data", PROMPTS_FILE).read_bytes()


def load_prompts(raw: bytes | None = None, *, expected_sha256: str | None = PROMPTS_SHA256) -> PromptSet:
    """The committed prompt set, refused unless its sha256 is :data:`PROMPTS_SHA256`.

    A file that was edited by hand (or regenerated without updating the pin) would
    silently change every stored era, so it fails loudly instead.
    """
    np = _np()
    raw = _prompts_bytes() if raw is None else raw
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise EraUnavailable(f"{PROMPTS_FILE} is not the pinned prompt set (sha256 {digest})")
    doc = json.loads(raw)
    labels = list(doc["labels"])
    matrix = np.asarray([doc["labels"][lb]["vector"] for lb in labels], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    bias = np.asarray([doc["labels"][lb].get("bias", 0.0) for lb in labels], dtype=np.float32)
    return PromptSet(labels, matrix, bias, float(doc["logit_scale"]), model=doc.get("model", MODEL_REPO), sha256=digest)


def export_prompts(model_dir: Path, output: Path, *, revision: str | None = None) -> str:
    """Encode :data:`LABEL_PROMPTS` with the CLIP text tower in ``model_dir``; returns the file's sha256.

    Offline and CPU-only; one tower, freed before returning. Biases are written as 0:
    calibration may set them (``scripts/calibrate_era.py --write-bias``), which changes the sha.
    """
    try:
        import torch
        from transformers import CLIPTextModelWithProjection, CLIPTokenizer
    except ImportError as exc:
        raise EraUnavailable("exporting prompts requires: pip install argus-quarry[search]") from exc
    np = _np()
    weights = next(
        (model_dir / n for n in ("model.safetensors", "pytorch_model.bin") if (model_dir / n).is_file()), None
    )
    if weights is None:
        raise EraUnavailable(f"{model_dir}: no model.safetensors / pytorch_model.bin")
    h = hashlib.sha256()
    with open(weights, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    tokenizer = CLIPTokenizer.from_pretrained(model_dir, local_files_only=True)
    # The same tower semantic.ClipTextEncoder runs for text queries, so both share one space.
    model = CLIPTextModelWithProjection.from_pretrained(model_dir, local_files_only=True).eval()
    scale = _logit_scale(weights)
    labels: dict[str, dict[str, Any]] = {}
    try:
        with torch.inference_mode():
            for (kind, era), prompts in LABEL_PROMPTS.items():
                tokens = tokenizer(list(prompts), padding=True, return_tensors="pt")
                v = model(**tokens).text_embeds
                v = torch.nn.functional.normalize(v.float(), dim=-1).mean(0)
                v = (v / v.norm()).numpy()
                labels[label_key(kind, era)] = {
                    "prompts": list(prompts),
                    "bias": 0.0,
                    "vector": [round(float(x), 6) for x in np.asarray(v)],
                }
    finally:
        del model
    doc = {
        "model": MODEL_REPO,
        "revision": revision or model_dir.name,
        "weights_sha256": h.hexdigest(),
        "logit_scale": round(scale, 4),
        "labels": labels,
    }
    raw = (json.dumps(doc, separators=(",", ":")) + "\n").encode()
    output.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _logit_scale(weights: Path) -> float:
    """The checkpoint's learned temperature, exp(logit_scale) (100 for OpenAI's CLIP)."""
    if weights.suffix == ".safetensors":
        from safetensors import safe_open

        with safe_open(str(weights), framework="pt") as f:
            return float(f.get_tensor("logit_scale").exp())
    import torch

    return float(torch.load(weights, map_location="cpu", weights_only=True)["logit_scale"].exp())


# ── the scores file ──────────────────────────────────────────────────────
def scores_path(root: Path, name: str) -> Path:
    return root / f"{name}.era.parquet"


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise EraUnavailable("era scores require: pip install argus-quarry[archives]") from exc
    return duckdb


def read_scores_meta(root: Path, name: str) -> dict[str, Any] | None:
    """The scores file's metadata (rows, prompts sha, built_at), or None when absent, foreign or old."""
    path = scores_path(root, name)
    if not path.is_file():
        return None
    try:
        con = _duckdb().connect()
        try:
            kv = con.execute("SELECT decode(key), decode(value) FROM parquet_kv_metadata(?)", [str(path)]).fetchall()
            rows = con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
        finally:
            con.close()
    except Exception:  # unreadable / half-written: same as absent
        return None
    meta = dict(kv)
    if meta.get("era_schema") != str(ERA_SCHEMA):
        return None
    return {
        "rows": rows,
        "total": int(meta.get("total", rows)),
        "prompts_sha256": meta.get("prompts_sha256"),
        "model": meta.get("model"),
        "built_at": meta.get("built_at"),
    }


def build_scores(
    sources: Sequence[Path],
    out_root: Path,
    name: str,
    *,
    expected_rows: int,
    prompts: PromptSet | None = None,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    limit_rows: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Score every embedding row in chunks and publish ``<name>.era.parquet`` atomically.

    Memory is one chunk, never the matrix: each chunk is read with plain file I/O (as the
    vector-index build does), scored, written as a small part file and freed. Part files
    survive an interruption under ``.<name>.era.parts/``, so a rerun with the same prompt
    set resumes where it stopped. ``limit_rows`` scores only the first rows (a sample run);
    rows past it stay unscored (NULL) in the index.
    """
    from argus_quarry.archives import semantic as sem  # the shared chunked .npy reader

    np = _np()
    duckdb = _duckdb()
    prompts = prompts or load_prompts()
    try:
        mats, dim = sem._open_sources(sources)
    except sem.SemanticUnavailable as exc:
        raise EraUnavailable(str(exc)) from exc
    if dim != prompts.matrix.shape[1]:
        raise EraUnavailable(f"{name}: embeddings are {dim}-d but the prompt set is {prompts.matrix.shape[1]}-d")
    sizes = [m.rows for m in mats]
    starts = [sum(sizes[:i]) for i in range(len(sizes))]
    total = sum(sizes)
    if total != expected_rows:
        raise EraUnavailable(f"{name}: embeddings have {total:,} rows but the metadata has {expected_rows:,}")
    stop = min(total, limit_rows) if limit_rows else total

    out_root.mkdir(parents=True, exist_ok=True)
    parts = out_root / f".{name}.era.parts"
    stamp = parts / "prompts.sha256"
    if parts.is_dir() and (not stamp.is_file() or stamp.read_text().strip() != (prompts.sha256 or "")):
        shutil.rmtree(parts)  # parts from another prompt set: never mix them
    parts.mkdir(exist_ok=True)
    stamp.write_text(prompts.sha256 or "")
    con = duckdb.connect()
    try:
        for lo in range(0, stop, chunk_rows):
            hi = min(stop, lo + chunk_rows)
            part = parts / f"{lo:012d}-{hi:012d}.parquet"
            if part.is_file():
                if progress:
                    progress(hi, stop)
                continue
            x = sem._read_rows(mats, starts, lo, hi)  # unit length, float32
            s = prompts.score(x)
            del x
            chunk = {"pos": np.arange(lo, hi, dtype=np.int64), **s}  # DuckDB scans it by name below
            tmp = part.with_suffix(".tmp")
            con.execute(f"COPY (SELECT * FROM chunk) TO '{_sql(tmp)}' (FORMAT parquet)")
            os.replace(tmp, part)
            del chunk, s
            if progress:
                progress(hi, stop)
        built_at = datetime.now(UTC).isoformat(timespec="seconds")
        kv = {
            "era_schema": str(ERA_SCHEMA),
            "total": str(total),
            "prompts_sha256": prompts.sha256 or "",
            "model": prompts.model,
            "built_at": built_at,
        }
        kv_sql = ", ".join(f"'{k}': '{_sql(v)}'" for k, v in kv.items())
        final = scores_path(out_root, name)
        tmp = final.with_name(f".{final.name}.building")
        wanted = [parts / f"{lo:012d}-{min(stop, lo + chunk_rows):012d}.parquet" for lo in range(0, stop, chunk_rows)]
        files = "[" + ", ".join(f"'{_sql(p)}'" for p in wanted) + "]"
        con.execute(
            f"COPY (SELECT * FROM read_parquet({files}) ORDER BY pos) TO '{_sql(tmp)}' "
            f"(FORMAT parquet, KV_METADATA {{{kv_sql}}})"
        )
        os.replace(tmp, final)
    finally:
        con.close()
    shutil.rmtree(parts, ignore_errors=True)
    return {"rows": stop, "total": total, "prompts_sha256": prompts.sha256, "built_at": built_at}


def _sql(value: object) -> str:
    return str(value).replace("'", "''")


def era_join_sql(path: Path) -> str:
    """The columns the index takes from a scores file, keyed by ``pos`` (see ``Archive.build_index``)."""
    eras = "[" + ", ".join(f"'{e}'" for e in ERAS) + "]"
    kinds = "[" + ", ".join(f"'{k}'" for k in KINDS) + "]"
    return (
        f"SELECT pos, list_extract({eras}, era::INTEGER + 1) AS clip_era, round(era_conf, 3)::REAL AS clip_era_conf, "
        f"list_extract({kinds}, kind::INTEGER + 1) AS clip_kind, round(kind_conf, 3)::REAL AS clip_kind_conf "
        f"FROM read_parquet('{_sql(path)}')"
    )
