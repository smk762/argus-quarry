"""Provenance API for argus-quarry (peer to argus-curator on :8102).

The tiny FastAPI server DESIGN.md section 9 deferred — provenance queries, plus
search over the local public-domain metadata archives. It powers the
argus-studio ``/gallery`` view. Everything is read-only except one opt-in route,
``POST /archives/{name}/fetch``, which lands a slice through the normal ingest
pipeline and is refused (403) unless ``QUARRY_ALLOW_FETCH=1`` — the demo-safe
posture forge, proof and atlas share. A refused fetch still gets the slice JSON
and the CLI command from ``GET /archives/{name}/slice``.

Routes:

    GET /health                          -> liveness (always 200 while up)
    GET /ready                           -> readiness (503 until the pool is serveable)
    GET /stats
    GET /subjects?category=
    GET /photos?category=&subject=&licence=&source=&status=&limit=&offset=
    GET /photos/{photo_id}
    GET /thumb?id=<photo id>&size=<px>   -> image/webp

    GET  /archives                        -> configured archives + fetch_allowed
    GET  /archives/{name}/facets          -> collections / licences / mimes with counts
    GET  /archives/{name}/search?q=&exclude=&collection=&licence=&mime=&min_width=&min_height=
                                 &min_short_side=&orientation=&sized_only=&tone=&medium=&year_min=&year_max=&ids=&sort=&limit=&offset=
    GET  /archives/{name}/items/{id}
    GET  /archives/{name}/thumb?id=&size= -> image/webp (PD12M) or a redirect to Flickr's own small size
    GET  /archives/{name}/slice?<search filters>&category=&subject=&limit=
                                          -> {slice, matches, command, fetch_allowed}
    POST /archives/{name}/fetch  (SliceSpec body) -> NDJSON progress; 403 unless QUARRY_ALLOW_FETCH=1

The store lives under ``QUARRY_HOME`` (env var, or ``create_app(home=...)``).
A short-lived connection is opened per request, so a concurrently running
``argus-quarry fetch`` is always visible (SQLite WAL readers).
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

try:
    from fastapi import Depends, FastAPI, HTTPException, Query
    from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError("Server requires: pip install argus-quarry[server]") from exc

import structlog
from PIL import Image

from argus_quarry import __version__
from argus_quarry.archives import (
    SLICE_MAX,
    ArchiveQuery,
    ArchiveUnavailable,
    SliceSpec,
    available_archives,
    get_archive,
    slice_command,
)
from argus_quarry.config import QuarryConfig
from argus_quarry.models import Photograph
from argus_quarry.store import ProvenanceStore, ProvenanceUnavailable, has_wal_content

logger = structlog.get_logger()

THUMB_DEFAULT = 384  # longest-edge px for /thumb webp output
THUMB_MAX = 1024  # requested sizes are capped here
PAGE_DEFAULT = 100  # /photos page size
PAGE_MAX = 500
ARCHIVE_THUMB_CACHE = 512  # rendered archive thumbs kept in memory (never written to disk)
ARCHIVE_THUMB_MAX_BYTES = 40 * 1024 * 1024  # skip pathological originals instead of decoding them

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
_SERVICE = "argus-quarry"


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
    *,
    allow_fetch: bool | None = None,
    net_factory=None,  # noqa: ANN001 - Callable[[QuarryConfig], NetClient]; tests inject an offline one
    thumb_fetcher=None,  # noqa: ANN001 - Callable[[str], bytes]; None = httpx GET with the polite UA
) -> FastAPI:
    """Create the read-only quarry FastAPI application.

    ``home`` overrides the pool root; by default it is resolved from the
    ``QUARRY_HOME`` env var (falling back to ``./quarry``), exactly like the CLI.
    """
    config = QuarryConfig.from_env(home=Path(home)) if home else QuarryConfig.from_env()
    if allow_fetch is not None:
        config.allow_fetch = allow_fetch

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
            # Read-only surface; POST only exists when the operator opted in to
            # archive slice fetches.
            allow_methods=["GET", "HEAD", "POST"] if config.allow_fetch else ["GET", "HEAD"],
            allow_headers=["*"],
        )

    logger.info("server_ready", quarry_home=str(config.home), db=str(config.db_path))
    _open_store = _make_opener(config.db_path)
    # Resolve the pool path once, at startup. The mount cannot change while the
    # process lives (the same invariant ``_make_opener`` relies on), so liveness
    # never repeats a blocking ``resolve()`` on the event loop — a slow or hung
    # ``QUARRY_HOME`` mount must not be able to stall the liveness probe.
    quarry_home = str(config.home.resolve())

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness: the process is up and answering HTTP. Independent of the pool.

        Deliberately does NOT open the DB (nor touch the pool at request time).
        A container mounted at an empty ``QUARRY_HOME`` that is seeded later
        (argus-halo restores the pool from tape after boot) is *live* from the
        first request; whether it has data to serve yet is :func:`ready`'s job.
        Conflating the two here is what made an empty pool report unhealthy
        forever and blocked the release smoke test (issue #10).
        """
        return {
            "status": "ok",
            "service": _SERVICE,
            "version": __version__,
            "quarry_home": quarry_home,
            "fetch_allowed": config.allow_fetch,
        }

    @app.get("/ready")
    async def ready() -> Response:
        """Readiness: ``200`` only when the provenance DB can actually be served.

        Orchestrators and load balancers should route on this, so a container
        whose pool is missing, unmigrated, or carrying an unreadable WAL is taken
        out of rotation (``503``) instead of answering the data routes with
        errors. Opens the DB exactly as the data routes do, so it 503s for the
        same reasons they would fail. The ``database`` field is a single generic
        reason, not a per-cause code, so it never leaks a filesystem path.
        """

        def _probe() -> str | None:
            try:
                with _open_store():
                    return None
            except HTTPException as exc:
                return str(exc.detail)
            except Exception:  # any open failure means "not ready" — a 503, never a 500
                return _UNAVAILABLE

        detail = await asyncio.to_thread(_probe)
        if detail is not None:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "service": _SERVICE, "database": detail},
            )
        return JSONResponse(content={"status": "ready", "service": _SERVICE, "database": "ok"})

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

    _register_archive_routes(app, config, net_factory=net_factory, thumb_fetcher=thumb_fetcher)
    return app


