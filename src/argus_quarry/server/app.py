"""Read-only provenance API for argus-quarry (peer to argus-curator on :8102).

The tiny FastAPI server DESIGN.md section 9 deferred — provenance queries only,
no mutation endpoints. It powers the argus-studio ``/gallery`` view.

Routes:

    GET /health
    GET /stats
    GET /subjects?category=
    GET /photos?category=&subject=&licence=&source=&status=&limit=&offset=
    GET /photos/{photo_id}
    GET /thumb?id=<photo id>&size=<px>   -> image/webp

The store lives under ``QUARRY_HOME`` (env var, or ``create_app(home=...)``).
A short-lived connection is opened per request, so a concurrently running
``argus-quarry fetch`` is always visible (SQLite WAL readers).
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import Response
except ImportError as exc:  # pragma: no cover
    raise ImportError("Server requires: pip install argus-quarry[server]") from exc

import structlog
from PIL import Image

from argus_quarry import __version__
from argus_quarry.config import QuarryConfig
from argus_quarry.models import Photograph
from argus_quarry.store import ProvenanceStore, ProvenanceUnavailable, has_wal_content

logger = structlog.get_logger()

THUMB_DEFAULT = 384  # longest-edge px for /thumb webp output
THUMB_MAX = 1024  # requested sizes are capped here
PAGE_DEFAULT = 100  # /photos page size
PAGE_MAX = 500

# The wire shape for a photograph (``subject_id`` / ``phash`` stay internal).
_PHOTO_FIELDS = (
    "id",
    "subject",
    "category",
    "title",
    "photographer",
    "year",
    "source",
    "source_url",
    "licence",
    "attribution",
    "width",
    "height",
    "file_size",
    "filename",
    "sha256",
    "remote_url",
    "status",
    "downloaded_at",
)


def _photo_json(photo: Photograph) -> dict[str, Any]:
    data = photo.model_dump(mode="json")
    return {k: data.get(k) for k in _PHOTO_FIELDS}


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


_UNAVAILABLE = "provenance database unavailable"


def _make_opener(db_path: Path):
    """Build the per-request store opener, remembering the mode that worked.

    The mount's writability cannot change while the process lives, so resolving
    it once spares every later request the failed connects. The *connection* is
    still per-request, which is what keeps a concurrent ``fetch`` visible.

    An ``immutable`` mode is re-validated against the WAL on each use: it is only
    safe while no ``-wal`` exists, and a `fetch` can create one at any time.
    """
    cached: dict[str, str] = {}

    def _open() -> ProvenanceStore:
        mode = cached.get("mode")
        if mode == "read_only" or (mode == "immutable" and not has_wal_content(db_path)):
            try:
                return ProvenanceStore(db_path, **{mode: True})
            except (OSError, sqlite3.Error):
                cached.pop("mode", None)  # mount changed under us; re-resolve below
        try:
            store = ProvenanceStore.open_readable(db_path)
        except FileNotFoundError as exc:
            logger.error("provenance_db_missing", db=str(db_path), error=str(exc))
            raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
        except ProvenanceUnavailable as exc:
            logger.error("provenance_db_unavailable", db=str(db_path), error=str(exc))
            raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
        if cached.get("mode") != store.open_mode:
            logger.info("provenance_db_opened", db=str(db_path), mode=store.open_mode)
        cached["mode"] = store.open_mode
        return store

    return _open


def create_app(
    cors: bool = False,
    cors_origins: list[str] | None = None,
    home: str | Path | None = None,
) -> FastAPI:
    """Create the read-only quarry FastAPI application.

    ``home`` overrides the pool root; by default it is resolved from the
    ``QUARRY_HOME`` env var (falling back to ``./quarry``), exactly like the CLI.
    """
    config = QuarryConfig.from_env(home=Path(home)) if home else QuarryConfig.from_env()

    app = FastAPI(
        title="Argus Quarry",
        description="Read-only provenance queries over the raw public-domain/CC0 image pool.",
        version=__version__,
    )

    if cors:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins or ["*"],
            # Credentials only make sense for an explicit origin list. With the
            # "*" default Starlette reflects whatever Origin is presented, which
            # would let any site read the API with the caller's cookies.
            allow_credentials=bool(cors_origins),
            allow_methods=["GET", "HEAD"],  # the whole surface is read-only
            allow_headers=["*"],
        )

    logger.info("server_ready", quarry_home=str(config.home), db=str(config.db_path))
    _open_store = _make_opener(config.db_path)

    @app.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        """Readiness, not just liveness: a process that cannot open the pool is not serving."""

        def _probe() -> str | None:
            try:
                with _open_store():
                    return None
            except HTTPException as exc:
                return str(exc.detail)

        detail = await asyncio.to_thread(_probe)
        if detail is not None:
            response.status_code = 503
        return {
            "status": "ok" if detail is None else "degraded",
            "service": "argus-quarry",
            "version": __version__,
            "quarry_home": str(config.home.resolve()),
            "database": "ok" if detail is None else detail,
        }

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        def _stats() -> dict[str, Any]:
            with _open_store() as store:
                return store.stats()

        return await asyncio.to_thread(_stats)

    @app.get("/subjects")
    async def subjects(
        category: str | None = Query(None, description="only this category (identity/wardrobe/setting/concept)"),
    ) -> dict[str, Any]:
        def _subjects() -> dict[str, Any]:
            with _open_store() as store:
                return {"subjects": store.subjects_with_counts(category=category)}

        return await asyncio.to_thread(_subjects)

    @app.get("/photos")
    async def photos(
        category: str | None = Query(None, description="filter by category"),
        subject: str | None = Query(None, description="filter by subject folder name, e.g. Albert_Einstein"),
        licence: str | None = Query(None, description="comma-separated licences, e.g. CC0,PD"),
        source: str | None = Query(None, description="filter by source, e.g. commons"),
        status: str = Query("complete", description="filter by status (empty = all statuses)"),
        limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        licences = _split_csv(licence)

        def _page() -> dict[str, Any]:
            filters: dict[str, Any] = {
                "status": status or None,
                "source": source,
                "licences": licences,
                "subject": subject,
                "category": category,
            }
            with _open_store() as store:
                total = store.count_photographs(**filters)
                rows = store.iter_photographs(**filters, limit=limit, offset=offset)
            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "photos": [_photo_json(p) for p in rows],
            }

        return await asyncio.to_thread(_page)

    @app.get("/photos/{photo_id}")
    async def photo(photo_id: int) -> dict[str, Any]:
        def _get() -> Photograph | None:
            with _open_store() as store:
                return store.get_photograph(photo_id)

        ph = await asyncio.to_thread(_get)
        if ph is None:
            raise HTTPException(status_code=404, detail=f"unknown photo id: {photo_id}")
        return _photo_json(ph)

    @app.get("/thumb")
    async def thumb(
        id: int = Query(..., description="photo id"),
        size: int = Query(THUMB_DEFAULT, ge=1, description=f"longest-edge px (capped at {THUMB_MAX})"),
    ) -> Response:
        size = min(size, THUMB_MAX)

        def _get() -> Photograph | None:
            with _open_store() as store:
                return store.get_photograph(id)

        ph = await asyncio.to_thread(_get)
        if ph is None or not ph.filename:
            raise HTTPException(status_code=404, detail=f"unknown photo id: {id}")
        # Pooled-file path, computed the same way export/verify do.
        target = config.images_dir / ph.category / ph.subject / ph.filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"file missing for photo {id}: {ph.filename}")

        def _render() -> bytes:
            with Image.open(target) as img:
                img = img.convert("RGB")
                img.thumbnail((size, size))
                buf = io.BytesIO()
                img.save(buf, format="WEBP", quality=80)
                return buf.getvalue()

        try:
            data = await asyncio.to_thread(_render)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"cannot render thumb: {exc}") from exc
        return Response(content=data, media_type="image/webp")

    return app
