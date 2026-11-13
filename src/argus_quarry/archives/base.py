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
    pos         the item's row in the archive's published embeddings, NULL when it has none

Searching is read-only. A *slice* (:class:`SliceSpec`) is a saved query plus the
category/subject it lands under; :meth:`Archive.records` turns it into the same
:class:`~argus_quarry.models.SourceRecord` stream every downloader yields, so
licence-gate, SHA256 dedup and provenance stay in :mod:`argus_quarry.ingest`.

An archive that publishes image embeddings (PD12M) can also be searched by meaning
(``semantic``) or by example (``similar_to``); see :mod:`argus_quarry.archives.semantic`.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus_quarry.archives import semantic as sem
from argus_quarry.config import QuarryConfig
from argus_quarry.models import DEFAULT_CATEGORY, SourceRecord, normalise_category, slugify_name

logger = structlog.get_logger()

PAGE_MAX = 200
SLICE_MAX = 5000
#: DuckDB memory ceiling for an index build; joins and sorts spill to disk past it.
#: Megalith's build peaked over 14 GB unbounded, which got it OOM-killed beside other tenants.
DEFAULT_BUILD_MEMORY = "4GB"
#: Upper bound for integer filters: DuckDB binds them as INTEGER, so larger values would error.
INT_MAX = 2**31 - 1
ITEM_FIELDS = (
    "id", "url", "thumb_url", "source_url", "caption", "width", "height", "licence", "collection", "mime",
    "tone", "medium", "year",
)  # fmt: skip

#: Bumped whenever the ``items`` columns, or how they are derived, change; an older index asks to be
#: rebuilt. 3: "The photo shows …" openers count as medium photo. 4: ``pos`` (embedding row).
INDEX_SCHEMA = 4
#: A semantic query ranks at most this many items (the nearest neighbours that pass the filters).
SEMANTIC_MAX = 5000

# ── caption-derived columns ─────────────────────────────────────────────
# Neither dataset records tone, medium or date per image, so they are read off
# the recaption at index time. They are cues, not ground truth: NULL means the
# caption doesn't say. Measured on the real archives (2026-10):
#   tone   PD12M 29% bw / 35% color; Megalith 7% bw / 77% color
#   medium named in the caption's opening words: PD12M ~45%; Megalith is photos by construction
#   year   PD12M 0.5%, Megalith 1.3% of captions mention one
_BW = "black and white|black-and-white|monochrom|grayscale|greyscale|sepia"
_COLOUR = r"\b(red|blue|green|yellow|orange|purple|pink|brown|colou?rful|vibrant)\b"
#: First match wins, so a "photo of a painting" is a painting (a reproduction).
MEDIUM_RULES: tuple[tuple[str, str], ...] = (
    ("painting", r"painting|watercolou?r|oil on canvas|fresco"),
    ("drawing", r"drawing|sketch|illustration|cartoon"),
    ("print", r"engraving|etching|lithograph|woodcut|\bprint\b"),
    ("map", r"\bmap\b"),
    ("text", r"\b(page|letter|document|text|book|manuscript|newspaper)\b"),
    ("object", r"sculpture|statue|\bvase\b|\bcoin\b|\bobject\b"),
    ("photo", r"photo"),
)
MEDIUMS = tuple(m for m, _ in MEDIUM_RULES)
#: The first year (1500-2029) or decade ("1920s") a caption mentions.
_YEAR = r"\b(1[5-9][0-9]{2}|20[0-2][0-9])s?\b"


#: Where the recaptions name the medium: "The image shows a black and white photo of …",
#: "The image is a painting of …". Only the first few words after that opener are
#: read, so "a porcelain brush pot with a painting of a man" is not a painting.
_OPENER = (
    r"^(the|this) (image|picture|photo|photograph) (shows|is|depicts|features|contains|appears to be)( an?| the)? "
)
_HEAD_WORDS = 6
#: The opener itself can name the medium ("The photo shows …"); checked after the head rules.
_PHOTO_OPENER = r"^(the|this) (photo|photograph) "


