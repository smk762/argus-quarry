"""Export — publish a clean, curator-ready ``Person_Name/`` tree into ``DATASET_DIR``.

Quarry fetches into a raw pool it fully owns; ``export`` then publishes a
filtered view (symlink by default, ``--copy`` when a mount can't cross the
boundary). Because the pool stays separate, a subset (e.g. only ``CC0``) can be
re-published without re-downloading, and a curator scan only ever sees clean
images.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from argus_quarry.config import QuarryConfig
from argus_quarry.store import ProvenanceStore

logger = structlog.get_logger()


@dataclass
class ExportResult:
    dest: str
    mode: str
    published: int = 0
    skipped: int = 0
    missing: int = 0
    people: set[str] = field(default_factory=set)


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
    person: str | None = None,
) -> ExportResult:
    """Publish complete photographs into ``dest`` as ``Person_Name/<file>``."""
    dest_root = Path(dest)
    result = ExportResult(dest=str(dest_root), mode=mode)

    photos = store.iter_photographs(status="complete", licences=licences, person=person)
    for ph in photos:
        if not ph.filename:
            continue
        src = config.images_dir / ph.person_name / ph.filename
        if not src.exists():
            logger.warning("export_source_missing", person=ph.person_name, filename=ph.filename)
            result.missing += 1
            continue
        dst = dest_root / ph.person_name / ph.filename
        try:
            _publish_one(src, dst, mode)
            result.published += 1
            result.people.add(ph.person_name)
        except Exception as exc:
            logger.warning("export_failed", filename=ph.filename, error=str(exc))
            result.skipped += 1

    logger.info(
        "export_done",
        dest=str(dest_root),
        mode=mode,
        published=result.published,
        people=len(result.people),
    )
    return result
