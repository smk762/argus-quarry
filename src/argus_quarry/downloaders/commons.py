"""Wikimedia Commons downloader (Phase 1).

Uses the MediaWiki API's ``generator=search`` over the File namespace to find
candidate images for a subject (person, garment, scene or concept), then reads
each file's ``imageinfo`` + ``extmetadata`` for the full-resolution URL,
dimensions, licence and attribution. Licence *acceptance* is decided centrally
in ingest — this module just faithfully reports whatever provenance Commons
exposes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import structlog

from argus_quarry.downloaders.base import Downloader
from argus_quarry.models import SourceRecord, Subject

logger = structlog.get_logger()

API_URL = "https://commons.wikimedia.org/w/api.php"
_IMAGE_MIMES = {"image/jpeg", "image/png"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")


def _clean(value: str | None) -> str | None:
    """Strip HTML tags / collapse whitespace from an ``extmetadata`` value."""
    if not value:
        return None
    text = _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", value)).strip()
    return text or None


def _extract_year(*values: str | None) -> int | None:
    for v in values:
        if not v:
            continue
        m = _YEAR_RE.search(v)
        if m:
            return int(m.group(1))
    return None


def _meta(extmetadata: dict, key: str) -> str | None:
    entry = extmetadata.get(key)
    if isinstance(entry, dict):
        return entry.get("value")
    return None


class CommonsDownloader(Downloader):
    name = "commons"

    def harvest(self, subject: Subject, limit: int) -> Iterator[SourceRecord]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": subject.query,
            "gsrnamespace": 6,  # File:
            "gsrlimit": max(1, min(limit, 50)),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiextmetadatafilter": "|".join(
                ["LicenseShortName", "License", "UsageTerms", "Artist", "Credit", "DateTimeOriginal"]
            ),
        }
        try:
            data = self.net.get_json(API_URL, params=params, source=self.name)
        except Exception as exc:
            logger.warning("commons_search_failed", subject=subject.name, error=str(exc))
            return

        pages = (data.get("query") or {}).get("pages") or {}
        yielded = 0
        for page in pages.values():
            if yielded >= limit:
                break
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            if info.get("mime") not in _IMAGE_MIMES:
                continue

            remote_url = info.get("url")
            if not remote_url:
                continue
            ext = info.get("extmetadata") or {}

            raw_licence = _meta(ext, "LicenseShortName") or _meta(ext, "License") or _meta(ext, "UsageTerms")
            artist = _clean(_meta(ext, "Artist"))
            credit = _clean(_meta(ext, "Credit"))
            title = (page.get("title") or "").removeprefix("File:") or None
            year = _extract_year(_meta(ext, "DateTimeOriginal"), title)

            yield SourceRecord(
                subject=subject.folder,
                category=subject.category,
                wikidata_id=subject.wikidata_id,
                birth_year=subject.birth_year,
                death_year=subject.death_year,
                occupation=subject.occupation,
                title=title,
                photographer=artist,
                year=year,
                remote_url=remote_url,
                source=self.name,
                source_url=info.get("descriptionurl") or remote_url,
                licence=(raw_licence or "unknown").strip(),
                attribution=artist or credit,
            )
            yielded += 1
