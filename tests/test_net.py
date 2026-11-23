"""NetClient.download resume: only a recorded, matching resource is resumed, never spliced."""

from __future__ import annotations

import re

import httpx

from argus_quarry.config import QuarryConfig
from argus_quarry.net import NetClient

URL = "https://img.test/a.jpg"
BODY = bytes(range(256)) * 4  # 1 KiB
ETAG = '"v2"'


class Server:
    """Serves BODY with an ETag; honours Range + If-Range like a real origin (or misbehaves on request)."""

    def __init__(self, *, wrong_offset: bool = False) -> None:
        self.wrong_offset = wrong_offset
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rng = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if rng is None or (if_range is not None and if_range != ETAG):
            return httpx.Response(200, content=BODY, headers={"etag": ETAG})
        first = int(re.match(r"bytes=(\d+)-", rng).group(1))
        if first >= len(BODY):
            return httpx.Response(416, headers={"content-range": f"bytes */{len(BODY)}"})
        if self.wrong_offset:
            first = 0
        return httpx.Response(
            206,
            content=BODY[first:],
            headers={"etag": ETAG, "content-range": f"bytes {first}-{len(BODY) - 1}/{len(BODY)}"},
        )


def _net(tmp_path, server: Server) -> NetClient:
    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=3)
    return NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(server)))


def _partial(tmp_path, data: bytes, validator: str | None):
    dest = tmp_path / "a.jpg"
    dest.with_suffix(".jpg.part").write_bytes(data)
    if validator is not None:
        dest.with_suffix(".jpg.part.validator").write_text(validator)
    return dest


def test_resume_sends_if_range_and_appends_only_the_matching_tail(tmp_path):
    server = Server()
    dest = _partial(tmp_path, BODY[:100], ETAG)
    with _net(tmp_path, server) as net:
        result = net.download(URL, dest)
    assert dest.read_bytes() == BODY and result.resumed
    assert server.requests[0].headers["range"] == "bytes=100-"
    assert server.requests[0].headers["if-range"] == ETAG
    assert not dest.with_suffix(".jpg.part.validator").exists()


def test_a_changed_resource_restarts_instead_of_splicing(tmp_path):
    server = Server()
    dest = _partial(tmp_path, b"OLD-RENDITION-BYTES", '"v1"')
    with _net(tmp_path, server) as net:
        net.download(URL, dest)
    assert dest.read_bytes() == BODY


def test_a_206_at_the_wrong_offset_is_never_appended(tmp_path):
    server = Server(wrong_offset=True)
    dest = _partial(tmp_path, BODY[:100], ETAG)
    with _net(tmp_path, server) as net:
        net.download(URL, dest)
    assert dest.read_bytes() == BODY


def test_a_partial_with_no_recorded_identity_is_not_resumed(tmp_path):
    server = Server()
    dest = _partial(tmp_path, b"who-knows", None)
    with _net(tmp_path, server) as net:
        net.download(URL, dest)
    assert "range" not in server.requests[0].headers
    assert dest.read_bytes() == BODY


def test_a_complete_part_whose_rename_was_interrupted_is_finalised(tmp_path):
    server = Server()
    dest = _partial(tmp_path, BODY, ETAG)
    with _net(tmp_path, server) as net:
        result = net.download(URL, dest)
    assert dest.read_bytes() == BODY and result.size == len(BODY)
    assert len(server.requests) == 1  # the 416 finished it: no retries, no re-download


def test_every_request_is_paced_and_carries_the_user_agent(tmp_path, monkeypatch):
    """Politeness can't be bypassed: each request, retries included, takes a token and sends the UA."""
    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=3)
    assert NetClient(cfg)._client.headers["User-Agent"] == cfg.user_agent

    taken: list[str] = []
    seen: list[str] = []
    flaky = iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return next(flaky) if "api" in request.url.host else Server()(request)

    net = NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "ua"}))
    monkeypatch.setattr(net, "_bucket", lambda source: type("B", (), {"take": lambda _s: taken.append(source)})())
    assert net.get_json("https://api.test/x", source="commons") == {"ok": True}
    net.download(URL, tmp_path / "a.jpg", source="commons")
    assert taken == ["commons"] * len(seen) and len(seen) == 3 and set(seen) == {"ua"}


