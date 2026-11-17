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
