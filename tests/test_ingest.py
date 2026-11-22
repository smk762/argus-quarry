from __future__ import annotations

import pytest
from conftest import FakeNet, make_exif_jpeg, make_image_bytes, make_record

from argus_quarry.ingest import BudgetReached, IngestEngine, fetch
from argus_quarry.store import ProvenanceStore


def test_ingest_lands_and_is_idempotent(config):
    url = "https://img/einstein.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=1)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        out = engine.ingest_record(make_record(url))
        assert out.status == "complete"
        landed = config.images_dir / "identity" / "Albert_Einstein" / out.filename
        assert landed.exists()
        assert store.total_bytes() == out.file_size

        # Rerun: same remote_url already complete -> skipped, no second download.
        out2 = engine.ingest_record(make_record(url))
        assert out2.status == "skipped"
        assert net.downloads == [url]


def test_non_jpeg_formats_land_under_their_real_suffix(config):
    """A TIFF/GIF/WebP is re-encoded to PNG, never landed with TIFF bytes under a .jpg name."""
    from PIL import Image

    urls = {
        "https://img/a.tiff": make_image_bytes(seed=11, fmt="TIFF"),
        "https://img/b.gif": make_image_bytes(seed=12, fmt="GIF"),
        "https://img/c.webp": make_image_bytes(seed=13, fmt="WEBP"),
        "https://img/d.jpeg": make_image_bytes(seed=14),
        "https://img/e": make_image_bytes(seed=15, fmt="PNG"),  # no suffix in the URL
    }
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, FakeNet(assets=urls))
        landed = {}
        for url in urls:
            out = engine.ingest_record(make_record(url))
            assert out.status == "complete", out
            landed[url] = config.images_dir / "identity" / "Albert_Einstein" / out.filename
    for url, path in landed.items():
        with Image.open(path) as im:
            assert {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}[path.suffix] == im.format, (url, path)
    assert landed["https://img/a.tiff"].suffix == ".png" and landed["https://img/d.jpeg"].suffix == ".jpeg"
    assert landed["https://img/e"].suffix == ".png"


def test_retried_row_lands_where_its_db_row_says(config):
    """A failed row reused by a later record keeps its category/subject, so the file is findable."""
    url = "https://img/retry.jpg"
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, FakeNet(assets={}))  # first attempt fails: no asset
        assert engine.ingest_record(make_record(url, subject="First", category="identity")).status == "failed"
        engine.net = FakeNet(assets={url: make_image_bytes(seed=21)})
        out = engine.ingest_record(make_record(url, subject="Second", category="setting"))
        assert out.status == "complete"
        row = store.get_photograph(out.photo_id)
        assert (config.images_dir / row.category / row.subject / row.filename).is_file()


def test_short_hash_filename_collision_does_not_overwrite(config, monkeypatch):
    import argus_quarry.ingest as ingest

    real = ingest._sha256_file
    monkeypatch.setattr(ingest, "_sha256_file", lambda p: "deadbeef" + real(p)[8:])  # same 8-char prefix
    urls = {"https://img/x1.jpg": make_image_bytes(seed=31), "https://img/x2.jpg": make_image_bytes(seed=32)}
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, FakeNet(assets=urls))
        outs = [engine.ingest_record(make_record(u)) for u in urls]
    assert [o.status for o in outs] == ["complete", "complete"]
    assert outs[0].filename != outs[1].filename
    assert all((config.images_dir / "identity" / "Albert_Einstein" / o.filename).is_file() for o in outs)


def test_exact_duplicate_bytes_are_deduped(config):
    data = make_image_bytes(seed=7)
    a, b = "https://img/a.jpg", "https://img/b.jpg"
    net = FakeNet(assets={a: data, b: data})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        assert engine.ingest_record(make_record(a)).status == "complete"
        dup = engine.ingest_record(make_record(b))
        assert dup.status == "duplicate"
        # Only one file physically landed.
        files = list((config.images_dir / "identity" / "Albert_Einstein").glob("*"))
        assert len(files) == 1