def _derived_sql(default_medium: str | None) -> str:
    # The head: the caption minus its opener, cut to its first _HEAD_WORDS words.
    head = (
        f"array_to_string(list_slice(string_split(regexp_replace(lower(caption), '{_OPENER}', ''), ' '), "
        f"1, {_HEAD_WORDS}), ' ')"
    )
    mediums = " ".join(f"WHEN regexp_matches({head}, '{rx}') THEN '{m}'" for m, rx in MEDIUM_RULES)
    default = f"'{default_medium}'" if default_medium else "NULL"
    return (
        f"CASE WHEN regexp_matches(lower(caption), '{_BW}') THEN 'bw' "
        f"WHEN regexp_matches(lower(caption), '{_COLOUR}') THEN 'color' END AS tone, "
        f"CASE {mediums} WHEN regexp_matches(lower(caption), '{_PHOTO_OPENER}') THEN 'photo' "
        f"ELSE {default} END AS medium, "
        f"TRY_CAST(nullif(regexp_extract(caption, '{_YEAR}', 1), '') AS INTEGER) AS year"
    )


class ArchiveUnavailable(RuntimeError):
    """The archive dir is unset/missing, or its index has not been built yet."""


class IndexStale(ArchiveUnavailable):
    """The index exists but was built for a different schema: it needs a rebuild."""


class ItemNotFound(LookupError):
    """A query named an item (``similar_to``) the archive does not have."""


