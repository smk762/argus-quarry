from __future__ import annotations

from conftest import FakeNet, make_image_bytes, make_record

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
