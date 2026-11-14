"""Megalith's Drawthings mirror: tar offset scan, resumable index, and ingest fallback.

Network is a fake range server (``httpx.MockTransport``) behind a real ``NetClient``,
so the Range / Content-Range / redirect handling is what actually runs.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path

import httpx
import pytest

duckdb = pytest.importorskip("duckdb", reason="archives extra not installed")

from conftest import make_image_bytes  # noqa: E402

from argus_quarry.archives import SliceSpec, get_archive  # noqa: E402
from argus_quarry.archives.mirror import MegalithMirror, TarMember, scan_tar, shard_url  # noqa: E402
from argus_quarry.config import QuarryConfig  # noqa: E402
from argus_quarry.ingest import IngestEngine, fetch  # noqa: E402
from argus_quarry.net import NetClient, RemoteGone, byte_range_url, split_byte_range  # noqa: E402
from argus_quarry.store import ProvenanceStore  # noqa: E402

REV = "829c00885e3926118674375b50c41d970aceb5ac"
CDN = "https://cdn.test"


def _write_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    cols = list(rows[0])
    values = ", ".join("(" + ", ".join("?" * len(cols)) + ")" for _ in rows)
    con.execute(
        f"COPY (SELECT * FROM (VALUES {values}) t({', '.join(cols)})) TO '{path}' (FORMAT parquet)",
        [v for r in rows for v in r.values()],
    )
    con.close()


def _shard_tar(samples: dict[str, bytes]) -> bytes:
    """A webdataset-style shard like Drawthings': key.jpg/.json/.txt, each behind a PAX header."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for key, jpg in samples.items():
            for name, data in (
                (f"{key}.jpg", jpg),
                (f"{key}.json", b'{"key": "%s"}' % key.encode()),
                (f"{key}.txt", b"x"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.pax_headers = {"comment": "mtime"}  # force a PAX header per member, as Drawthings has
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _flickr(pid: str) -> str:
    return f"https://farm1.staticflickr.com/1/{pid}_abc_b.jpg"


class FakeHub:
    """Hugging Face (resolve -> signed CDN redirect, byte ranges) plus Flickr, in one transport."""

    def __init__(self, shards: dict[str, bytes], flickr: dict[str, bytes | int]) -> None:
        self.shards = shards
        self.flickr = flickr  # url -> bytes, or an HTTP status for a gone photo
        self.requests: list[tuple[str, str, str | None]] = []
        self.expire_next_cdn_read = False
        self.ignore_range = False
        self.sig = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        rng = request.headers.get("range")
        self.requests.append((request.method, url, rng))
        if url.startswith("https://huggingface.co/"):
            shard = url.rsplit("/", 1)[1].removesuffix(".tar")
            if shard not in self.shards:
                return httpx.Response(404)
            self.sig += 1
            return httpx.Response(302, headers={"location": f"{CDN}/{shard}.tar?sig={self.sig}"})
        if url.startswith(CDN):
            if self.expire_next_cdn_read:
                self.expire_next_cdn_read = False
                return httpx.Response(403)
            data = self.shards[request.url.path.strip("/").removesuffix(".tar")]
            if self.ignore_range or rng is None:
                return httpx.Response(200, content=data)
            first, last = (int(x) if x else None for x in re.match(r"bytes=(\d+)-(\d*)", rng).groups())
            if first >= len(data):
                return httpx.Response(416)
            last = min(len(data) - 1, last if last is not None else len(data) - 1)
            return httpx.Response(
                206, content=data[first : last + 1], headers={"content-range": f"bytes {first}-{last}/{len(data)}"}
            )
        if url in self.flickr:
            body = self.flickr[url]
            return httpx.Response(body) if isinstance(body, int) else httpx.Response(200, content=body)
        return httpx.Response(404)

    def hits(self, prefix: str) -> int:
        return sum(1 for _m, url, _r in self.requests if url.startswith(prefix))


def _net(cfg: QuarryConfig, hub: FakeHub) -> NetClient:
    return NetClient(cfg, client=httpx.Client(transport=httpx.MockTransport(hub), follow_redirects=True))


@pytest.fixture
def images() -> dict[str, bytes]:
    return {pid: make_image_bytes(48, seed=int(pid)) for pid in ("9001", "9002", "9003", "9004")}


@pytest.fixture
def mirror_env(tmp_path: Path, images: dict[str, bytes]):
    """A Megalith archive (links + drawthings metadata + SOURCE.json), its two tar shards, and a fake hub."""
    root = tmp_path / "megalith"
    pids = list(images)
    _write_parquet(
        root / "links" / "data" / "megalith-00001-of-00010.parquet",
        [
            {"url": f"https://farm1.staticflickr.com/1/{p}_abc.jpg", "url_highres": _flickr(p),
             "url_source": f"https://www.flickr.com/photos/111@N01/{p}"}
            for p in pids
        ],
    )  # fmt: skip
    # Shard 00000 holds 9001/9002, shard 00001 holds 9003; 9004 failed in Drawthings (not mirrored).
    layout = {"00000": {"000000001": "9001", "000000002": "9002"}, "00001": {"000000003": "9003"}}
    for shard, members in layout.items():
        rows = [
            {"url": _flickr(p), "key": k, "status": "success", "width": 48, "height": 48} for k, p in members.items()
        ]
        if shard == "00001":
            rows.append({"url": _flickr("9004"), "key": "000000004", "status": "failed_to_download",
                         "width": None, "height": None})  # fmt: skip
        _write_parquet(root / "drawthings" / f"{shard}.parquet", rows)
    (root / "SOURCE.json").write_text(json.dumps({"parts": {"drawthings": {"revision": REV}}}))
    shards = {s: _shard_tar({k: images[p] for k, p in m.items()}) for s, m in layout.items()}
    cfg = QuarryConfig(home=tmp_path / "quarry", archive_dirs={"megalith": root}, backoff_base=0.0, max_retries=2)
    cfg.ensure_dirs()
    get_archive("megalith", cfg).build_index()
    hub = FakeHub(shards, flickr={})
    return cfg, hub


# ── tar scan ─────────────────────────────────────────────────────────


def test_scan_tar_finds_member_offsets_reading_only_headers():
    images = {"9001": make_image_bytes(200, seed=1), "9002": make_image_bytes(200, seed=2)}  # >> one window
    data = _shard_tar({"000000001": images["9001"], "000000002": images["9002"]})
    reads: list[tuple[int, int]] = []

    def read(offset: int, length: int) -> bytes:
        reads.append((offset, length))
        return data[offset : offset + length]

    members = list(scan_tar(read))
    assert [m.name for m in members] == [
        "000000001.jpg", "000000001.json", "000000001.txt", "000000002.jpg", "000000002.json", "000000002.txt",
    ]  # fmt: skip
    jpg = members[0]
    assert data[jpg.offset : jpg.offset + jpg.size] == images["9001"]
    assert data[members[3].offset : members[3].offset + members[3].size] == images["9002"]
    # The image bytes are skipped: one read per sample (its .json/.txt and the next image's
    # headers share the window), not one per member or per block.
    assert len(reads) <= 3
    assert sum(n for _o, n in reads) < len(data) / 2


def test_scan_tar_reads_pax_long_names():
    long = "d/" + "n" * 150 + ".jpg"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(long)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    data = buf.getvalue()
    (member,) = scan_tar(lambda o, n: data[o : o + n])
    assert member == TarMember(long, member.offset, 3)
    assert data[member.offset : member.offset + 3] == b"abc"


def test_scan_tar_rejects_a_misaligned_header():
    with pytest.raises(ValueError, match="checksum"):
        list(scan_tar(lambda o, n: (b"garbage!" * 64)[o : o + n]))


# ── byte-range URLs and NetClient ────────────────────────────────────


def test_byte_range_url_round_trips():
    url = byte_range_url("https://h/x.tar", 1536, 100)
    assert url == "https://h/x.tar#bytes=1536-1635"
    assert split_byte_range(url) == ("https://h/x.tar", (1536, 1635))
    assert split_byte_range("https://h/x.jpg") == ("https://h/x.jpg", None)


def test_ranged_download_fetches_exactly_the_member(mirror_env, images, tmp_path):
    cfg, hub = mirror_env
    data = hub.shards["00000"]
    jpg = next(m for m in scan_tar(lambda o, n: data[o : o + n]) if m.name.endswith(".jpg"))
    dest = tmp_path / "out.jpg"
    with _net(cfg, hub) as net:
        net.download(byte_range_url(shard_url(REV, "00000"), jpg.offset, jpg.size), dest)
    assert dest.read_bytes() == images["9001"]
    assert hub.requests[-1][2] == f"bytes={jpg.offset}-{jpg.offset + jpg.size - 1}"  # Range kept across the 302


def test_ranged_download_refuses_a_server_that_ignores_range(mirror_env, tmp_path):
    cfg, hub = mirror_env
    hub.ignore_range = True  # a 200 would stream the whole ~1.9 GB shard
    with _net(cfg, hub) as net, pytest.raises(RuntimeError, match="failed after"):
        net.download(byte_range_url(shard_url(REV, "00000"), 1536, 100), tmp_path / "out.jpg")
    assert not (tmp_path / "out.jpg").exists()


def test_gone_photo_raises_at_once_without_retries(mirror_env, tmp_path):
    cfg, hub = mirror_env
    hub.flickr[_flickr("9001")] = 410
    with _net(cfg, hub) as net, pytest.raises(RemoteGone) as exc:
        net.download(_flickr("9001"), tmp_path / "x.jpg")
    assert exc.value.status == 410
    assert hub.hits(_flickr("9001")) == 1


# ── offset index ─────────────────────────────────────────────────────


def test_index_build_maps_flickr_urls_to_member_ranges(mirror_env, images):
    cfg, hub = mirror_env
    mirror = MegalithMirror(cfg)
    with _net(cfg, hub) as net:
        result = mirror.build(net, workers=2)
    assert result["scanned"] == result["shards"] == 2 and result["members"] == 3 and not result["failed"]
    found = mirror.lookup([_flickr(p) for p in images])
    assert set(found) == {_flickr("9001"), _flickr("9002"), _flickr("9003")}  # 9004 failed in Drawthings
    base, (first, last) = split_byte_range(found[_flickr("9003")])
    assert base == shard_url(REV, "00001")
    assert hub.shards["00001"][first : last + 1] == images["9003"]
    # One resolve per shard; every range read went to the signed CDN URL, not the resolver.
    assert hub.hits("https://huggingface.co/") == 2
    assert mirror.status()["members"] == 3


def test_index_build_resumes_and_skips_scanned_shards(mirror_env):
    cfg, hub = mirror_env
    mirror = MegalithMirror(cfg)
    with _net(cfg, hub) as net:
        first = mirror.build(net, shards=["00000"])
        assert first["scanned"] == 1 and first["members"] == 2
        hub.requests.clear()
        second = mirror.build(net)
    assert second["this_run"] == 1 and second["members"] == 3
    assert all("00000" not in url for _m, url, _r in hub.requests)  # shard 00000 was not re-read


def test_failed_shard_is_reported_and_retried_next_run(mirror_env):
    cfg, hub = mirror_env
    mirror = MegalithMirror(cfg)
    saved = hub.shards.pop("00001")
    with _net(cfg, hub) as net:
        result = mirror.build(net)
        assert result["failed"] == ["00001"] and result["members"] == 2
        hub.shards["00001"] = saved
        assert mirror.build(net)["members"] == 3


def test_expired_signed_url_is_resolved_again(mirror_env):
    cfg, hub = mirror_env
    hub.expire_next_cdn_read = True
    with _net(cfg, hub) as net:
        assert MegalithMirror(cfg).build(net, shards=["00000"])["members"] == 2
    assert hub.hits("https://huggingface.co/") == 2  # initial resolve + one after the 403


def test_offsets_from_another_revision_are_discarded(mirror_env):
    cfg, hub = mirror_env
    mirror = MegalithMirror(cfg)
    with _net(cfg, hub) as net:
        mirror.build(net, shards=["00000"])
        (mirror.work_dir / "REVISION").write_text("someotherrevision\n")
        hub.requests.clear()
        mirror.build(net, shards=["00000"])
    assert any("00000" in url for _m, url, _r in hub.requests)  # rescanned for the pinned revision


def test_no_index_means_no_fallback_not_an_error(mirror_env):
    cfg, _hub = mirror_env
    records = list(get_archive("megalith", cfg).records(SliceSpec(archive="megalith", subject="x")))
    assert records and all(r.alternates == [] for r in records)


# ── ingest fallback ──────────────────────────────────────────────────


def _land(cfg: QuarryConfig, hub: FakeHub, spec: SliceSpec):
    with ProvenanceStore(cfg.db_path) as store, _net(cfg, hub) as net:
        summary = fetch(IngestEngine(cfg, store, net), get_archive("megalith", cfg).records(spec))
        rows = {r.source_url.rsplit("/", 1)[1]: r for r in store.iter_photographs(status=None)}
    return summary, rows


def test_deleted_flickr_photo_lands_from_the_mirror(mirror_env, images):
    cfg, hub = mirror_env
    with _net(cfg, hub) as net:
        MegalithMirror(cfg).build(net)
    hub.flickr.update({_flickr("9001"): 410, _flickr("9002"): images["9002"], _flickr("9003"): 404,
                       _flickr("9004"): 404})  # fmt: skip
    spec = SliceSpec(archive="megalith", category="setting", subject="streets", limit=10)

    summary, rows = _land(cfg, hub, spec)
    assert (summary.complete, summary.failed) == (3, 1)  # 9004: gone on Flickr and never mirrored

    gone = rows["9001"]
    assert gone.status == "complete"
    assert gone.remote_url == _flickr("9001")  # full-res primary kept: it is the resume key
    assert gone.fetched_from.startswith(shard_url(REV, "00000") + "#bytes=")  # provenance names the mirror host
    assert gone.source_url == "https://www.flickr.com/photos/111@N01/9001"  # original page kept
    landed = cfg.images_dir / "setting" / "streets" / gone.filename
    assert landed.read_bytes() == images["9001"]
    assert rows["9002"].remote_url == _flickr("9002")  # alive on Flickr: no mirror involved
    assert rows["9002"].fetched_from is None
    assert rows["9003"].remote_url == _flickr("9003")
    assert rows["9003"].fetched_from.startswith(shard_url(REV, "00001"))
    assert rows["9004"].status == "failed"

    # A rerun finds the mirrored rows under their Flickr URL: nothing is fetched again.
    hub.requests.clear()
    summary, _ = _land(cfg, hub, spec)
    assert summary.skipped == 3 and hub.hits(CDN) == 0

    # Resume doesn't depend on the mirror index: without it, the landed rows still match.
    MegalithMirror(cfg).db_path.unlink()
    hub.requests.clear()
    summary, rows = _land(cfg, hub, spec)
    assert summary.skipped == 3 and hub.hits(CDN) == 0
    assert len(rows) == 4  # no extra pending rows


def test_prefer_mirror_skips_flickr(mirror_env, images):
    cfg, hub = mirror_env
    with _net(cfg, hub) as net:
        MegalithMirror(cfg).build(net)
    hub.flickr.update({_flickr(p): images[p] for p in images})
    hub.requests.clear()
    spec = SliceSpec(archive="megalith", subject="streets", prefer_mirror=True, limit=10)

    summary, rows = _land(cfg, hub, spec)
    assert summary.complete == 4
    assert all(rows[p].fetched_from.startswith("https://huggingface.co/") for p in ("9001", "9002", "9003"))
    assert all(rows[p].remote_url == _flickr(p) for p in images)  # full-res primary always kept
    assert rows["9004"].fetched_from is None  # not mirrored: Flickr is the only copy
    flickr_hits = {url for _m, url, _r in hub.requests if "staticflickr" in url}
    assert flickr_hits == {_flickr("9004")}


def test_mirror_is_not_tried_when_flickr_fails_for_other_reasons(mirror_env):
    cfg, hub = mirror_env
    with _net(cfg, hub) as net:
        MegalithMirror(cfg).build(net, shards=["00000"])
    hub.flickr[_flickr("9001")] = 500  # transient, retried, then a plain failure: not "gone"
    hub.requests.clear()
    spec = SliceSpec(archive="megalith", subject="streets", query={"ids": ["9001"]})
    summary, rows = _land(cfg, hub, spec)
    assert summary.failed == 1 and hub.hits(CDN) == 0


def test_cli_parses_shard_ranges_and_reports_mirror_status(mirror_env, monkeypatch):
    from typer.testing import CliRunner

    from argus_quarry.cli import _parse_shards, app

    assert _parse_shards("0-2, 958") == ["00000", "00001", "00002", "00958"]
    cfg, hub = mirror_env
    with _net(cfg, hub) as net:
        MegalithMirror(cfg).build(net, shards=["00000"])
    monkeypatch.setenv("QUARRY_HOME", str(cfg.home))
    monkeypatch.setenv("QUARRY_MEGALITH_DIR", str(cfg.archive_dirs["megalith"]))
    out = CliRunner().invoke(app, ["archives", "status"]).output
    assert "2 members" in out and "1/2 shards scanned" in out and REV[:10] in out


def test_ext_for_never_raises_on_a_malformed_range():
    from argus_quarry.ingest import _ext_for

    # An empty span (size 0) is refused by the download, not by naming its temp file.
    assert _ext_for("https://example.org/00001.tar#bytes=10-9") == ".jpg"
    assert _ext_for("https://example.org/a.png?x=1#bytes=0-9") == ".png"
