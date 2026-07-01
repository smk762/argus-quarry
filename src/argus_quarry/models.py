"""Pydantic models — the acquisition stage's data contract.

The :class:`PortraitRecord` is the source-independent object every downloader
yields. Because each archive maps its own API onto the *same* record, the rest
of the pipeline (ingest, store, export) never learns which source a file came
from. Provenance and licence travel with every record — that is the whole point
of the tool (``provenance-first``): a record with no accepted licence never
lands (see :func:`is_accepted_licence`).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# Ingest lifecycle for a photograph row. ``pending``/``downloading`` support
# resumability; ``duplicate``/``quarantined`` record *why* a candidate did not
# land so reruns skip it cheaply instead of re-fetching.
PhotoStatus = tuple(("pending", "downloading", "complete", "failed", "duplicate", "quarantined"))

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


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slugify_name(name: str) -> str:
    """Canonical folder-safe person name, e.g. ``"Albert Einstein"`` -> ``"Albert_Einstein"``."""
    cleaned = _SLUG_RE.sub("_", name.strip()).strip("_")
    return cleaned or "unknown"


class Person(BaseModel):
    """A subject to harvest portraits of.

    Decoupled from the downloaders: the same shape is produced by the curated
    ``seeds/people.yaml`` loader and (Phase 2) the Wikidata SPARQL harvester, so
    downloaders never care which source supplied the list.
    """

    name: str
    wikidata_id: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    occupation: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @property
    def folder(self) -> str:
        """Canonical folder / DB name for this person (e.g. ``Albert_Einstein``)."""
        return slugify_name(self.name)


class PortraitRecord(BaseModel):
    """The common contract every downloader yields (DESIGN.md section 4).

    ``ingest`` turns each record into bytes on disk plus a DB row, idempotently.
    The full-resolution ``remote_url`` is always retained so a capped/downscaled
    image can be re-fetched at original size later without losing provenance.
    """

    # identity / subject
    person_name: str
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


class Photograph(BaseModel):
    """A ``photographs`` row — the persisted result of ingesting a record.

    Mirrors the SQLite schema in :mod:`argus_quarry.store`. ``sha256`` is the
    exact-dedup key (``UNIQUE`` in the DB); ``phash`` is informational only and
    never drives dedup here (that is argus-curator's job).
    """

    id: int | None = None
    person_id: int
    person_name: str

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