# ── archives ────────────────────────────────────────────────────────────
_FETCH_REFUSAL = (
    "fetching archive slices is disabled on this host (set QUARRY_ALLOW_FETCH=1); "
    "save the slice from GET /archives/{name}/slice and run the CLI command instead"
)


def _archive_query(
    q: str | None = Query(None, description='caption terms, all must match; "phrases" and -excluded terms'),
    exclude: str | None = Query(None, description="caption terms none of which may appear"),
    collection: str | None = Query(None, description="comma-separated institutions / Flickr owners"),
    licence: str | None = Query(None, description="comma-separated licence labels, e.g. CC0,PDM"),
    mime: str | None = Query(None, description="comma-separated MIME types"),
    min_width: int | None = Query(None, ge=1),
    min_height: int | None = Query(None, ge=1),
    min_short_side: int | None = Query(None, ge=1),
    orientation: Literal["landscape", "portrait", "square"] | None = Query(None),
    sized_only: bool = Query(False, description="only items whose dimensions are known"),
    tone: Literal["bw", "color"] | None = Query(None, description="caption-derived: bw or color"),
    medium: str | None = Query(None, description="comma-separated caption-derived mediums, e.g. photo,painting"),
    year_min: int | None = Query(None, description="caption-mentioned year >="),
    year_max: int | None = Query(None, description="caption-mentioned year <="),
    ids: str | None = Query(None, description="comma-separated explicit selection"),
    sort: Literal["shuffle", "largest"] = Query("shuffle"),
):
    return ArchiveQuery(
        q=q,
        exclude=exclude,
        collections=_split_csv(collection) or [],
        licences=_split_csv(licence) or [],
        mimes=_split_csv(mime) or [],
        min_width=min_width,
        min_height=min_height,
        min_short_side=min_short_side,
        orientation=orientation,
        sized_only=sized_only,
        tone=tone,
        mediums=_split_csv(medium) or [],
        year_min=year_min,
        year_max=year_max,
        ids=_split_csv(ids) or [],
        sort=sort,
    )


