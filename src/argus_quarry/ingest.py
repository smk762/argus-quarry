"""Ingest — turn a :class:`SourceRecord` into bytes on disk + a DB row.

The pipeline per record is: **licence-gate → resume-check → download → verify →
cap → SHA256 dedup → land in the raw pool → record**. Everything is idempotent:
reruns resume partials, skip completed/quarantined candidates, and never
duplicate bytes (SHA256 ``UNIQUE``). The total-archive budget is enforced from
an O(1) running byte counter seeded once from the DB. Files land under
``images/<category>/<subject>/`` so a single pool serves every LoRA workflow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import structlog
from PIL import Image

from argus_quarry.config import QuarryConfig
from argus_quarry.models import Photograph, SourceRecord, Subject, is_accepted_licence, normalise_licence
from argus_quarry.net import NetClient, SizeCapExceeded
from argus_quarry.store import ProvenanceStore

logger = structlog.get_logger()

_ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}


class BudgetReached(Exception):
    """Raised to stop a run cleanly when the total-archive cap would be exceeded."""


@dataclass
class IngestOutcome:
    status: str  # complete | skipped | duplicate | quarantined | failed
    photo_id: int | None = None
    filename: str | None = None
    file_size: int = 0
    reason: str | None = None


def _ext_for(remote_url: str) -> str:
    ext = Path(remote_url.split("?")[0]).suffix.lower()
    return ext if ext in _ALLOWED_EXTS else ".jpg"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_phash(image: Image.Image) -> str | None:
    """Opportunistic perceptual hash (informational only). Absent if ImageHash unavailable."""
    try:
        import imagehash
    except Exception:
        return None
    try:
        return str(imagehash.phash(image))
    except Exception:
        return None


class IngestEngine:
    """Stateful ingester for one run — owns the running byte budget counter."""

    def __init__(self, config: QuarryConfig, store: ProvenanceStore, net: NetClient) -> None:
        self.config = config
        self.store = store
        self.net = net
        self.config.ensure_dirs()
        self._pool_bytes = store.total_bytes()

    @property
    def pool_bytes(self) -> int:
        return self._pool_bytes

    def _budget_would_exceed(self, extra: int) -> bool:
        cap = self.config.max_total_bytes
        return cap > 0 and (self._pool_bytes + extra) > cap

    def ingest_record(self, record: SourceRecord) -> IngestOutcome:
        # 1. Licence gate — a record with no accepted licence never lands.
        canon = normalise_licence(record.licence)
        existing = self.store.get_by_remote_url(record.remote_url)
        if existing and existing.status in {"complete", "duplicate", "quarantined"}:
            return IngestOutcome(status="skipped", photo_id=existing.id, reason=existing.status)

        if not is_accepted_licence(record.licence):
            self._quarantine(record, existing)
            logger.info("quarantined", url=record.remote_url, licence=record.licence)
            return IngestOutcome(status="quarantined", reason=f"licence={record.licence!r}")

        # Early budget stop (pool already at/over cap before we fetch anything more).
        if self._budget_would_exceed(0):
            raise BudgetReached(f"pool at {self._pool_bytes} bytes, cap {self.config.max_total_bytes}")

        subject_id = self._upsert_subject(record)
        photo_id = existing.id if existing else self._insert_pending(record, subject_id, canon or record.licence)
        self.store.set_status(photo_id, "downloading")

        tmp = (
            self.config.cache_dir
            / f"{record.source}-{hashlib.sha1(record.remote_url.encode()).hexdigest()}{_ext_for(record.remote_url)}"
        )
        try:
            self.net.download(record.remote_url, tmp, source=record.source)
        except SizeCapExceeded:
            self.store.set_status(photo_id, "failed")
            return IngestOutcome(status="failed", photo_id=photo_id, reason="size_cap")
        except Exception as exc:
            self.store.set_status(photo_id, "failed")
            logger.warning("download_failed", url=record.remote_url, error=str(exc))
            return IngestOutcome(status="failed", photo_id=photo_id, reason=str(exc))

        # 2. Verify + apply per-file resolution cap (re-encode/downscale as needed).
        try:
            landed, width, height, phash = self._verify_and_cap(tmp, _ext_for(record.remote_url))
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            self.store.set_status(photo_id, "failed")
            logger.warning("verify_failed", url=record.remote_url, error=str(exc))
            return IngestOutcome(status="failed", photo_id=photo_id, reason=f"decode:{exc}")

        file_size = landed.stat().st_size
        sha256 = _sha256_file(landed)

        # 3. Exact-dedup: identical bytes already landed -> no-op.
        dup = self.store.get_by_sha256(sha256)
        if dup is not None and dup.id != photo_id:
            landed.unlink(missing_ok=True)
            self.store.set_status(photo_id, "duplicate")
            logger.info("dedup_skip", url=record.remote_url, sha256=sha256[:12])
            return IngestOutcome(status="duplicate", photo_id=photo_id, reason=f"dup_of={dup.id}")

        # 4. Budget: stop cleanly if this file would push the pool over the cap.
        if self._budget_would_exceed(file_size):
            landed.unlink(missing_ok=True)
            self.store.set_status(photo_id, "pending")  # resumable if the cap is raised
            logger.info("budget_reached", pool_bytes=self._pool_bytes, next_size=file_size)
            raise BudgetReached(f"landing {file_size} bytes would exceed cap {self.config.max_total_bytes}")

        # 5. Land in the raw pool + record (sorted into <category>/<subject>/).
        filename = self._pool_filename(record, sha256, landed.suffix)
        dest = self.config.images_dir / record.category / record.subject / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        landed.replace(dest)

        self.store.mark_complete(
            photo_id,
            filename=filename,
            sha256=sha256,
            width=width,
            height=height,
            file_size=file_size,
            phash=phash,
        )
        self._pool_bytes += file_size
        logger.info("landed", url=record.remote_url, filename=filename, size=file_size)
        return IngestOutcome(status="complete", photo_id=photo_id, filename=filename, file_size=file_size)

    # ── helpers ─────────────────────────────────────────────────────
    def _upsert_subject(self, record: SourceRecord) -> int:
        return self.store.upsert_subject(
            Subject(
                name=record.subject,
                category=record.category,
                wikidata_id=record.wikidata_id,
                birth_year=record.birth_year,
                death_year=record.death_year,
                occupation=record.occupation,
            )
        )

    def _insert_pending(self, record: SourceRecord, subject_id: int, licence: str) -> int:
        return self.store.insert_pending(
            Photograph(
                subject_id=subject_id,
                subject=record.subject,
                category=record.category,
                title=record.title,
                photographer=record.photographer,
                year=record.year,
                source=record.source,
                source_url=record.source_url,
                licence=licence,
                attribution=record.attribution,
                remote_url=record.remote_url,
                status="pending",
            )
        )

    def _quarantine(self, record: SourceRecord, existing: Photograph | None) -> None:
        if existing is not None:
            self.store.set_status(existing.id, "quarantined")
            return
        subject_id = self._upsert_subject(record)
        pid = self._insert_pending(record, subject_id, record.licence)
        self.store.set_status(pid, "quarantined")

    def _verify_and_cap(self, src: Path, ext: str) -> tuple[Path, int, int, str | None]:
        """Decode, enforce the megapixel cap, and return (landed_path, w, h, phash)."""
        with Image.open(src) as im:
            im.load()
            width, height = im.size
            mp = (width * height) / 1_000_000
            phash = _compute_phash(im)

            if mp > self.config.max_megapixels > 0:
                scale = (self.config.max_megapixels / mp) ** 0.5
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                capped = im.convert("RGB").resize(new_size, Image.LANCZOS)
                out = src.with_suffix(".capped.jpg")
                capped.save(out, "JPEG", quality=90, optimize=True)
                src.unlink(missing_ok=True)
                return out, new_size[0], new_size[1], phash

        return src, width, height, phash

    def _pool_filename(self, record: SourceRecord, sha256: str, ext: str) -> str:
        year = str(record.year) if record.year else "ny"
        base = record.subject.lower()
        return f"{base}_{year}_{record.source}_{sha256[:8]}{ext}"


@dataclass
class FetchSummary:
    complete: int = 0
    skipped: int = 0
    duplicate: int = 0
    quarantined: int = 0
    failed: int = 0
    budget_reached: bool = False

    def record(self, outcome: IngestOutcome) -> None:
        setattr(self, outcome.status, getattr(self, outcome.status) + 1)


def fetch(engine: IngestEngine, records) -> FetchSummary:
    """Ingest an iterable of :class:`SourceRecord`, stopping cleanly on budget.

    ``records`` is any iterable (a downloader stream, a flattened multi-subject
    generator, or a fixture list). Returns per-status counts.
    """
    summary = FetchSummary()
    for record in records:
        try:
            outcome = engine.ingest_record(record)
        except BudgetReached:
            summary.budget_reached = True
            logger.info("run_stopped", reason="budget_reached", pool_bytes=engine.pool_bytes)
            break
        summary.record(outcome)
    return summary
