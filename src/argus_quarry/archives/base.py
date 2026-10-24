"""Search + slice over a local metadata archive (links/captions/sizes, no photos).

An :class:`Archive` reads a dataset's metadata where the operator archived it
(``QUARRY_<NAME>_DIR``, laid out by ``scripts/fetch_public_domain_metadata.py``)
and builds one DuckDB index under ``$QUARRY_HOME/cache/archives/`` with a common
``items`` table, so search never rescans multi-GB parquet/CSV over a NAS share:

    id          stable per-item id (PD12M uuid, Flickr photo id)
    url         full-size image URL ingest downloads
    thumb_url   a small rendition the browser can load directly, or NULL
    source_url  the provenance page (or the URL itself when there is none)
    caption     recaption text (searchable)
    width/height  pixels, NULL when the archive does not know them
    licence     the raw licence label ingest's licence-gate judges
    collection  the facet: PD12M institution, Flickr owner account
    mime        image MIME type, NULL when unknown
    rnd         a fixed random key, so "shuffle" pages are stable and diverse

Searching is read-only. A *slice* (:class:`SliceSpec`) is a saved query plus the
category/subject it lands under; :meth:`Archive.records` turns it into the same
:class:`~argus_quarry.models.SourceRecord` stream every downloader yields, so
licence-gate, SHA256 dedup and provenance stay in :mod:`argus_quarry.ingest`.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator

from argus_quarry.config import QuarryConfig
from argus_quarry.models import DEFAULT_CATEGORY, SourceRecord, normalise_category, slugify_name

logger = structlog.get_logger()

PAGE_MAX = 200
SLICE_MAX = 5000
#: DuckDB memory ceiling for an index build; joins and sorts spill to disk past it.
#: Megalith's build peaked over 14 GB unbounded, which got it OOM-killed beside other tenants.
DEFAULT_BUILD_MEMORY = "4GB"
ITEM_FIELDS = ("id", "url", "thumb_url", "source_url", "caption", "width", "height", "licence", "collection", "mime")


class ArchiveUnavailable(RuntimeError):
    """The archive dir is unset/missing, or its index has not been built yet."""


class ArchiveQuery(BaseModel):
    """Filters shared by every archive; fields an archive lacks simply match nothing extra."""

    q: str | None = Field(None, description="caption words, all must match (case-insensitive)")
    collections: list[str] = Field(default_factory=list, description="PD12M institutions / Flickr owners")
    licences: list[str] = Field(default_factory=list, description="raw licence labels, e.g. CC0, PDM")
    mimes: list[str] = Field(default_factory=list, description="e.g. image/jpeg")
    min_width: int | None = Field(None, ge=1)
    min_height: int | None = Field(None, ge=1)
    min_short_side: int | None = Field(None, ge=1)
    orientation: Literal["landscape", "portrait", "square"] | None = None
    sized_only: bool = Field(False, description="only items whose dimensions are known")
    ids: list[str] = Field(default_factory=list, description="explicit selection; other filters still apply")
    sort: Literal["shuffle", "largest"] = "shuffle"


class SliceSpec(BaseModel):
    """A query plus where its images land — the unit the UI saves and ``archives fetch`` runs."""

    archive: str
    query: ArchiveQuery = Field(default_factory=ArchiveQuery)
    category: str = DEFAULT_CATEGORY
    subject: str
    limit: int = Field(100, ge=1, le=SLICE_MAX)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return normalise_category(v)

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        return slugify_name(v)


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise ArchiveUnavailable("archives require: pip install argus-quarry[archives]") from exc
    return duckdb


class Archive(ABC):
    """One searchable metadata archive."""

    name: str = "base"
    title: str = ""
    description: str = ""
    #: Value stored as ``SourceRecord.source`` (and in the pool filename).
    source: str = "base"

    def __init__(self, config: QuarryConfig) -> None:
        self.config = config

    # ── locations ─────────────────────────────────────────────────────
    @property
    def root(self) -> Path | None:
        return self.config.archive_dirs.get(self.name)

    @property
    def index_path(self) -> Path:
        return self.config.archive_index_dir / f"{self.name}.duckdb"

    def status(self) -> dict[str, Any]:
        root = self.root
        info: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "configured": root is not None,
            "present": bool(root and root.is_dir()),
            "indexed": self.index_path.is_file(),
            "items": None,
        }
        if info["indexed"]:
            try:
                with self._connect() as con:
                    info["items"] = con.execute("SELECT count(*) FROM items").fetchone()[0]
            except Exception:  # a half-written or foreign file is "not indexed"
                info["indexed"] = False
        return info

    # ── index build (the only write; CLI-driven) ──────────────────────
    @abstractmethod
    def _select_sql(self, root: Path) -> str:
        """A SELECT producing the common columns (minus ``rnd``) from the raw archive files."""

    def _stage_sql(self, root: Path) -> list[str]:
        """Statements run before :meth:`_select_sql`, typically ``CREATE TEMP TABLE`` staging
        that the final SELECT joins. Temp tables spill to disk under the memory limit."""
        return []

    def build_index(self, *, threads: int | None = None, memory_limit: str = DEFAULT_BUILD_MEMORY) -> int:
        """(Re)build the DuckDB index atomically: write a temp file, then rename over the old one."""
        root = self.root
        if root is None or not root.is_dir():
            raise ArchiveUnavailable(f"{self.name}: set QUARRY_{self.name.upper()}_DIR to the archived metadata")
        duckdb = _duckdb()
        self.config.archive_index_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".duckdb.tmp")
        tmp.unlink(missing_ok=True)
        spill = self.config.archive_index_dir / f".{self.name}.spill"
        con = duckdb.connect(str(tmp))
        built = False
        try:
            if threads:
                con.execute(f"SET threads = {int(threads)}")
            con.execute("SET memory_limit = ?", [memory_limit])
            con.execute("SET temp_directory = ?", [str(spill)])
            con.execute("SET preserve_insertion_order = false")  # lets big CREATE TABLE AS stream
            for statement in self._stage_sql(root):
                con.execute(statement)
            con.execute(
                "CREATE TABLE items AS SELECT *, hash(id || 'argus') AS rnd FROM (" + self._select_sql(root) + ")"
            )
            con.execute(
                "CREATE TABLE facets AS SELECT collection, count(*) AS n FROM items "
                "WHERE collection IS NOT NULL GROUP BY collection ORDER BY n DESC"
            )
            n, distinct = con.execute("SELECT count(*), count(DISTINCT id) FROM items").fetchone()
            if distinct != n:
                logger.warning("archive_duplicate_ids", archive=self.name, rows=n, distinct=distinct)
            built = True
        except duckdb.Error as exc:
            raise ArchiveUnavailable(f"{self.name}: index build failed: {exc}") from exc
        finally:
            con.close()
            shutil.rmtree(spill, ignore_errors=True)
            if not built:  # never leave a half-built index behind
                tmp.unlink(missing_ok=True)
        os.replace(tmp, self.index_path)
        return n

    # ── read path ─────────────────────────────────────────────────────
    def _connect(self):
        if not self.index_path.is_file():
            raise ArchiveUnavailable(f"{self.name}: no index yet — run `argus-quarry archives index {self.name}`")
        return _duckdb().connect(str(self.index_path), read_only=True)

    @staticmethod
    def _where(query: ArchiveQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for word in (query.q or "").split():
            clauses.append("caption ILIKE ?")
            params.append(f"%{word}%")

        def _in(column: str, values: list[str]) -> None:
            if values:
                clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
                params.extend(values)

        _in("collection", query.collections)
        _in("licence", query.licences)
        _in("mime", query.mimes)
        _in("id", query.ids)
        if query.min_width:
            clauses.append("width >= ?")
            params.append(query.min_width)
        if query.min_height:
            clauses.append("height >= ?")
            params.append(query.min_height)
        if query.min_short_side:
            clauses.append("least(width, height) >= ?")
            params.append(query.min_short_side)
        if query.sized_only:
            clauses.append("width IS NOT NULL AND height IS NOT NULL")
        if query.orientation == "landscape":
            clauses.append("width > height * 1.1")
        elif query.orientation == "portrait":
            clauses.append("height > width * 1.1")
        elif query.orientation == "square":
            clauses.append("width BETWEEN height * 0.9 AND height * 1.1")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _order(query: ArchiveQuery) -> str:
        return " ORDER BY width * height DESC NULLS LAST, rnd" if query.sort == "largest" else " ORDER BY rnd"

    def search(self, query: ArchiveQuery, *, limit: int = 48, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, PAGE_MAX))
        where, params = self._where(query)
        cols = ", ".join(ITEM_FIELDS)
        with self._connect() as con:
            total = con.execute("SELECT count(*) FROM items" + where, params).fetchone()[0]
            rows = con.execute(
                f"SELECT {cols} FROM items{where}{self._order(query)} LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [dict(zip(ITEM_FIELDS, r, strict=True)) for r in rows],
        }

    def facets(self, *, top: int = 200) -> dict[str, Any]:
        with self._connect() as con:
            collections = con.execute("SELECT collection, n FROM facets LIMIT ?", [top]).fetchall()
            n_collections = con.execute("SELECT count(*) FROM facets").fetchone()[0]
            licences = con.execute("SELECT licence, count(*) FROM items GROUP BY 1 ORDER BY 2 DESC").fetchall()
            mimes = con.execute(
                "SELECT mime, count(*) FROM items WHERE mime IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            sized = con.execute("SELECT count(*) FROM items WHERE width IS NOT NULL").fetchone()[0]
            total = con.execute("SELECT count(*) FROM items").fetchone()[0]
        return {
            "total": total,
            "sized": sized,
            "n_collections": n_collections,
            "collections": [{"name": c, "count": n} for c, n in collections],
            "licences": [{"name": c, "count": n} for c, n in licences],
            "mimes": [{"name": c, "count": n} for c, n in mimes],
        }

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(f"SELECT {', '.join(ITEM_FIELDS)} FROM items WHERE id = ?", [item_id]).fetchone()
        return dict(zip(ITEM_FIELDS, row, strict=True)) if row else None

    def count(self, query: ArchiveQuery) -> int:
        where, params = self._where(query)
        with self._connect() as con:
            return con.execute("SELECT count(*) FROM items" + where, params).fetchone()[0]

    # ── slice -> SourceRecord stream ──────────────────────────────────
    def records(self, spec: SliceSpec) -> Iterator[SourceRecord]:
        """The slice's items, in the query's order, as ingest-ready records."""
        page = self.search(spec.query, limit=min(spec.limit, PAGE_MAX), offset=0)
        yielded = 0
        offset = 0
        while page["items"] and yielded < spec.limit:
            for item in page["items"]:
                if yielded >= spec.limit:
                    return
                yield self.to_record(item, category=spec.category, subject=spec.subject)
                yielded += 1
            offset += len(page["items"])
            page = self.search(spec.query, limit=min(spec.limit - yielded, PAGE_MAX) or 1, offset=offset)

    @abstractmethod
    def to_record(self, item: dict[str, Any], *, category: str, subject: str) -> SourceRecord:
        """Map one index row to a :class:`SourceRecord` with full provenance."""


def sql_path(path: Path) -> str:
    """A filesystem path (or glob) as a single-quoted DuckDB string literal."""
    return "'" + str(path).replace("'", "''") + "'"
