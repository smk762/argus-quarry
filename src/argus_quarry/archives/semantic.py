"""Semantic search over an archive's published image embeddings (PD12M's CLIP ViT-L/14).

An archive that ships embeddings row-aligned with its metadata (``items.pos`` is
the row) gets a vector index under ``$QUARRY_HOME/cache/archives/``:

    <name>.semantic.json            pointer: which build is current, plus its shape
    <name>.semantic.<token>/
        index.faiss                 IVF-PQ (inner product) over every row, ~1.3 GB for PD12M
        vectors.f16.npy             the embeddings as float16, ~19 GB for PD12M

FAISS finds candidates from compact PQ codes; the float16 copy then rescores them
exactly (and supplies the query vector for "more like this"). The copy is local
because the archived ``.npy`` files usually sit on a NAS share, where a random
row read costs ~40 ms. A build streams the source files in chunks, so its memory
is bounded by one chunk plus the training sample, never the 36 GB matrix.

Builds never touch the live index: each writes a fresh ``<name>.semantic.<token>``
directory and only then swaps the pointer (``os.replace``), so a reader sees the
old build or the new one, never half of either.

Text queries need the matching CLIP text tower (``[search]`` extra: transformers +
torch). Everything else here needs only faiss + numpy (``[embeddings]``).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

logger = structlog.get_logger()

#: Bumped when the on-disk layout changes; an older build reads as "not indexed".
SEMANTIC_SCHEMA = 1
#: Exact rescoring needs at least this many candidates, so the first pages stay stable as you page.
POOL_MIN = 1024
#: Filter matches at or below this are scored exactly, all of them, from the float16 copy.
EXACT_MAX = 20_000
DEFAULT_NPROBE = 32
DEFAULT_CHUNK_ROWS = 131_072
#: Rows read per contiguous training block: spread over the archive, cheap on a NAS.
TRAIN_BLOCK = 2048
#: Below this many rows a flat (exact) index beats IVF-PQ and needs no training.
FLAT_MAX = 100_000


class SemanticUnavailable(RuntimeError):
    """No usable vector index / encoder (raised as ArchiveUnavailable by the archive)."""


def _np():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise SemanticUnavailable("semantic search requires: pip install argus-quarry[embeddings]") from exc
    return np


def _faiss():
    try:
        import faiss
    except ImportError as exc:  # pragma: no cover
        raise SemanticUnavailable("semantic search requires: pip install argus-quarry[embeddings]") from exc
    return faiss


def default_factory(rows: int, dim: int = 768) -> str:
    """The FAISS index for ``rows`` vectors: exact when small, IVF-PQ (up to 96 bytes/vector) when large.

    PQ needs its sub-quantizer count to divide ``dim``: 96 for CLIP ViT-L/14's 768.
    """
    if rows <= FLAT_MAX:
        return "Flat"
    nlist = 4096 if rows >= 2_000_000 else 1024
    m = next(m for m in range(min(96, dim), 0, -1) if dim % m == 0)
    return f"IVF{nlist},PQ{m}"


# ── build ────────────────────────────────────────────────────────────────
@dataclass
class _Source:
    """One ``.npy`` matrix read with plain file I/O. Memory-mapping 36 GB would let the
    page cache count against the build's resident memory (15 GB peak on the real
    archive); seek + read keeps the build at one chunk plus the index."""

    path: Path
    offset: int  # where the row data starts
    rows: int
    dim: int
    dtype: Any


def _open_sources(files: Sequence[Path]) -> tuple[list[_Source], int]:
    np = _np()
    sources: list[_Source] = []
    dim = None
    for f in files:
        with open(f, "rb") as fh:
            version = np.lib.format.read_magic(fh)
            if version == (1, 0):
                shape, fortran, dtype = np.lib.format.read_array_header_1_0(fh)
            else:
                shape, fortran, dtype = np.lib.format.read_array_header_2_0(fh)
            offset = fh.tell()
        if len(shape) != 2 or dtype.kind != "f" or fortran:
            raise SemanticUnavailable(f"{f.name}: expected a 2-D C-order float matrix, got {dtype} {shape}")
        if dim is not None and shape[1] != dim:
            raise SemanticUnavailable(f"{f.name}: dimension {shape[1]} differs from {dim}")
        dim = shape[1]
        sources.append(_Source(f, offset, shape[0], dim, dtype))
    if not sources:
        raise SemanticUnavailable("no embedding files found")
    return sources, dim


def _read_rows(sources: list[_Source], starts: list[int], lo: int, hi: int):
    """Global rows [lo, hi) as float32, across file boundaries, L2-normalised."""
    np = _np()
    parts = []
    for src, s in zip(sources, starts, strict=True):
        a, b = max(lo, s), min(hi, s + src.rows)
        if a < b:
            with open(src.path, "rb") as fh:
                fh.seek(src.offset + (a - s) * src.dim * src.dtype.itemsize)
                raw = np.fromfile(fh, dtype=src.dtype, count=(b - a) * src.dim)
            if raw.size != (b - a) * src.dim:
                raise SemanticUnavailable(f"{src.path.name}: truncated (short read at row {a - s})")
            parts.append(raw.reshape(b - a, src.dim).astype(np.float32, copy=False))
    x = np.concatenate(parts) if len(parts) > 1 else parts[0]
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    np.divide(x, norms, out=x, where=norms > 0)
    return x


def build_vector_index(
    files: Sequence[Path],
    out_root: Path,
    name: str,
    *,
    expected_rows: int,
    model: str,
    factory: str | None = None,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    train_rows: int | None = None,
    threads: int | None = None,
    progress=None,  # noqa: ANN001  (callable(done, total) or None)
) -> dict[str, Any]:
    """Build ``<name>.semantic.*`` from row-aligned embedding files; returns the pointer's metadata.

    ``expected_rows`` is the metadata row count: a different total means the files
    are not the matrix that ``items.pos`` points into, and nothing is written.
    """
    np = _np()
    faiss = _faiss()
    mats, dim = _open_sources(files)
    sizes = [m.rows for m in mats]
    starts = [sum(sizes[:i]) for i in range(len(sizes))]
    total = sum(sizes)
    if total != expected_rows:
        raise SemanticUnavailable(
            f"{name}: embeddings have {total:,} rows but the metadata has {expected_rows:,}: not row-aligned"
        )
    if threads:
        faiss.omp_set_num_threads(int(threads))
    factory = factory or default_factory(total, dim)

    out_root.mkdir(parents=True, exist_ok=True)
    token = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    building = out_root / f".{name}.semantic.{token}.building"
    final = out_root / f"{name}.semantic.{token}"
    building.mkdir()
    done = False
    try:
        index = faiss.index_factory(dim, factory, faiss.METRIC_INNER_PRODUCT)
        if not index.is_trained:
            ivf = faiss.try_extract_index_ivf(index)
            want = train_rows or min(total, max(40 * (ivf.nlist if ivf else 256), 10_000))
            blocks = max(1, math.ceil(want / TRAIN_BLOCK))
            step = total / blocks
            sample = np.concatenate(
                [
                    _read_rows(mats, starts, int(i * step), min(total, int(i * step) + TRAIN_BLOCK))
                    for i in range(blocks)
                ]
            )[:want]
            logger.info("semantic_train", archive=name, rows=len(sample), factory=factory)
            index.train(sample)
            del sample

        # Written with plain appends, not open_memmap: its dirty pages would count as resident too.
        with open(building / "vectors.f16.npy", "wb") as out:
            header = {"descr": np.lib.format.dtype_to_descr(np.dtype(np.float16)), "fortran_order": False}
            np.lib.format.write_array_header_1_0(out, {**header, "shape": (total, dim)})
            for lo in range(0, total, chunk_rows):
                hi = min(total, lo + chunk_rows)
                x = _read_rows(mats, starts, lo, hi)
                out.write(x.astype(np.float16).tobytes())
                index.add(x)  # ids are sequential, so a hit's id *is* its items.pos
                if progress:
                    progress(hi, total)
        faiss.write_index(index, str(building / "index.faiss"))
        del index
        meta = {
            "schema": SEMANTIC_SCHEMA,
            "dir": final.name,
            "rows": total,
            "dim": dim,
            "factory": factory,
            "model": model,
            "sources": [f.name for f in files],
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (building / "meta.json").write_text(json.dumps(meta, indent=2))
        building.rename(final)
        pointer = pointer_path(out_root, name)
        tmp = pointer.with_name(f".{pointer.name}.{token}")
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, pointer)
        done = True
    finally:
        if not done:
            shutil.rmtree(building, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
    # Older builds are unreachable now. A server that still maps one keeps its open
    # files until it reloads (POSIX unlink semantics), so this is safe to do live.
    for old in out_root.glob(f"{name}.semantic.*"):
        if old.is_dir() and old.name != final.name:
            shutil.rmtree(old, ignore_errors=True)
    return meta


# ── read ─────────────────────────────────────────────────────────────────
def pointer_path(root: Path, name: str) -> Path:
    return root / f"{name}.semantic.json"


def read_pointer(root: Path, name: str) -> dict[str, Any] | None:
    """The current build's metadata, or None when absent, foreign or from another schema."""
    try:
        meta = json.loads(pointer_path(root, name).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("schema") != SEMANTIC_SCHEMA or not meta.get("dir"):
        return None
    if not (root / meta["dir"]).is_dir():
        return None
    return meta


@dataclass
class Hit:
    pos: int
    score: float


class VectorIndex:
    """A loaded build: the FAISS index and the float16 matrix, both memory-mapped."""

    def __init__(self, root: Path, meta: dict[str, Any]) -> None:
        np = _np()
        faiss = _faiss()
        self.meta = meta
        self.rows: int = meta["rows"]
        d = root / meta["dir"]
        self.index = faiss.read_index(str(d / "index.faiss"), faiss.IO_FLAG_MMAP)
        self.vectors = np.load(d / "vectors.f16.npy", mmap_mode="r")
        if self.index.ntotal != self.rows or self.vectors.shape[0] != self.rows:
            raise SemanticUnavailable(f"{d.name}: index, vectors and pointer disagree on the row count")
        self.ivf = faiss.try_extract_index_ivf(self.index)

    def vector(self, pos: int):
        return self.vectors[pos].astype("float32")

    def _exact(self, q, positions) -> list[Hit]:
        np = _np()
        positions = np.sort(np.asarray(positions, dtype=np.int64))  # sorted: sequential-ish reads
        scores = self.vectors[positions].astype(np.float32) @ q
        return [Hit(int(p), float(s)) for p, s in zip(positions, scores, strict=True)]

    def search(self, q, k: int, *, allowed=None, exclude: int | None = None) -> list[Hit]:
        """The ``k`` rows nearest ``q``, best first, restricted to the ``allowed`` boolean mask.

        Scores are exact cosines (the vectors are unit length) from the float16 copy.
        """
        np = _np()
        faiss = _faiss()
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if norm == 0:
            return []
        q = q / norm
        if allowed is not None:
            if exclude is not None and 0 <= exclude < self.rows:
                allowed[exclude] = False
            n_allowed = int(allowed.sum())
        if allowed is not None and n_allowed <= max(EXACT_MAX, k):
            hits = self._exact(q, np.flatnonzero(allowed))
        else:
            pool = max(POOL_MIN, 1 << math.ceil(math.log2(max(2 * k, 1))))
            if exclude is not None:
                pool += 1
            params: Any = None
            sel = bitmap = None
            if allowed is not None:
                bitmap = np.packbits(allowed, bitorder="little")
                sel = faiss.IDSelectorBitmap(self.rows, faiss.swig_ptr(bitmap))
            if self.ivf is not None:
                nprobe = DEFAULT_NPROBE
                if allowed is not None:  # a narrow filter leaves fewer hits per list: probe more lists
                    nprobe = math.ceil(DEFAULT_NPROBE * self.rows / max(n_allowed, 1))
                nprobe = min(self.ivf.nlist, nprobe)
                params = (
                    faiss.SearchParametersIVF(sel=sel, nprobe=nprobe)
                    if sel
                    else faiss.SearchParametersIVF(nprobe=nprobe)
                )
            elif sel is not None:
                params = faiss.SearchParameters(sel=sel)
            _, ids = self.index.search(q.reshape(1, -1), min(pool, self.rows), params=params)
            del bitmap  # the selector only borrows it: keep it alive through the search
            candidates = ids[0][ids[0] >= 0]
            if exclude is not None:
                candidates = candidates[candidates != exclude]
            hits = self._exact(q, candidates)
        if exclude is not None:
            hits = [h for h in hits if h.pos != exclude]
        hits.sort(key=lambda h: (-h.score, h.pos))
        return hits[:k]


_loaded: dict[Path, tuple[str, VectorIndex]] = {}
_loaded_lock = threading.Lock()


def load_vector_index(root: Path, name: str) -> VectorIndex | None:
    """The current build, loaded once per process and reloaded after a rebuild flips the pointer."""
    meta = read_pointer(root, name)
    if meta is None:
        return None
    key = pointer_path(root, name)
    with _loaded_lock:
        cached = _loaded.get(key)
        if cached and cached[0] == meta["dir"]:
            return cached[1]
        index = VectorIndex(root, meta)
        _loaded[key] = (meta["dir"], index)
        return index


# ── text encoder ─────────────────────────────────────────────────────────
class TextEncoder(Protocol):
    def encode(self, text: str) -> Any:
        """One unit-length (or normalisable) vector in the image embeddings' space."""


class ClipTextEncoder:
    """The CLIP text tower (transformers), loaded lazily, CPU by default."""

    def __init__(self, model: str, device: str | None = None) -> None:
        try:
            import torch
            from transformers import CLIPTextModelWithProjection, CLIPTokenizer
        except ImportError as exc:
            raise SemanticUnavailable("text queries require: pip install argus-quarry[search]") from exc
        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.tokenizer = CLIPTokenizer.from_pretrained(model)
        self.model = CLIPTextModelWithProjection.from_pretrained(model).eval().to(self.device)
        self._lock = threading.Lock()

    def encode(self, text: str):
        tokens = self.tokenizer([text], padding=True, truncation=True, max_length=77, return_tensors="pt")
        with self._lock, self._torch.inference_mode():
            out = self.model(**{k: v.to(self.device) for k, v in tokens.items()}).text_embeds
        return out[0].float().cpu().numpy()


_encoders: dict[str, TextEncoder] = {}
_encoder_override: TextEncoder | None = None
_encoder_lock = threading.Lock()


def set_text_encoder(encoder: TextEncoder | None) -> None:
    """Use ``encoder`` for every text query in this process (tests; or a remote encoder). None resets."""
    global _encoder_override
    _encoder_override = encoder


def get_text_encoder(model: str) -> TextEncoder:
    """The encoder for ``model``; ``QUARRY_CLIP_MODEL`` may name a local copy of the same weights."""
    if _encoder_override is not None:
        return _encoder_override
    model = os.environ.get("QUARRY_CLIP_MODEL", "").strip() or model
    with _encoder_lock:
        if model not in _encoders:
            logger.info("clip_text_encoder_load", model=model)
            _encoders[model] = ClipTextEncoder(model)
        return _encoders[model]