def _gzip_server(requests: list[httpx.Request], *, etag: str | None = ETAG):
    """An origin that gzips every body regardless of Accept-Encoding, ranges included."""
    import gzip

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"content-encoding": "gzip", **({"etag": etag} if etag else {})}
        rng = request.headers.get("range")
        if rng is None:
            return httpx.Response(200, content=gzip.compress(BODY), headers=headers)
        first, last = (int(g) if g else None for g in re.match(r"bytes=(\d+)-(\d*)", rng).groups())
        last = len(BODY) - 1 if last is None else last
        headers["content-range"] = f"bytes {first}-{last}/{len(BODY)}"
        return httpx.Response(206, content=gzip.compress(BODY[first : last + 1]), headers=headers)

    return handler


def _mock_net(tmp_path, handler) -> NetClient:
    cfg = QuarryConfig(home=tmp_path / "q", backoff_base=0.0, max_retries=3)
    return NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_byte_counted_requests_ask_for_the_identity_encoding(tmp_path):
    """Range offsets count stored bytes; a transparently-decoded body would count different ones."""
    server = Server()
    with _net(tmp_path, server) as net:
        net.download(URL, tmp_path / "a.jpg")
        net.read_range(URL, 0, 10)
    assert server.requests and all(r.headers["accept-encoding"] == "identity" for r in server.requests)


def test_an_interrupted_encoded_download_is_never_resumed_from_decoded_bytes(tmp_path):
    """A .part of decoded bytes is no offset into the encoded resource: restart, never Range from it."""
    import gzip
    import os

    body = os.urandom(200_000)  # incompressible, so a cut stream still decodes to a sizeable prefix
    packed = gzip.compress(body)
    requests: list[httpx.Request] = []

    class CutStream(httpx.SyncByteStream):
        def __iter__(self):
            yield packed[: len(packed) // 2]
            raise httpx.ReadError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"content-encoding": "gzip", "etag": ETAG}
        if len(requests) == 1:
            return httpx.Response(200, stream=CutStream(), headers=headers)
        if "range" in request.headers:  # what a resume would get: a gzip fragment of the tail
            first = int(re.match(r"bytes=(\d+)-", request.headers["range"]).group(1))
            headers["content-range"] = f"bytes {first}-{len(body) - 1}/{len(body)}"
            return httpx.Response(206, content=gzip.compress(body[first:]), headers=headers)
        return httpx.Response(200, content=packed, headers=headers)

    dest = tmp_path / "a.jpg"
    with _mock_net(tmp_path, handler) as net:
        net.download(URL, dest)
    assert len(requests) == 2 and "range" not in requests[1].headers
    assert dest.read_bytes() == body


def test_an_encoded_byte_range_is_refused_not_miscounted(tmp_path):
    import pytest

    requests: list[httpx.Request] = []
    with _mock_net(tmp_path, _gzip_server(requests)) as net:
        with pytest.raises(RuntimeError, match="failed after"):
            net.read_range(URL, 100, 10)
        with pytest.raises(RuntimeError, match="failed after"):
            net.download(f"{URL}#bytes=100-109", tmp_path / "b.jpg")
    assert not (tmp_path / "b.jpg").exists() and not (tmp_path / "b.jpg.part").exists()
    assert all(r.headers["accept-encoding"] == "identity" for r in requests)  # asked; the server ignored it


def test_an_encoded_resume_is_never_appended(tmp_path):
    requests: list[httpx.Request] = []
    dest = _partial(tmp_path, BODY[:100], ETAG)
    with _mock_net(tmp_path, _gzip_server(requests)) as net:
        net.download(URL, dest)
    assert dest.read_bytes() == BODY  # restarted from 0, not a gzip fragment spliced onto the .part
    assert requests[0].headers["range"] == "bytes=100-" and "range" not in requests[1].headers


def test_get_json_never_retries_a_permanent_client_error(tmp_path):
    import pytest

    from argus_quarry.net import RemoteGone

    calls: list[int] = []

    def handler(status: int):
        def h(request: httpx.Request) -> httpx.Response:
            calls.append(status)
            return httpx.Response(status, json={})

        return h

    for status, exc in ((404, RemoteGone), (403, RemoteGone), (410, RemoteGone), (400, httpx.HTTPStatusError)):
        calls.clear()
        with _mock_net(tmp_path, handler(status)) as net, pytest.raises(exc):
            net.get_json("https://api.test/x", source="commons")
        assert calls == [status]  # one request: no backoff, no extra rate-limit tokens


def test_get_json_still_retries_a_transient_status(tmp_path):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    with _mock_net(tmp_path, handler) as net:
        assert net.get_json("https://api.test/x") == {"ok": True}
    assert len(calls) == 2  # the 503 body is not a successful answer
