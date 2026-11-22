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
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from PIL import Image

from argus_quarry.config import QuarryConfig
from argus_quarry.exif import capture_medium, exif_device, exif_text
from argus_quarry.models import Photograph, SourceRecord, Subject, is_accepted_licence, normalise_licence
from argus_quarry.net import NetClient, RemoteGone, SizeCapExceeded, part_validator_path
from argus_quarry.store import ProvenanceStore

logger = structlog.get_logger()

_ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
#: Formats that land as-is; anything else Pillow decodes (TIFF, GIF, WebP, …) is re-encoded to PNG.
_LANDED_FORMATS = {"JPEG": ".jpg", "PNG": ".png"}


class BudgetReached(Exception):
    """Raised to stop a run cleanly when the total-archive cap would be exceeded."""


@dataclass(frozen=True)
class _Landed:
    """What :meth:`IngestEngine._verify_and_cap` read from a download and where it left the bytes."""

    path: Path
    width: int
    height: int
    phash: str | None
    captured_at: str | None
    camera_make: str | None
    camera_model: str | None


@dataclass
class IngestOutcome:
    status: str  # complete | skipped | duplicate | quarantined | oversize | failed
    photo_id: int | None = None
    filename: str | None = None
    file_size: int = 0
    reason: str | None = None
    #: The URL the bytes actually came from (a mirror's, when the primary was gone).
    fetched_from: str | None = None


def _ext_for(remote_url: str) -> str:
    # Drop any #fragment (a mirror's #bytes= range) and query without parsing them: this runs
    # outside the download's error handling, so a malformed range must fail the download, not the run.
    ext = Path(remote_url.split("#")[0].split("?")[0]).suffix.lower()
    return ext if ext in _ALLOWED_EXTS else ".jpg"


def _unsafe_path_part(record: SourceRecord, existing: Photograph | None) -> str | None:
    """The first category/subject/source that can't be one path component, else ``None``.

    These name the landing folder (``images/<category>/<subject>/``), the pool filename and the
    cache temp file. A separator, ``..`` or NUL would land a file outside ``images/`` or where the
    DB row can't find it, so such a record is refused rather than rewritten: renaming it here would
    break the ``category``/``subject`` columns readers rebuild the path from.
    """
    # The record's own subject names the pool file even when a reused row names the folder, so both are checked.
    checks = [("category", record.category), ("subject", record.subject), ("source", record.source)]
    if existing is not None:
        checks += [("category", existing.category), ("subject", existing.subject)]
    for field_name, value in checks:
        if not value or value in {".", ".."} or any(c in value for c in "/\\\0"):
            return f"unsafe_{field_name}={value!r}"
    return None


def _discard_download(tmp: Path) -> None:
    """Remove a candidate's temp file and any partial of it (its ``.part`` and resume validator)."""
    tmp.unlink(missing_ok=True)
    tmp.with_suffix(tmp.suffix + ".part").unlink(missing_ok=True)
    part_validator_path(tmp).unlink(missing_ok=True)


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


