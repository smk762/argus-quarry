"""Wikimedia Commons downloader (Phase 1).

Uses the MediaWiki API's ``generator=search`` over the File namespace to find
candidate images for a subject (person, garment, scene or concept), following
``continue`` across pages and searching each alias after the primary name, then reads
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
#: Hits per ``generator=search`` request: the API's limit for non-bot clients.
PAGE_SIZE = 50
#: Pages followed per search string. CirrusSearch stops paging at offset 10,000 (200 pages of 50) anyway.
MAX_PAGES = 200
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
        """Yield up to ``limit`` candidates, searching the subject's name and then each alias.

        Each search follows the API's ``continue`` token one page (at most
        :data:`PAGE_SIZE` hits) at a time, so ``limit`` is not capped by a single
        request. A file found by more than one search is yielded once.
        """
        if limit <= 0:
            return
        seen: set[str] = set()
        yielded = 0
        for query in subject.queries:
            for record in self._search(subject, query, limit - yielded):
                if record.remote_url in seen:
                    continue
                seen.add(record.remote_url)
                yield record
                yielded += 1
                if yielded >= limit:
                    return

    def _search(self, subject: Subject, query: str, wanted: int) -> Iterator[SourceRecord]:
        """Page through one ``generator=search`` query until the results (or ``max_pages``) run out.

        Callers stop consuming once they have enough, so no further pages are requested.
        """
        base: dict[str, object] = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,  # File:
            "gsrlimit": max(1, min(wanted, PAGE_SIZE)),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiextmetadatafilter": "|".join(
                ["LicenseShortName", "License", "UsageTerms", "Artist", "Credit", "DateTimeOriginal"]
            ),
        }
        params = base
        seen_continue: set[tuple] = set()
        for page_no in range(MAX_PAGES):
            try:
                data = self.net.get_json(API_URL, params=params, source=self.name)
            except Exception as exc:
                logger.warning("commons_search_failed", subject=subject.name, query=query, page=page_no, error=str(exc))
                return

            pages = (data.get("query") or {}).get("pages") or {}
            # ``pages`` is keyed by page id; ``index`` is the search rank, so keep relevance order.
            for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
                record = self._record(subject, page)
                if record is not None:
                    yield record

            cont = data.get("continue")
            if not isinstance(cont, dict) or not cont:
                return
            key = tuple(sorted((k, str(v)) for k, v in cont.items()))
            if key in seen_continue:  # a token that doesn't advance would loop forever
                logger.warning("commons_continue_stuck", subject=subject.name, query=query, token=cont)
                return
            seen_continue.add(key)
            # Onto the original params, not the last request's: a continue key the API dropped
            # (e.g. an iicontinue) must not be resent with the next page.
            params = {**base, **cont, "gsrlimit": PAGE_SIZE}
        logger.info("commons_search_page_cap", subject=subject.name, query=query, pages=MAX_PAGES)

    def _record(self, subject: Subject, page: dict) -> SourceRecord | None:
        infos = page.get("imageinfo") or []
        if not infos:
            return None
        info = infos[0]
        if info.get("mime") not in _IMAGE_MIMES:
            return None

        remote_url = info.get("url")
        if not remote_url:
            return None
        ext = info.get("extmetadata") or {}

        raw_licence = _meta(ext, "LicenseShortName") or _meta(ext, "License") or _meta(ext, "UsageTerms")
        artist = _clean(_meta(ext, "Artist"))
        credit = _clean(_meta(ext, "Credit"))
        title = (page.get("title") or "").removeprefix("File:") or None
        year = _extract_year(_meta(ext, "DateTimeOriginal"), title)

        return SourceRecord(
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
