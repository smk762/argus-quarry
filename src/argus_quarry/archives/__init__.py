"""Local metadata archives of large public-domain datasets, searchable and sliceable.

``name -> Archive`` registry, mirroring :mod:`argus_quarry.downloaders`. An archive
is configured by ``QUARRY_<NAME>_DIR`` and indexed with ``argus-quarry archives
index``; slices of it land through the shared ingest pipeline.
"""

from __future__ import annotations

from argus_quarry.archives.base import (
    PAGE_MAX,
    SLICE_MAX,
    Archive,
    ArchiveQuery,
    ArchiveUnavailable,
    SliceSpec,
)
from argus_quarry.archives.megalith import MegalithArchive
from argus_quarry.archives.pd12m import PD12MArchive
from argus_quarry.config import QuarryConfig

_REGISTRY: dict[str, type[Archive]] = {
    PD12MArchive.name: PD12MArchive,
    MegalithArchive.name: MegalithArchive,
}


def available_archives() -> list[str]:
    return sorted(_REGISTRY)


def get_archive(name: str, config: QuarryConfig) -> Archive:
    try:
        return _REGISTRY[name](config)
    except KeyError:
        raise KeyError(f"unknown archive '{name}'; available: {', '.join(available_archives())}") from None


def slice_command(spec_file: str = "slice.json") -> str:
    """The CLI invocation that lands a saved slice (what the UI shows when fetch is disabled)."""
    return f"argus-quarry archives fetch {spec_file}"


__all__ = [
    "PAGE_MAX",
    "SLICE_MAX",
    "Archive",
    "ArchiveQuery",
    "ArchiveUnavailable",
    "SliceSpec",
    "available_archives",
    "get_archive",
    "slice_command",
]
