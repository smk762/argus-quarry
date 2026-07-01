"""Downloader registry — ``name -> Downloader`` subclass.

Sources register here so the CLI can resolve ``--source commons`` to a class
without importing each module explicitly. Phase 2/3 sources (loc, smithsonian,
rijksmuseum, lac, europeana, flickr) slot in the same way.
"""

from __future__ import annotations

from argus_quarry.downloaders.base import Downloader
from argus_quarry.downloaders.commons import CommonsDownloader

_REGISTRY: dict[str, type[Downloader]] = {
    CommonsDownloader.name: CommonsDownloader,
}


def get_downloader(name: str) -> type[Downloader]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown source '{name}'; available: {', '.join(available_sources())}") from None


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Downloader", "CommonsDownloader", "get_downloader", "available_sources"]