def test_a_matching_phash_never_dedups(config, monkeypatch):
    """phash is informational only: different bytes with the same phash both land (near-dup is curator's)."""
    import argus_quarry.ingest as ingest

    monkeypatch.setattr(ingest, "_compute_phash", lambda _im: "ffffffffffffffff")
    a, b = "https://img/a.jpg", "https://img/b.jpg"
    net = FakeNet(assets={a: make_image_bytes(seed=1), b: make_image_bytes(seed=2)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        assert engine.ingest_record(make_record(a)).status == "complete"
        assert engine.ingest_record(make_record(b)).status == "complete"
        rows = list(store.iter_photographs(status="complete"))
    assert len(rows) == 2 and {r.phash for r in rows} == {"ffffffffffffffff"}


def test_non_identity_lands_under_category_subfolder(config):
    url = "https://img/red_dress.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=11)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        out = engine.ingest_record(make_record(url, subject="Red_dress", category="wardrobe"))
        assert out.status == "complete"
        landed = config.images_dir / "wardrobe" / "Red_dress" / out.filename
        assert landed.exists()
        row = store.get_by_remote_url(url)
        assert row.category == "wardrobe"
        assert row.subject == "Red_dress"


def test_non_free_licence_is_quarantined(config):
    url = "https://img/copyrighted.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=2)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        out = engine.ingest_record(make_record(url, licence="CC BY-SA 4.0"))
        assert out.status == "quarantined"
        assert net.downloads == []  # never fetched
        row = store.get_by_remote_url(url)
        assert row.status == "quarantined"


def test_megapixel_cap_downscales(config):
    config.max_megapixels = 0.05  # force a downscale of a 256x256 (0.065 MP) image
    url = "https://img/big.jpg"
    net = FakeNet(assets={url: make_image_bytes(size=256, seed=3)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        out = engine.ingest_record(make_record(url))
        assert out.status == "complete"
        row = store.get_by_sha256(store.get_by_remote_url(url).sha256)
        assert (row.width * row.height) / 1_000_000 <= config.max_megapixels + 1e-6


def test_budget_stops_run_cleanly(config):
    data = make_image_bytes(seed=4)
    config.max_total_bytes = len(data)  # room for ~one file
    a, b = "https://img/a.jpg", "https://img/b.jpg"
    net = FakeNet(assets={a: data, b: make_image_bytes(seed=5)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        summary = fetch(engine, [make_record(a), make_record(b, year=1930)])
        assert summary.budget_reached is True
        assert summary.complete == 1
        # The second candidate is left resumable.
        assert store.get_by_remote_url(b).status == "pending"


def test_budget_reached_raises_when_pool_full(config):
    config.max_total_bytes = 10
    url = "https://img/a.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=6)})
    with ProvenanceStore(config.db_path) as store:
        # Seed the pool over the cap.
        engine = IngestEngine(config, store, net)
        engine._pool_bytes = 100
        try:
            engine.ingest_record(make_record(url))
            raise AssertionError("expected BudgetReached")
        except BudgetReached:
            pass


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "../../escaped"),
        ("subject", "AC/DC"),
        ("subject", ".."),
        ("category", "../outside"),
        ("category", "a\\b"),
        ("source", "../../evil"),
    ],
)
def test_unsafe_path_component_is_refused_before_anything_lands(config, field, value):
    """A category/subject/source that isn't one path component never writes outside the pool (or anywhere)."""
    url = "https://img/unsafe.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=41)})
    with ProvenanceStore(config.db_path) as store:
        out = IngestEngine(config, store, net).ingest_record(make_record(url, **{field: value}))
        assert out.status == "failed" and out.reason.startswith(f"unsafe_{field}=")
        assert store.get_by_remote_url(url) is None  # no DB row either
    assert net.downloads == []
    home = config.images_dir.parent
    landed = [p for p in home.parent.rglob("*") if p.is_file() and p.suffix in {".jpg", ".png"}]
    assert landed == []


@pytest.mark.parametrize("error", ["gone", "size_cap"])
def test_a_dead_download_leaves_no_partial_in_the_cache(config, error):
    from argus_quarry.net import RemoteGone, SizeCapExceeded

    class PartialThenFail(FakeNet):
        def download(self, url, dest, *, source="default", max_bytes=None):
            dest.with_suffix(dest.suffix + ".part").write_bytes(b"partial")
            dest.write_bytes(b"stale temp")
            raise RemoteGone(url, 404) if error == "gone" else SizeCapExceeded(10)

    with ProvenanceStore(config.db_path) as store:
        out = IngestEngine(config, store, PartialThenFail()).ingest_record(make_record("https://img/x.jpg"))
    assert out.status == ("failed" if error == "gone" else "oversize")
    assert list(config.cache_dir.iterdir()) == []


def _real_net(config, assets: dict[str, bytes], hits: list[str]):
    """A real NetClient over a mock transport, so max_bytes is enforced by the actual stream."""
    import httpx

    from argus_quarry.net import NetClient

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, content=assets[str(request.url)])

    return NetClient(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_download_over_max_file_bytes_is_aborted_and_marked_oversize(config):
    url = "https://img/huge.jpg"
    data = make_image_bytes(256, seed=5)
    config.max_file_bytes = len(data) // 2
    hits: list[str] = []
    with ProvenanceStore(config.db_path) as store:
        out = IngestEngine(config, store, _real_net(config, {url: data}, hits)).ingest_record(make_record(url))
        row = store.get_by_remote_url(url)
        assert store.total_bytes() == 0  # nothing landed, nothing counted against the budget
    assert out.status == "oversize" and "max_file_bytes" in out.reason
    assert row.status == "oversize" and row.file_size > config.max_file_bytes  # bytes seen, a lower bound
    assert not list(config.images_dir.rglob("*.jpg")) and list(config.cache_dir.iterdir()) == []


def test_an_oversize_row_is_skipped_until_the_cap_is_raised(config):
    url = "https://img/huge.jpg"
    data = make_image_bytes(256, seed=5)
    config.max_file_bytes = len(data) // 2
    hits: list[str] = []
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, _real_net(config, {url: data}, hits))
        assert engine.ingest_record(make_record(url)).status == "oversize"
        again = engine.ingest_record(make_record(url))
        assert again.status == "skipped" and again.reason == "oversize" and len(hits) == 1  # not re-downloaded
        config.max_file_bytes = len(data) * 2
        assert engine.ingest_record(make_record(url)).status == "complete"


@pytest.mark.parametrize("cap", [0, 10**9])
def test_a_file_under_the_cap_or_with_no_cap_lands(config, cap):
    url = "https://img/a.jpg"
    config.max_file_bytes = cap
    with ProvenanceStore(config.db_path) as store:
        out = IngestEngine(config, store, _real_net(config, {url: make_image_bytes(seed=3)}, [])).ingest_record(
            make_record(url)
        )
    assert out.status == "complete"


def test_max_file_mb_from_env(monkeypatch, tmp_path):
    from argus_quarry.config import QuarryConfig

    monkeypatch.setenv("QUARRY_HOME", str(tmp_path))
    monkeypatch.setenv("QUARRY_MAX_FILE_MB", "2.5")
    assert QuarryConfig.from_env().max_file_bytes == int(2.5 * 1024 * 1024)
    monkeypatch.setenv("QUARRY_MAX_FILE_MB", "0")
    assert QuarryConfig.from_env().max_file_bytes == 0
    monkeypatch.delenv("QUARRY_MAX_FILE_MB")
    assert QuarryConfig.from_env().max_file_bytes == 100 * 1024 * 1024
    # Negative would abort every download while ingest read it as unlimited and retried forever.
    monkeypatch.setenv("QUARRY_MAX_FILE_MB", "-1")
    with pytest.raises(ValueError):
        QuarryConfig.from_env()


def test_an_unsafe_subject_on_a_record_reusing_a_row_is_still_refused(config):
    """A rerun record whose subject climbs out must not reach the pool filename, even when its row exists."""
    url = "https://img/reused.jpg"
    net = FakeNet(assets={url: make_image_bytes(seed=43)})
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        pid = engine._insert_pending(make_record(url), engine._upsert_subject(make_record(url)), "PD")
        store.set_status(pid, "failed")  # a retryable row with a safe subject
        out = engine.ingest_record(make_record(url, subject="../../escaped"))
        assert out.status == "failed" and out.reason.startswith("unsafe_subject=")
    home = config.images_dir.parent
    outside = [p for p in home.parent.rglob("*.jpg") if not p.resolve().is_relative_to(config.images_dir.resolve())]
    assert outside == []


def _land(config, data: bytes, url: str = "https://img/exif.jpg"):
    net = FakeNet(assets={url: data})
    with ProvenanceStore(config.db_path) as store:
        outcome = IngestEngine(config, store, net).ingest_record(make_record(url))
        assert outcome.status == "complete"
        return store.get_by_remote_url(url)


def test_exif_date_time_original_is_recorded_as_the_capture_date(config):
    row = _land(config, make_exif_jpeg(original="1998:07:14 09:30:05", offset="+02:00"))
    assert row.captured_at == "1998-07-14T09:30:05+02:00"
    assert row.captured_at_source == "exif"


def test_the_capture_date_survives_a_downscale(config):
    config.max_megapixels = 0.05  # the re-encode drops EXIF, so it must be read before
    row = _land(config, make_exif_jpeg(original="2004:01:02 03:04:05"))
    assert row.captured_at == "2004-01-02T03:04:05" and row.captured_at_source == "exif"


@pytest.mark.parametrize(
    "exif",
    [
        {},  # no EXIF date at all
        {"modified": "2020:01:01 00:00:00"},  # only the file-modified time: not a capture
        {"original": "0000:00:00 00:00:00"},  # an unset camera clock
        {"original": "    :  :     :  :  "},
        {"original": "2001:13:40 25:00:00"},  # not a date
        {"original": "1700:01:01 00:00:00"},  # before photography
        {"original": "2999:01:01 00:00:00"},  # the future
    ],
)
def test_no_trustworthy_exif_date_records_no_capture_date(config, exif):
    row = _land(config, make_exif_jpeg(**exif))
    assert row.captured_at is None and row.captured_at_source is None


def test_a_malformed_offset_keeps_the_date_without_it(config):
    row = _land(config, make_exif_jpeg(original="1985:06:01 12:00:00", offset="Z"))
    assert row.captured_at == "1985-06-01T12:00:00"


@pytest.mark.parametrize(
    ("make", "model", "medium"),
    [
        ("Canon", "Canon EOS 5D Mark II", "digital"),
        ("Apple", "iPhone 8", "digital"),
        ("Nikon", "Nikon SUPER COOLSCAN 9000 ED", "scan"),
        ("EPSON", "Perfection V800/V850", "scan"),
        ("NORITSU KOKI", "EZ Controller", "scan"),
    ],
)
def test_the_exif_device_is_recorded_with_its_capture_medium(config, make, model, medium):
    row = _land(config, make_exif_jpeg(make=make, model=model))
    assert (row.camera_make, row.camera_model, row.capture_medium) == (make, model, medium)


@pytest.mark.parametrize(
    ("exif", "make"),
    [
        ({}, None),  # no EXIF at all: many scans carry none
        ({"original": "1999:01:01 00:00:00"}, None),  # a date but no device
        ({"make": "Canon"}, "Canon"),  # a maker alone names no device
        ({"make": "  ", "model": "\x00\x00 "}, None),  # padding only
    ],
)
def test_a_file_naming_no_device_records_an_unknown_medium_never_digital(config, exif, make):
    row = _land(config, make_exif_jpeg(**exif))
    assert (row.camera_make, row.camera_model, row.capture_medium) == (make, None, None)


def test_the_capture_device_survives_a_downscale(config):
    config.max_megapixels = 0.05  # the re-encode drops EXIF, so it must be read before
    row = _land(config, make_exif_jpeg(make="HP", model="HP Scanjet G4050"))
    assert row.camera_model == "HP Scanjet G4050" and row.capture_medium == "scan"
