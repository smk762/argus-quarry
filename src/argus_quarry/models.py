"""Pydantic models — the acquisition stage's data contract.

The :class:`SourceRecord` is the source-independent object every downloader
yields. Because each archive maps its own API onto the *same* record, the rest
of the pipeline (ingest, store, export) never learns which source a file came
from. Provenance and licence travel with every record — that is the whole point
of the tool (``provenance-first``): a record with no accepted licence never
lands (see :func:`is_accepted_licence`).

Subjects are grouped into **categories** that mirror the suite's LoRA-training
taxonomy (identity / wardrobe / setting / concept). A category is independent of
identity: a subject can be a person, a garment, a scene, or a visual concept.
On disk everything is sorted into ``<category>/<subject>/`` subfolders.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Ingest lifecycle for a photograph row. ``pending``/``downloading`` support
# resumability; ``duplicate``/``quarantined`` record *why* a candidate did not
# land so reruns skip it cheaply instead of re-fetching.
PhotoStatus = ("pending", "downloading", "complete", "failed", "duplicate", "quarantined")

# Subject categories, aligned with the suite's LoRA-training taxonomy
# (argus-curator ``TargetCategory``). ``identity`` is people/portraits; the rest
# are identity-independent training workflows. The tuple is the canonical set,
# but categories are not hard-validated so downstream can extend them.
SUBJECT_CATEGORIES = ("identity", "wardrobe", "setting", "concept")
DEFAULT_CATEGORY = "identity"

# Licence strings we accept as free-to-use for this archive. We normalise the
# messy per-source labels (Commons ``extmetadata``, museum rights URIs, …) and
# match against these tokens. Anything else is quarantined, never landed.
_ACCEPTED_TOKENS = (
    "cc0",
    "pd",
    "public domain",
    "publicdomain",
    "pdm",
    "pd-us",
    "pd-art",
    "no known copyright",
    "no copyright",
)


def normalise_licence(raw: str | None) -> str | None:
    """Collapse a raw source licence label to a canonical token, or ``None``.

    Returns ``"CC0"`` or ``"PD"`` for anything we recognise as free-to-use, else
    ``None``. Kept deliberately conservative: unknown labels are *not* coerced to
    PD — they simply fail acceptance and get quarantined.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if "cc0" in text or "creative commons zero" in text:
        return "CC0"
    for token in _ACCEPTED_TOKENS:
        if token in text:
            return "PD"
    return None


def is_accepted_licence(raw: str | None) -> bool:
    """True if ``raw`` normalises to an accepted free-to-use licence."""
    return normalise_licence(raw) is not None


def normalise_category(raw: str | None) -> str:
    """Canonical category token (lower-cased); empty/None falls back to identity."""
    if not raw or not raw.strip():
        return DEFAULT_CATEGORY
    return raw.strip().lower()


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slugify_name(name: str) -> str:
    """Canonical folder-safe subject name, e.g. ``"Red dress"`` -> ``"Red_dress"``."""
    cleaned = _SLUG_RE.sub("_", name.strip()).strip("_")
    return cleaned or "unknown"


class Subject(BaseModel):
    """A thing to harvest images of, within a :data:`category <SUBJECT_CATEGORIES>`.

    Decoupled from the downloaders: the same shape is produced by every curated
    ``seeds/<category>.yaml`` loader (and, for identity, the Phase 2 Wikidata
    harvester), so downloaders never care which source supplied the list. The
    identity-only fields (``wikidata_id`` / ``birth_year`` / ``death_year`` /
    ``occupation``) stay ``None`` for wardrobe / setting / concept subjects.
    """

    name: str
    category: str = DEFAULT_CATEGORY
    # Explicit archive search string; defaults to ``name`` when unset.
    search: str | None = None
    aliases: list[str] = Field(default_factory=list)

    # identity-only metadata
    wikidata_id: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    occupation: str | None = None

    @field_validator("category")
    @classmethod
    def _normalise_category(cls, v: str) -> str:
        return normalise_category(v)

    @property
    def folder(self) -> str:
        """Canonical folder / DB name for this subject (e.g. ``Red_dress``)."""
        return slugify_name(self.name)

    @property
    def query(self) -> str:
        """The string a downloader searches the archive with."""
        return self.search or self.name


class SourceRecord(BaseModel):
    """The common contract every downloader yields (DESIGN.md section 4).

    ``ingest`` turns each record into bytes on disk plus a DB row, idempotently.
    The full-resolution ``remote_url`` is always retained so a capped/downscaled
    image can be re-fetched at original size later without losing provenance.
    """

    # subject / category
    subject: str  # canonical folder name, e.g. "Red_dress" or "Albert_Einstein"
    category: str = DEFAULT_CATEGORY

    # identity-only metadata (None for non-person subjects)
    wikidata_id: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    occupation: str | None = None

    # the asset
    title: str | None = None
    photographer: str | None = None
    year: int | None = None
    remote_url: str

    # provenance / licence (never optional in spirit — this is the point)
    source: str
    source_url: str
    licence: str
    attribution: str | None = None

    @field_validator("category")
    @classmethod
    def _normalise_category(cls, v: str) -> str:
        return normalise_category(v)


class Photograph(BaseModel):
    """A ``photographs`` row — the persisted result of ingesting a record.

    Mirrors the SQLite schema in :mod:`argus_quarry.store`. ``sha256`` is the
    exact-dedup key (``UNIQUE`` in the DB); ``phash`` is informational only and
    never drives dedup here (that is argus-curator's job).
    """

    id: int | None = None
    subject_id: int
    subject: str
    category: str = DEFAULT_CATEGORY

    title: str | None = None
    photographer: str | None = None
    year: int | None = None

    source: str
    source_url: str
    licence: str
    attribution: str | None = None

    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    filename: str | None = None
    sha256: str | None = None
    phash: str | None = None

    remote_url: str
    status: str = "pending"
    downloaded_at: str | None = None


# ── Backward-compatible aliases (the pre-0.2 portrait-only API) ─────────
# quarry began life person/portrait-only; keep the old names importable.
Person = Subject
PortraitRecord = SourceRecord