class ArchiveQuery(BaseModel):
    """Filters shared by every archive; fields an archive lacks simply match nothing extra."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(
        None,
        description='caption terms, all must match (case-insensitive); "quoted phrases" stay whole and '
        "a leading - excludes a term",
    )
    exclude: str | None = Field(None, description="caption terms none of which may appear (same syntax as q)")
    collections: list[str] = Field(default_factory=list, description="PD12M institutions / Flickr owners")
    licences: list[str] = Field(default_factory=list, description="raw licence labels, e.g. CC0, PDM")
    mimes: list[str] = Field(default_factory=list, description="e.g. image/jpeg")
    min_width: int | None = Field(None, ge=1, le=INT_MAX)
    min_height: int | None = Field(None, ge=1, le=INT_MAX)
    min_short_side: int | None = Field(None, ge=1, le=INT_MAX)
    orientation: Literal["landscape", "portrait", "square"] | None = None
    sized_only: bool = Field(False, description="only items whose dimensions are known")
    tone: Literal["bw", "color"] | None = Field(None, description="caption-derived: black & white or colour")
    mediums: list[str] = Field(default_factory=list, description=f"caption-derived, any of {', '.join(MEDIUMS)}")
    year_min: int | None = Field(
        None, ge=-INT_MAX, le=INT_MAX, description="caption-mentioned year >= (items without one are excluded)"
    )
    year_max: int | None = Field(None, ge=-INT_MAX, le=INT_MAX, description="caption-mentioned year <=")
    ids: list[str] = Field(default_factory=list, description="explicit selection; other filters still apply")
    sort: Literal["shuffle", "largest"] = Field("shuffle", description="ignored by semantic/similar_to queries")
    semantic: str | None = Field(
        None,
        max_length=1000,
        description="rank by meaning: CLIP text query, e.g. 'a woman in a wool coat, outdoors' "
        "(archives with embeddings only)",
    )
    similar_to: str | None = Field(None, description="rank by likeness to this item id (more like this)")

    @model_validator(mode="after")
    def _one_ranking(self) -> ArchiveQuery:
        if self.semantic is not None and not self.semantic.strip():
            self.semantic = None
        if self.similar_to is not None and not self.similar_to.strip():
            self.similar_to = None
        if self.semantic and self.similar_to:
            raise ValueError("semantic and similar_to are exclusive: rank by text or by example, not both")
        return self

    @property
    def ranked(self) -> bool:
        """True when results are ordered by embedding similarity rather than ``sort``."""
        return bool(self.semantic or self.similar_to)

    @field_validator("mediums")
    @classmethod
    def _mediums(cls, v: list[str]) -> list[str]:
        v = [m.strip().lower() for m in v]
        unknown = sorted(set(v) - set(MEDIUMS))
        if unknown:
            raise ValueError(f"unknown medium(s) {', '.join(unknown)}; expected any of {', '.join(MEDIUMS)}")
        return v


#: A ``-``-prefixed or plain ``"quoted phrase"``, else a bare whitespace-separated word.
_TERM_RE = re.compile(r'(-?)"([^"]*)"|(\S+)')


def caption_terms(text: str | None) -> tuple[list[str], list[str]]:
    """Split a caption query into ``(required, excluded)`` terms.

    Whitespace separates terms, ``"double quotes"`` keep a phrase whole, and a
    leading ``-`` (``-painting``, ``-"black and white"``) marks a term excluded.
    Only double quotes group, so apostrophes (``children's``) stay literal; an
    unbalanced ``"`` is kept as part of a plain word rather than failing.
    """
    if not text or not text.strip():
        return [], []
    required: list[str] = []
    excluded: list[str] = []
    for m in _TERM_RE.finditer(text):
        negated, phrase, word = m.groups()
        if word is None:
            token = phrase.strip()
        elif word.startswith("-") and len(word) > 1:
            negated, token = "-", word[1:]
        else:
            token = "" if word == "-" else word
        if token:
            (excluded if negated else required).append(token)
    return required, excluded


class SliceSpec(BaseModel):
    """A query plus where its images land — the unit the UI saves and ``archives fetch`` runs."""

    model_config = ConfigDict(extra="forbid")

    archive: str
    query: ArchiveQuery = Field(default_factory=ArchiveQuery)
    category: str = DEFAULT_CATEGORY
    subject: str
    limit: int = Field(100, ge=1, le=SLICE_MAX)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        # The category becomes a directory under images/: never let it climb out.
        v = normalise_category(v)
        if not _CATEGORY_RE.fullmatch(v):
            raise ValueError(f"category {v!r} must be letters, digits, '_' or '-'")
        return v

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        return slugify_name(v)


_CATEGORY_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def split_collections(values: list[str] | str | None) -> list[str]:
    """Collection names from repeated params and/or one comma-joined value.

    Only a comma *not* followed by whitespace separates: institution names such as
    "Natural History Museum, Berlin" (55 of PD12M's 640) always put a space after
    theirs, while joined lists ("Rijksmuseum,iNaturalist") do not.
    """
    if values is None:
        return []
    out: list[str] = []
    for value in [values] if isinstance(values, str) else values:
        out += [v.strip() for v in re.split(r",(?!\s)", value) if v.strip()]
    return out


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
    #: Medium assumed when the caption names none (Megalith is photos by construction).
    default_medium: str | None = None
    #: The model that made the archive's published image embeddings (and so the text
    #: encoder for ``semantic`` queries); None when the archive publishes none.
    embedding_model: str | None = None

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
            "stale": False,
            "items": None,
            "semantic": None,
        }
        if self.embedding_model:
            meta = sem.read_pointer(self.config.archive_index_dir, self.name)
            info["semantic"] = {
                "indexed": meta is not None,
                "vectors": meta["rows"] if meta else None,
                "model": self.embedding_model,
                "built_at": meta.get("built_at") if meta else None,
            }
        if info["indexed"]:
            try:
                with self._connect() as con:
                    info["items"] = con.execute("SELECT count(*) FROM items").fetchone()[0]
            except IndexStale:  # built for another schema: present, but needs a rebuild
                info["indexed"] = False
                info["stale"] = True
            except Exception:  # duckdb missing, or a half-written/foreign file: "not indexed"
                info["indexed"] = False
        return info

    # ── index build (the only write; CLI-driven) ──────────────────────
    @abstractmethod
    def _select_sql(self, root: Path) -> str:
        """A SELECT producing the common columns (minus ``rnd``) from the raw archive files."""

    def _embedding_files(self, root: Path) -> list[Path]:
        """The published embedding matrices, in row order (concatenated, row ``i`` is ``items.pos = i``)."""
        return []

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
        # Per-build names: two overlapping builds must not share (and delete) each other's
        # temp file, and "<name>.duckdb.tmp" is DuckDB's default spill dir for readers.
        token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = self.config.archive_index_dir / f".{self.name}.{token}.building.duckdb"
        spill = self.config.archive_index_dir / f".{self.name}.{token}.spill"
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
                f"CREATE TABLE items AS SELECT *, hash(id || 'argus') AS rnd, {_derived_sql(self.default_medium)} "
                f"FROM ({self._select_sql(root)})"
            )
            if self.embedding_model:
                # Ranked queries join their hits back by pos: without an index that is a scan
                # of every row (~1.5 s on PD12M), with it ~5 ms. Archives with no embeddings
                # have an all-NULL pos and never rank, so they skip it.
                con.execute("CREATE INDEX items_pos ON items (pos)")
            con.execute("CREATE TABLE meta AS SELECT ? AS schema_version, now() AS built_at", [INDEX_SCHEMA])
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
                tmp.with_name(tmp.name + ".wal").unlink(missing_ok=True)
        os.replace(tmp, self.index_path)
        return n

    def build_embeddings(
        self,
        *,
        threads: int | None = None,
        factory: str | None = None,
        chunk_rows: int = sem.DEFAULT_CHUNK_ROWS,
        train_rows: int | None = None,
        progress=None,  # noqa: ANN001
    ) -> dict[str, Any]:
        """(Re)build the vector index from the archive's published embeddings.

        Needs a current metadata index: its ``pos`` column is what a hit maps back to,
        so the embeddings must have exactly one row per item.
        """
        if not self.embedding_model:
            raise ArchiveUnavailable(f"{self.name}: this archive publishes no embeddings")
        root = self.root
        if root is None or not root.is_dir():
            raise ArchiveUnavailable(f"{self.name}: set QUARRY_{self.name.upper()}_DIR to the archived metadata")
        files = self._embedding_files(root)
        if not files:
            raise ArchiveUnavailable(f"{self.name}: no embeddings under {root} (archive them with --embeddings)")
        with self._connect() as con:
            n, n_pos, max_pos = con.execute("SELECT count(*), count(pos), max(pos) FROM items").fetchone()
        if n_pos != n or max_pos != n - 1:
            raise ArchiveUnavailable(f"{self.name}: index rows do not map 1:1 onto embedding rows — rebuild the index")
        try:
            return sem.build_vector_index(
                files,
                self.config.archive_index_dir,
                self.name,
                expected_rows=n,
                model=self.embedding_model,
                factory=factory,
                chunk_rows=chunk_rows,
                train_rows=train_rows,
                threads=threads,
                progress=progress,
            )
        except sem.SemanticUnavailable as exc:
            raise ArchiveUnavailable(str(exc)) from exc

    # ── read path ─────────────────────────────────────────────────────
    def _connect(self):
        if not self.index_path.is_file():
            raise ArchiveUnavailable(f"{self.name}: no index yet — run `argus-quarry archives index {self.name}`")
        duckdb = _duckdb()
        rebuild = f"rebuild with `argus-quarry archives index {self.name}`"
        try:
            con = duckdb.connect(str(self.index_path), read_only=True)
        except duckdb.Error as exc:
            raise ArchiveUnavailable(f"{self.name}: index is unreadable ({exc}) — {rebuild}") from exc
        try:
            try:
                version = con.execute("SELECT schema_version FROM meta").fetchone()[0]
            except duckdb.CatalogException:  # schema 1 had no meta table
                version = 1
        except BaseException as exc:
            con.close()
            if isinstance(exc, duckdb.Error):
                raise ArchiveUnavailable(f"{self.name}: index is unreadable ({exc}) — {rebuild}") from exc
            raise
        if version != INDEX_SCHEMA:
            con.close()
            age = "a newer" if isinstance(version, int) and version > INDEX_SCHEMA else "an older"
            raise IndexStale(
                f"{self.name}: index was built by {age} argus-quarry (schema {version}, need {INDEX_SCHEMA}) — "
                f"{rebuild}"
            )
        return con

    @staticmethod
    def _where(query: ArchiveQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        required, excluded = caption_terms(query.q)
        also_required, also_excluded = caption_terms(query.exclude)
        # Every term of `exclude` is a negative, with or without a leading "-".
        excluded += also_required + also_excluded
        for term in required:
            clauses.append("caption ILIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(term)}%")
        for term in excluded:
            # An uncaptioned item does not mention the term, so it is kept: excluding
            # narrows what captions say, it does not also drop the unknown.
            clauses.append("(caption IS NULL OR caption NOT ILIKE ? ESCAPE '\\')")
            params.append(f"%{_escape_like(term)}%")

        def _in(column: str, values: list[str]) -> None:
            if values:
                clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
                params.extend(values)

        _in("collection", query.collections)
        _in("licence", query.licences)
        _in("mime", query.mimes)
        _in("id", query.ids)
        _in("medium", query.mediums)
        if query.tone:
            clauses.append("tone = ?")
            params.append(query.tone)
        if query.year_min is not None:
            clauses.append("year >= ?")
            params.append(query.year_min)
        if query.year_max is not None:
            clauses.append("year <= ?")
            params.append(query.year_max)
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
        # BIGINT: two INTEGER sides multiply as INTEGER and overflow past ~46k x 46k.
        # id breaks rnd ties so paging and slices are deterministic.
        if query.sort == "largest":
            return " ORDER BY width::BIGINT * height DESC NULLS LAST, rnd, id"
        return " ORDER BY rnd, id"

    # ── semantic ranking ──────────────────────────────────────────────
    def _vector_index(self) -> sem.VectorIndex:
        if not self.embedding_model:
            raise ArchiveUnavailable(f"{self.name}: no embeddings, so no semantic or similar_to search")
        try:
            index = sem.load_vector_index(self.config.archive_index_dir, self.name)
        except sem.SemanticUnavailable as exc:
            raise ArchiveUnavailable(str(exc)) from exc
        except Exception as exc:  # a corrupt / foreign file: same as not built
            raise ArchiveUnavailable(f"{self.name}: embeddings index is unreadable ({exc})") from exc
        if index is None:
            raise ArchiveUnavailable(
                f"{self.name}: no embeddings index yet — run `argus-quarry archives index {self.name} --embeddings`"
            )
        return index

    def _ranked(self, con, query: ArchiveQuery, k: int) -> tuple[list[sem.Hit], int]:  # noqa: ANN001
        """The ``k`` best items for a ranked query, and how many items it ranks (capped at SEMANTIC_MAX).

        ``k == 0`` only counts: no query vector is computed (no text encoding) and no
        bitmap is built, so ``count`` stays cheap for the slice and fetch routes.
        """
        index = self._vector_index()
        # The vector index maps hits by row; after a metadata rebuild over different files
        # its rows no longer line up with items.pos (wrong items, or an out-of-range 500).
        n_items = con.execute("SELECT count(*) FROM items").fetchone()[0]
        if n_items != index.rows:
            raise ArchiveUnavailable(
                f"{self.name}: embeddings index has {index.rows:,} rows but the metadata index {n_items:,} — "
                f"rebuild it with `argus-quarry archives index {self.name} --embeddings-only`"
            )
        exclude = None
        if query.similar_to:
            row = con.execute("SELECT pos FROM items WHERE id = ?", [query.similar_to]).fetchone()
            if row is None:
                raise ItemNotFound(f"{self.name}: no item {query.similar_to!r}")
            if row[0] is None:
                raise ArchiveUnavailable(f"{self.name}: item {query.similar_to!r} has no embedding")
            exclude = int(row[0])
        where, params = self._where(query)
        # The reference item never ranks against itself; filtered rows need a pos to be rankable.
        rankable = f"{where} AND pos IS NOT NULL" if where else " WHERE pos IS NOT NULL"
        if exclude is not None:
            rankable += f" AND pos <> {exclude}"
        k = max(0, min(k, SEMANTIC_MAX))
        if k == 0:
            matches = con.execute(f"SELECT count(*) FROM items{rankable}", params).fetchone()[0]
            return [], min(matches, SEMANTIC_MAX)
        allowed = None
        if where:
            np = sem._np()
            positions = con.execute(f"SELECT pos FROM items{rankable}", params).fetchnumpy()["pos"]
            allowed = np.zeros(index.rows, dtype=bool)
            allowed[np.asarray(positions, dtype=np.int64)] = True
            matches = len(positions)
        else:
            matches = index.rows - (exclude is not None)
        if exclude is not None:
            vector = index.vector(exclude)
        else:
            try:
                vector = sem.get_text_encoder(self.embedding_model).encode(query.semantic)
            except sem.SemanticUnavailable as exc:
                raise ArchiveUnavailable(str(exc)) from exc
        hits = index.search(vector, k, allowed=allowed, exclude=exclude)
        return hits, min(matches, SEMANTIC_MAX)

    @staticmethod
    def _rows_at(con, hits: list[sem.Hit]) -> list[dict[str, Any]]:  # noqa: ANN001
        """Items for ``hits``, in hit order, each with its ``score``."""
        if not hits:
            return []
        rows = con.execute(
            f"SELECT {', '.join(ITEM_FIELDS)}, pos FROM items WHERE pos IN (SELECT unnest(?::BIGINT[]))",
            [[h.pos for h in hits]],
        ).fetchall()
        by_pos = {r[-1]: dict(zip(ITEM_FIELDS, r[:-1], strict=True)) for r in rows}
        return [{**by_pos[h.pos], "score": round(h.score, 4)} for h in hits if h.pos in by_pos]

    def search(self, query: ArchiveQuery, *, limit: int = 48, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, PAGE_MAX))
        if query.ranked:
            with self._connect() as con:
                hits, total = self._ranked(con, query, offset + limit)
                items = self._rows_at(con, hits[offset:])
            return {"total": total, "offset": offset, "limit": limit, "items": items, "ranked": True}
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
            tones = con.execute(
                "SELECT tone, count(*) FROM items WHERE tone IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            mediums = con.execute(
                "SELECT medium, count(*) FROM items WHERE medium IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            dated, year_lo, year_hi = con.execute("SELECT count(year), min(year), max(year) FROM items").fetchone()
            total = con.execute("SELECT count(*) FROM items").fetchone()[0]
        return {
            "total": total,
            "sized": sized,
            "n_collections": n_collections,
            "collections": [{"name": c, "count": n} for c, n in collections],
            "licences": [{"name": c, "count": n} for c, n in licences],
            "mimes": [{"name": c, "count": n} for c, n in mimes],
            "tones": [{"name": c, "count": n} for c, n in tones],
            "mediums": [{"name": c, "count": n} for c, n in mediums],
            "dated": dated,
            "year_range": [year_lo, year_hi] if dated else None,
        }

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(f"SELECT {', '.join(ITEM_FIELDS)} FROM items WHERE id = ?", [item_id]).fetchone()
        return dict(zip(ITEM_FIELDS, row, strict=True)) if row else None

    def count(self, query: ArchiveQuery) -> int:
        with self._connect() as con:
            if query.ranked:
                return self._ranked(con, query, 0)[1]
            where, params = self._where(query)
            return con.execute("SELECT count(*) FROM items" + where, params).fetchone()[0]

    # ── slice -> SourceRecord stream ──────────────────────────────────
    def records(self, spec: SliceSpec) -> Iterator[SourceRecord]:
        """The slice's items, in the query's order, as ingest-ready records.

        One query, read up front (a slice is at most ``SLICE_MAX`` rows), so the index
        connection is not held open while ingest downloads.
        """
        if spec.query.ranked:
            with self._connect() as con:
                hits, _ = self._ranked(con, spec.query, spec.limit)
                items = self._rows_at(con, hits)
            for item in items:
                item.pop("score", None)
                yield self.to_record(item, category=spec.category, subject=spec.subject)
            return
        where, params = self._where(spec.query)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {', '.join(ITEM_FIELDS)} FROM items{where}{self._order(spec.query)} LIMIT ?",
                [*params, spec.limit],
            ).fetchall()
        for row in rows:
            yield self.to_record(dict(zip(ITEM_FIELDS, row, strict=True)), category=spec.category, subject=spec.subject)

    @abstractmethod
    def to_record(self, item: dict[str, Any], *, category: str, subject: str) -> SourceRecord:
        """Map one index row to a :class:`SourceRecord` with full provenance."""


def _escape_like(term: str) -> str:
    """Make ``%`` and ``_`` in a user term literal for ``ILIKE … ESCAPE '\\'``.

    DuckDB has no default LIKE escape character, so the clause must name one.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sql_path(path: Path) -> str:
    """A filesystem path (or glob) as a single-quoted DuckDB string literal."""
    return "'" + str(path).replace("'", "''") + "'"