#: EXIF tags: the Exif sub-IFD pointer, ``DateTimeOriginal`` and ``OffsetTimeOriginal``.
_EXIF_IFD, _DATETIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 0x8769, 0x9003, 0x9011
_EXIF_DATETIME = re.compile(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_EXIF_OFFSET = re.compile(r"[+-]\d{2}:\d{2}")
#: Before the first surviving photograph (1826/27), an EXIF date is a camera's unset clock, not a capture.
_EARLIEST_CAPTURE_YEAR = 1826


def _exif_capture_date(image: Image.Image) -> str | None:
    """EXIF ``DateTimeOriginal`` as an ISO 8601 datetime (with ``OffsetTimeOriginal`` when stated), or None.

    Only ``DateTimeOriginal`` counts: IFD0 ``DateTime`` is when the file was last changed. Blank,
    zeroed (``0000:00:00 …``), impossible or future dates read as None. On a scan the date is
    the scan's, not the shot's; the column says it came from EXIF and claims nothing more.
    """
    try:
        raw = image.getexif().get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL)
        if raw is None:
            return None
        m = _EXIF_DATETIME.fullmatch(exif_text(raw))
        if m is None:
            return None
        taken = datetime(*(int(g) for g in m.groups()))  # ValueError on month 13, day 0, ...
        if not _EARLIEST_CAPTURE_YEAR <= taken.year <= datetime.now(UTC).year + 1:
            return None
        stamp = taken.isoformat()
        offset = exif_text(image.getexif().get_ifd(_EXIF_IFD).get(_OFFSET_TIME_ORIGINAL, ""))
        return stamp + offset if _EXIF_OFFSET.fullmatch(offset) else stamp
    except Exception:  # metadata is opportunistic: a malformed EXIF block never fails a landing
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

    def _cap_raised_past(self, seen_bytes: int | None) -> bool:
        """Whether the per-download cap now admits a file that was aborted after ``seen_bytes``."""
        cap = self.config.max_file_bytes
        return cap <= 0 or seen_bytes is None or seen_bytes <= cap

    def _budget_would_exceed(self, extra: int) -> bool:
        cap = self.config.max_total_bytes
        return cap > 0 and (self._pool_bytes + extra) > cap

    def ingest_record(self, record: SourceRecord) -> IngestOutcome:
        # 1. Licence gate — a record with no accepted licence never lands.
        canon = normalise_licence(record.licence)
        existing = self.store.get_by_remote_url(record.remote_url)
        if existing and existing.status in {"complete", "duplicate", "quarantined"}:
            return IngestOutcome(status="skipped", photo_id=existing.id, reason=existing.status)
        if existing and existing.status == "oversize" and not self._cap_raised_past(existing.file_size):
            # Re-downloading up to the same cap would fail the same way; raise it to retry.
            return IngestOutcome(status="skipped", photo_id=existing.id, reason="oversize")

        # Path components come from seed files, slice specs and downloaders: refuse any that would escape
        # the pool before anything is written (DB row included).
        if (unsafe := _unsafe_path_part(record, existing)) is not None:
            if existing is not None:
                self.store.set_status(existing.id, "failed")
            logger.warning("unsafe_path_component", url=record.remote_url, reason=unsafe)
            return IngestOutcome(status="failed", photo_id=existing.id if existing else None, reason=unsafe)

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

        candidates = self._candidates(record)
        for n, (url, rate_source) in enumerate(candidates, start=1):
            # One temp file per candidate: a mirror must never resume another host's partial.
            tmp = self.config.cache_dir / f"{record.source}-{hashlib.sha1(url.encode()).hexdigest()}{_ext_for(url)}"
            try:
                self.net.download(url, tmp, source=rate_source, max_bytes=self.config.max_file_bytes or None)
            except SizeCapExceeded as exc:
                _discard_download(tmp)
                self.store.mark_oversize(photo_id, exc.size)
                logger.info("oversize", url=url, seen_bytes=exc.size, cap=self.config.max_file_bytes)
                return IngestOutcome(
                    status="oversize", photo_id=photo_id, reason=f"over max_file_bytes={self.config.max_file_bytes}"
                )
            except RemoteGone as exc:
                _discard_download(tmp)  # the bytes are gone: a partial of them is never resumable
                if n < len(candidates):
                    logger.info("remote_gone_trying_alternate", url=url, status=exc.status, next=candidates[n][0])
                    continue
                self.store.set_status(photo_id, "failed")
                logger.warning("download_failed", url=url, error=str(exc))
                return IngestOutcome(status="failed", photo_id=photo_id, reason=str(exc))
            except Exception as exc:
                # A transient failure keeps its .part: the next attempt resumes it (NetClient only
                # resumes a partial whose ETag/Last-Modified still matches). The temp file is never kept.
                tmp.unlink(missing_ok=True)
                self.store.set_status(photo_id, "failed")
                logger.warning("download_failed", url=url, error=str(exc))
                return IngestOutcome(status="failed", photo_id=photo_id, reason=str(exc))
            fetched_from = url
            break

        # 2. Verify + apply per-file resolution cap (re-encode/downscale as needed).
        try:
            result = self._verify_and_cap(tmp, _ext_for(fetched_from))
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            self.store.set_status(photo_id, "failed")
            logger.warning("verify_failed", url=record.remote_url, error=str(exc))
            return IngestOutcome(status="failed", photo_id=photo_id, reason=f"decode:{exc}")

        landed = result.path
        file_size = landed.stat().st_size
        sha256 = _sha256_file(landed)

        # 3. Exact-dedup: identical bytes already landed -> no-op.
        dup = self.store.get_by_sha256(sha256)
        if dup is not None and dup.id != photo_id:
            landed.unlink(missing_ok=True)
            self.store.set_status(photo_id, "duplicate")
            logger.info("dedup_skip", url=fetched_from, sha256=sha256[:12])
            return IngestOutcome(
                status="duplicate", photo_id=photo_id, reason=f"dup_of={dup.id}", fetched_from=fetched_from
            )

        # 4. Budget: stop cleanly if this file would push the pool over the cap.
        if self._budget_would_exceed(file_size):
            landed.unlink(missing_ok=True)
            self.store.set_status(photo_id, "pending")  # resumable if the cap is raised
            logger.info("budget_reached", pool_bytes=self._pool_bytes, next_size=file_size)
            raise BudgetReached(f"landing {file_size} bytes would exceed cap {self.config.max_total_bytes}")

        # 5. Land in the raw pool + record (sorted into <category>/<subject>/). A reused
        # row keeps its own category/subject, so the file lands where the DB says it is.
        folder = self.config.images_dir / (existing or record).category / (existing or record).subject
        filename = self._pool_filename(record, sha256, landed.suffix)
        if (folder / filename).exists():  # different bytes (sha256 is unique) sharing a short-hash name
            filename = self._pool_filename(record, sha256, landed.suffix, full_hash=True)
        dest = folder / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        landed.replace(dest)

        # Provenance names the host that served the bytes; remote_url stays the full-res primary.
        self.store.mark_complete(
            photo_id,
            filename=filename,
            sha256=sha256,
            width=result.width,
            height=result.height,
            file_size=file_size,
            phash=result.phash,
            fetched_from=fetched_from if fetched_from != record.remote_url else None,
            captured_at=result.captured_at,
            captured_at_source="exif" if result.captured_at else None,
            camera_make=result.camera_make,
            camera_model=result.camera_model,
            capture_medium=capture_medium(result.camera_make, result.camera_model),
        )
        self._pool_bytes += file_size
        logger.info("landed", url=fetched_from, filename=filename, size=file_size)
        return IngestOutcome(
            status="complete", photo_id=photo_id, filename=filename, file_size=file_size, fetched_from=fetched_from
        )

    # ── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _candidates(record: SourceRecord) -> list[tuple[str, str]]:
        """``(url, rate_source)`` in the order to try them."""
        primary = [(record.remote_url, record.source)]
        mirrors = [(a.remote_url, a.rate_source) for a in record.alternates]
        return mirrors + primary if record.prefer_alternates else primary + mirrors

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

    def _verify_and_cap(self, src: Path, ext: str) -> _Landed:
        """Decode, enforce the megapixel cap, and return where the bytes landed and what they state.

        ``captured_at`` and the camera make/model are read from the downloaded bytes before a
        downscale or re-encode can drop their EXIF.

        The landed suffix follows the decoded format, not the URL: JPEG/PNG keep their
        bytes, other formats are re-encoded to PNG so a TIFF never lands named ``.jpg``.
        """
        with Image.open(src) as im:
            im.load()
            width, height = im.size
            mp = (width * height) / 1_000_000
            meta = {"phash": _compute_phash(im), "captured_at": _exif_capture_date(im)}
            meta["camera_make"], meta["camera_model"] = exif_device(im)

            if mp > self.config.max_megapixels > 0:
                scale = (self.config.max_megapixels / mp) ** 0.5
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                capped = im.convert("RGB").resize(new_size, Image.LANCZOS)
                out = src.with_suffix(".capped.jpg")
                capped.save(out, "JPEG", quality=90, optimize=True)
                src.unlink(missing_ok=True)
                return _Landed(out, new_size[0], new_size[1], **meta)

            suffix = _LANDED_FORMATS.get(im.format or "")
            if suffix is None:
                out = src.with_suffix(".converted.png")
                im.seek(0)  # first frame of an animated GIF/WebP
                frame = im if im.mode in {"1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"} else im.convert("RGB")
                frame.save(out, "PNG", optimize=True)
                src.unlink(missing_ok=True)
                return _Landed(out, width, height, **meta)

        if src.suffix != suffix and not (suffix == ".jpg" and src.suffix == ".jpeg"):
            src = src.replace(src.with_suffix(suffix))
        return _Landed(src, width, height, **meta)

    def _pool_filename(self, record: SourceRecord, sha256: str, ext: str, *, full_hash: bool = False) -> str:
        year = str(record.year) if record.year else "ny"
        base = record.subject.lower()
        return f"{base}_{year}_{record.source}_{sha256 if full_hash else sha256[:8]}{ext}"


@dataclass
class FetchSummary:
    complete: int = 0
    skipped: int = 0
    duplicate: int = 0
    quarantined: int = 0
    oversize: int = 0
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
