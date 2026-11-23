"""Flickr upload dates (#53): keyless photo pages by default, the API when a key is configured."""

from __future__ import annotations

import json
import traceback

import httpx
import pytest
from pydantic import SecretStr

from argus_quarry.archives.flickr_dates import (
    API_RATE_SOURCE,
    PAGE_RATE_SOURCE,
    FlickrDateError,
    FlickrDates,
    parse_posted,
)
from argus_quarry.config import QuarryConfig
from argus_quarry.net import NetClient

KEY = "s3cr3t-flickr-key"


def _record(photo_id: str, posted: str) -> str:
    return (
        '{"data":{"_flickrModelRegistry":"photo-stats-models","dateTaken":"1900-01-01 00:00:00",'
        f'"datePosted":"{posted}","takenGranularity":8,"id":"{photo_id}"}},"exportMetaType":"model"}}'
    )


#: A photo page lists its own record and other photos' (the owner's stream beside it).
PAGE = f"<html><script>{_record('111', '1200000000')}{_record('222', '1300000000')}</script></html>"


def test_the_page_parser_reads_only_the_asked_photos_record():
    assert parse_posted(PAGE, 222) == 1300000000
    assert parse_posted(PAGE, 111) == 1200000000
    assert parse_posted(PAGE, 333) is None  # a missing photo's page still lists others: never borrow theirs


def _dates(tmp_path, handler, *, key=None) -> tuple[FlickrDates, list[httpx.Request], QuarryConfig]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=2)
    net = NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(record), headers={"User-Agent": "ua"}))
    return FlickrDates(net, key), seen, cfg


def test_keyless_reads_the_public_photo_page(tmp_path, monkeypatch):
    dates, seen, _ = _dates(tmp_path, lambda r: httpx.Response(200, text=PAGE))
    sources: list[str] = []
    real = dates.net._bucket
    monkeypatch.setattr(dates.net, "_bucket", lambda source: sources.append(source) or real(source))
    got = dates.upload_date(222, "someone@N01")
    assert (got.posted, got.via) == (1300000000, "page") and dates.via == "page"
    assert str(seen[0].url) == "https://www.flickr.com/photos/someone@N01/222/"
    assert sources == [PAGE_RATE_SOURCE]  # paced in its own bucket


def test_keyless_gone_photo_is_none_and_a_page_without_it_is_an_error(tmp_path):
    dates, _, _ = _dates(tmp_path, lambda r: httpx.Response(404, text=PAGE))
    assert dates.upload_date(222, "x") is None
    dates, _, _ = _dates(tmp_path, lambda r: httpx.Response(200, text=PAGE))
    with pytest.raises(FlickrDateError, match="no datePosted"):
        dates.upload_date(333, "x")


def _api(ok: dict | None = None, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=ok or {})

    return handler


def test_with_a_key_the_api_answers(tmp_path):
    body = {"stat": "ok", "photo": {"id": "222", "dates": {"posted": "1300000000", "taken": "1900-01-01"}}}
    dates, seen, _ = _dates(tmp_path, _api(body), key=SecretStr(KEY))
    got = dates.upload_date(222, "ignored")
    assert (got.posted, got.via) == (1300000000, "api") and dates.via == "api"
    params = dict(seen[0].url.params)
    assert params["method"] == "flickr.photos.getInfo" and params["photo_id"] == "222" and params["api_key"] == KEY
    assert seen[0].url.host == "api.flickr.com"


@pytest.mark.parametrize("code", [1, 2])
def test_an_api_not_found_or_private_photo_is_none(tmp_path, code):
    dates, _, _ = _dates(tmp_path, _api({"stat": "fail", "code": code, "message": "Photo not found"}), key=KEY)
    assert dates.upload_date(222, "x") is None


def test_the_api_key_never_reaches_an_error_message(tmp_path):
    for handler in (_api(status=400), _api({"stat": "fail", "code": 100, "message": "Invalid API Key"})):
        dates, _, _ = _dates(tmp_path, handler, key=KEY)
        with pytest.raises(FlickrDateError) as info:
            dates.upload_date(222, "x")
        assert KEY not in str(info.value)
        assert info.value.__cause__ is None
        # `raise ...` inside `except httpx.HTTPError` sets __context__ even when __cause__ stays None.
        # from None suppresses it; the rendered chain is what a log line would show.
        assert info.value.__context__ is None or info.value.__suppress_context__
        assert KEY not in "".join(traceback.format_exception(info.value))


def test_the_key_comes_from_the_environment_and_never_shows_in_a_repr(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARRY_FLICKR_API_KEY", f"  {KEY} ")
    monkeypatch.setenv("QUARRY_HOME", str(tmp_path))
    cfg = QuarryConfig.from_env()
    assert cfg.flickr_api_key.get_secret_value() == KEY
    assert KEY not in repr(cfg) and KEY not in json.dumps(cfg.model_dump(mode="json"))
    monkeypatch.setenv("QUARRY_FLICKR_API_KEY", "")
    assert QuarryConfig.from_env().flickr_api_key is None


def test_the_api_has_its_own_rate_bucket(tmp_path, monkeypatch):
    body = {"stat": "ok", "photo": {"dates": {"posted": "1"}}}
    dates, _, _ = _dates(tmp_path, _api(body), key=KEY)
    sources: list[str] = []
    real = dates.net._bucket
    monkeypatch.setattr(dates.net, "_bucket", lambda source: sources.append(source) or real(source))
    dates.upload_date(1, "x")
    assert sources == [API_RATE_SOURCE]
