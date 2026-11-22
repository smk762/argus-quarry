"""When a Flickr photo was **uploaded** (posted), one photo at a time (#53).

Used to measure the id -> upload-year table (:mod:`argus_quarry.archives.upload_year`), not at
index time. Two backends, picked by whether a key is configured:

- **keyless** (default): the public photo page embeds a ``photo-stats-models`` record per photo
  with its ``datePosted`` (unix seconds). A missing photo's page still lists *other* photos'
  records, so only the record whose ``id`` is the one asked for counts.
- **API** (``QUARRY_FLICKR_API_KEY``): ``flickr.photos.getInfo``'s ``dates.posted``. Flickr issues
  keys to Pro accounts only. The key is never logged or put in an error message.

Every request goes through :class:`~argus_quarry.net.NetClient`, so it is paced per source and
carries the polite User-Agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from argus_quarry.net import NetClient, RemoteGone

API_URL = "https://api.flickr.com/services/rest/"
PAGE_URL = "https://www.flickr.com/photos/{owner}/{photo_id}/"
#: Rate-limit buckets (``QuarryConfig.rates``): pages are scraped more gently than the API is called.
PAGE_RATE_SOURCE = "flickr-pages"
API_RATE_SOURCE = "flickr-api"
#: flickr.photos.getInfo's "Photo not found" / "Permission denied" (a private photo).
_API_GONE_CODES = {1, 2}

_STATS_RECORD = re.compile(r'"_flickrModelRegistry":"photo-stats-models",([^{}]*)')
_FIELD = re.compile(r'"(datePosted|id)":"(\d+)"')


class FlickrDateError(RuntimeError):
    """The upload date could not be read (an unexpected page or API answer); the photo may still exist."""


@dataclass(frozen=True)
class UploadDate:
    photo_id: int
    posted: int  # unix seconds, UTC
    via: str  # "page" or "api"


def parse_posted(html: str, photo_id: int) -> int | None:
    """``datePosted`` of ``photo_id`` from a photo page's embedded records, or None if it has none."""
    for match in _STATS_RECORD.finditer(html):
        fields = dict(_FIELD.findall(match.group(1)))
        if fields.get("id") == str(photo_id) and "datePosted" in fields:
            return int(fields["datePosted"])
    return None


class FlickrDates:
    """Upload dates via the API when a key is set, else via the public photo pages."""

    def __init__(self, net: NetClient, api_key: SecretStr | str | None = None) -> None:
        self.net = net
        self._key = api_key if isinstance(api_key, SecretStr) or api_key is None else SecretStr(api_key)

    @property
    def via(self) -> str:
        return "api" if self._key else "page"

    def upload_date(self, photo_id: int, owner: str) -> UploadDate | None:
        """The photo's upload date; None when Flickr says it is gone, deleted or private.

        ``owner`` is the account segment of the photo URL (Megalith's ``collection``): the page
        URL needs it (``/photos/-/<id>/`` is a 404). The API ignores it.
        """
        if self._key:
            return self._from_api(photo_id)
        return self._from_page(photo_id, owner)

    def _from_page(self, photo_id: int, owner: str) -> UploadDate | None:
        try:
            html = self.net.get_text(PAGE_URL.format(owner=owner, photo_id=photo_id), source=PAGE_RATE_SOURCE)
        except RemoteGone:
            return None
        posted = parse_posted(html, photo_id)
        if posted is None:
            raise FlickrDateError(f"photo {photo_id}: page has no datePosted for it (layout changed, or gone)")
        return UploadDate(photo_id, posted, "page")

    def _from_api(self, photo_id: int) -> UploadDate | None:
        assert self._key is not None
        params = {
            "method": "flickr.photos.getInfo",
            "api_key": self._key.get_secret_value(),
            "photo_id": str(photo_id),
            "format": "json",
            "nojsoncallback": "1",
        }
        try:
            data = self.net.get_json(API_URL, params=params, source=API_RATE_SOURCE)
        except RemoteGone:
            return None
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            # httpx errors name the request URL, query string (the key) included: never pass that on.
            raise FlickrDateError(f"photo {photo_id}: API request failed ({type(exc).__name__})") from None
        if data.get("stat") != "ok":
            if data.get("code") in _API_GONE_CODES:
                return None
            raise FlickrDateError(f"photo {photo_id}: API error {data.get('code')}: {data.get('message')}")
        try:
            return UploadDate(photo_id, int(data["photo"]["dates"]["posted"]), "api")
        except (KeyError, TypeError, ValueError) as exc:
            raise FlickrDateError(f"photo {photo_id}: API answer has no dates.posted") from exc