def _fetch_original(config: QuarryConfig, url: str) -> bytes:
    """GET an archive original for thumbnailing, refusing pathological sizes mid-stream."""
    import httpx

    headers = {"User-Agent": config.user_agent}
    with (
        httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_bytes(65536):
            buf.write(chunk)
            if buf.tell() > ARCHIVE_THUMB_MAX_BYTES:
                raise ValueError("original too large to thumbnail")
    return buf.getvalue()


def _register_archive_routes(app: FastAPI, config: QuarryConfig, *, net_factory=None, thumb_fetcher=None) -> None:  # noqa: ANN001
    """Search/slice routes over the local metadata archives. duckdb (the ``[archives]``
    extra) is imported lazily, so without it these routes answer 503 rather than vanish."""
    thumbs: OrderedDict[tuple[str, str, int], bytes] = OrderedDict()
    thumbs_lock = threading.Lock()
    fetch_lock = threading.Lock()

    def _archive(name: str):
        if name not in available_archives():
            raise HTTPException(status_code=404, detail=f"unknown archive '{name}'")
        return get_archive(name, config)

    async def _run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ArchiveUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/archives")
    async def archives() -> dict[str, Any]:
        def _list() -> list[dict[str, Any]]:
            return [get_archive(n, config).status() for n in available_archives()]

        return {"fetch_allowed": config.allow_fetch, "archives": await asyncio.to_thread(_list)}

    @app.get("/archives/{name}/facets")
    async def archive_facets(name: str, top: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        return await _run(_archive(name).facets, top=top)

    @app.get("/archives/{name}/search")
    async def archive_search(
        name: str,
        query: ArchiveQuery = Depends(_archive_query),
        limit: int = Query(48, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return await _run(_archive(name).search, query, limit=limit, offset=offset)

    @app.get("/archives/{name}/items/{item_id}")
    async def archive_item(name: str, item_id: str) -> dict[str, Any]:
        item = await _run(_archive(name).get, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no item {item_id!r} in {name}")
        return item

    @app.get("/archives/{name}/thumb")
    async def archive_thumb(
        name: str,
        id: str = Query(..., description="item id"),
        size: int = Query(THUMB_DEFAULT, ge=1, description=f"longest-edge px (capped at {THUMB_MAX})"),
    ) -> Response:
        size = min(size, THUMB_MAX)
        archive = _archive(name)
        item = await _run(archive.get, id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no item {id!r} in {name}")
        # The archive already publishes a small rendition (Flickr's 500px): send the browser there.
        if item.get("thumb_url"):
            return RedirectResponse(item["thumb_url"], status_code=307)

        key = (name, id, size)
        with thumbs_lock:
            if key in thumbs:
                thumbs.move_to_end(key)
                return Response(content=thumbs[key], media_type="image/webp")

        def _render() -> bytes:
            raw = thumb_fetcher(item["url"]) if thumb_fetcher else _fetch_original(config, item["url"])
            with Image.open(io.BytesIO(raw)) as img:
                img.draft("RGB", (size, size))  # JPEG: decode at reduced scale, not full size
                img = img.convert("RGB")
                img.thumbnail((size, size))
                out = io.BytesIO()
                img.save(out, format="WEBP", quality=80)
                return out.getvalue()

        try:
            data = await asyncio.to_thread(_render)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"cannot render thumb: {exc}") from exc
        with thumbs_lock:
            thumbs[key] = data
            while len(thumbs) > ARCHIVE_THUMB_CACHE:
                thumbs.popitem(last=False)
        return Response(content=data, media_type="image/webp")

    @app.get("/archives/{name}/slice")
    async def archive_slice(
        name: str,
        query: ArchiveQuery = Depends(_archive_query),
        category: str = Query("identity"),
        subject: str = Query(..., min_length=1),
        limit: int = Query(100, ge=1, le=SLICE_MAX),
    ) -> dict[str, Any]:
        """The slice as a saveable spec, its match count, and the CLI command that lands it."""
        spec = SliceSpec(archive=name, query=query, category=category, subject=subject, limit=limit)
        matches = await _run(_archive(name).count, spec.query)
        return {
            "slice": spec.model_dump(mode="json"),
            "matches": matches,
            "will_fetch": min(matches, spec.limit),
            "command": slice_command(),
            "fetch_allowed": config.allow_fetch,
        }

    @app.post("/archives/{name}/fetch")
    async def archive_fetch(name: str, spec: SliceSpec) -> Response:
        """Land a slice in the raw pool, streaming one NDJSON frame per record."""
        archive = _archive(name)
        if spec.archive != name:
            raise HTTPException(status_code=400, detail=f"slice is for {spec.archive!r}, not {name!r}")
        if not config.allow_fetch:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": _FETCH_REFUSAL.format(name=name),
                    "slice": spec.model_dump(mode="json"),
                    "command": slice_command(),
                },
            )
        total = await _run(archive.count, spec.query)
        total = min(total, spec.limit)
        if not fetch_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="another archive fetch is already running")

        loop = asyncio.get_running_loop()
        frames: asyncio.Queue[dict | None] = asyncio.Queue()

        def emit(frame: dict | None) -> None:
            loop.call_soon_threadsafe(frames.put_nowait, frame)

        def work() -> None:
            from argus_quarry.ingest import BudgetReached, FetchSummary, IngestEngine
            from argus_quarry.net import NetClient

            summary = FetchSummary()
            try:
                emit({"type": "start", "total": total, "category": spec.category, "subject": spec.subject})
                net = net_factory(config) if net_factory else NetClient(config)
                with closing(net), ProvenanceStore(config.db_path) as store:
                    engine = IngestEngine(config, store, net)
                    for done, record in enumerate(archive.records(spec), start=1):
                        try:
                            outcome = engine.ingest_record(record)
                        except BudgetReached:
                            summary.budget_reached = True
                            break
                        summary.record(outcome)
                        emit(
                            {
                                "type": "item",
                                "done": done,
                                "total": total,
                                "status": outcome.status,
                                "url": record.remote_url,
                                "reason": outcome.reason,
                            }
                        )
                emit({"type": "done", "summary": summary.__dict__})
            except Exception as exc:  # report, never 500 mid-stream
                logger.error("archive_fetch_failed", archive=name, error=str(exc))
                emit({"type": "error", "message": str(exc), "summary": summary.__dict__})
            finally:
                fetch_lock.release()
                emit(None)

        # Started now, not when the body is first iterated: the worker owns
        # fetch_lock, so it must run (and release it) even if the client goes
        # away before reading a byte. A disconnect mid-fetch lets it finish.
        worker = loop.run_in_executor(None, work)

        async def stream():
            try:
                while (frame := await frames.get()) is not None:
                    yield json.dumps(frame) + "\n"
            finally:
                await worker

        return StreamingResponse(stream(), media_type="application/x-ndjson")
