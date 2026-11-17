"""Export — publish a clean, curator-ready ``<category>/<subject>/`` tree into ``DATASET_DIR``.

Quarry fetches into a raw pool it fully owns; ``export`` then publishes a
filtered view (symlink by default, ``--copy`` when a mount can't cross the
boundary). Because the pool stays separate, a subset (e.g. only ``CC0``, or a
single category) can be re-published without re-downloading, and a curator scan
only ever sees clean images sorted by category.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from argus_cortex.provenance import Provenance

from argus_quarry.config import QuarryConfig
from argus_quarry.models import Photograph
from argus_quarry.store import ProvenanceStore

logger = structlog.get_logger()


@dataclass
class ExportResult:
    dest: str
    mode: str
    published: int = 0
    skipped: int = 0
    missing: int = 0
    subjects: set[str] = field(default_factory=set)


#: Who wrote a sidecar. Darkroom records share the ``<stem>.json`` naming, so a reader checks this first.
PRODUCER = "argus-quarry"
#: Version of the sidecar shape; bump it when a field changes meaning or goes away.
SIDECAR_SCHEMA = "argus-quarry.provenance/1"
#: Categories whose photos show a real person. Their subjects never consented to their likeness being used.
LIKENESS_CATEGORIES = frozenset({"identity"})
#: The row fields a sidecar carries: licence, where the bytes came from, who made them, and their hash.
_PROVENANCE_FIELDS = (
    "category", "subject", "title", "photographer", "year", "source", "source_url", "licence",
    "attribution", "remote_url", "fetched_from", "sha256", "width", "height", "file_size", "downloaded_at",
)  # fmt: skip


def provenance_path(dst: Path) -> Path:
    """The sidecar beside a published file: ``a.jpg`` -> ``a.json``, the name curator's ``--carry-sidecars`` carries."""
    return dst.with_suffix(".json")


def provenance_record(ph: Photograph) -> dict:
    """The sidecar for *ph*: its row's provenance, the suite's shared ``Provenance`` and the likeness mark.

    A licence covers the photo, not the person in it. Every photo in a person category is marked
    ``likeness_consent: false`` explicitly (argus-mask#19), so curator and mask refuse it as a face
    source rather than read consent into a missing field. Other categories carry no mark.
    """
    record: dict = {"producer": PRODUCER, "schema": SIDECAR_SCHEMA, **ph.model_dump(include=set(_PROVENANCE_FIELDS))}
    record["provenance"] = Provenance(
        kind="stock",
        source=ph.source_url or ph.remote_url or ph.source,
        license=ph.licence or "",
        notes=ph.attribution or "",
    ).model_dump(mode="json")
    if ph.category in LIKENESS_CATEGORIES:
        record["likeness_consent"] = False
        record["likeness_note"] = (
            "a real, possibly named person; the licence covers the photo, not consent to use their likeness"
        )
    return record


def _safe_part(value: str | None) -> bool:
    """True if ``value`` is a single path component (no separator, ``..``, NUL or absolute path)."""
    return bool(value) and value not in {".", ".."} and not any(c in value for c in "/\\\0")


def _within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _write_provenance(ph: Photograph, dst: Path) -> None:
    """Write ``dst``'s provenance sidecar atomically, so a reader never sees half of one."""
    sidecar = provenance_path(dst)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_name(f".{sidecar.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(provenance_record(ph), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, sidecar)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _publish_one(src: Path, dst: Path, mode: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if mode == "copy":
        shutil.copy2(src, dst)
    else:  # symlink
        dst.symlink_to(src.resolve())


def export_tree(
    config: QuarryConfig,
    store: ProvenanceStore,
    dest: str | Path,
    *,
    mode: str = "symlink",
    licences: list[str] | None = None,
    subject: str | None = None,
    category: str | None = None,
) -> ExportResult:
    """Publish complete photographs into ``dest`` as ``<category>/<subject>/<file>``.

    Each published file gets a ``<stem>.json`` sidecar (:func:`provenance_record`), written first:
    a photo whose provenance can't be written is not published.
    """
    dest_root = Path(dest)
    # The raw pool and the published tree stay apart: curator scans dest, and must never see the pool's
    # side-car dirs (or publish into them).
    home = config.home
    if _within(dest_root, home) or _within(home, dest_root):
        raise ValueError(f"export dest {dest_root} overlaps $QUARRY_HOME {home}; publish outside the raw pool")
    result = ExportResult(dest=str(dest_root), mode=mode)

    photos = store.iter_photographs(status="complete", licences=licences, subject=subject, category=category)
    for ph in photos:
        if not ph.filename:
            continue
        parts = (ph.category, ph.subject, ph.filename)
        src = config.images_dir.joinpath(*parts)
        dst = dest_root.joinpath(*parts)
        # Rows come from third-party metadata: never read or write outside the pool / dest.
        if not all(_safe_part(p) for p in parts) or not _within(src, config.images_dir) or not _within(dst, dest_root):
            logger.warning("export_unsafe_path", category=ph.category, subject=ph.subject, filename=ph.filename)
            result.skipped += 1
            continue
        if not src.exists():
            logger.warning("export_source_missing", subject=ph.subject, filename=ph.filename)
            result.missing += 1
            continue
        try:
            _write_provenance(ph, dst)
            _publish_one(src, dst, mode)
            result.published += 1
            result.subjects.add(f"{ph.category}/{ph.subject}")
        except Exception as exc:
            provenance_path(dst).unlink(missing_ok=True)  # no sidecar for a file that didn't publish
            logger.warning("export_failed", filename=ph.filename, error=str(exc))
            result.skipped += 1

    logger.info(
        "export_done",
        dest=str(dest_root),
        mode=mode,
        published=result.published,
        subjects=len(result.subjects),
    )
    return result
