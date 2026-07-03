from __future__ import annotations

from conftest import FakeNet, make_image_bytes, make_record

from argus_quarry.export import export_tree
from argus_quarry.ingest import IngestEngine
from argus_quarry.store import ProvenanceStore


def _land_two(config):
    net = FakeNet(
        assets={
            "https://img/cc0.jpg": make_image_bytes(seed=1),
            "https://img/pd.jpg": make_image_bytes(seed=2),
            "https://img/dress.jpg": make_image_bytes(seed=3),
        }
    )
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        engine.ingest_record(make_record("https://img/cc0.jpg", licence="CC0"))
        engine.ingest_record(make_record("https://img/pd.jpg", licence="Public domain"))
        engine.ingest_record(
            make_record("https://img/dress.jpg", subject="Red_dress", category="wardrobe", licence="CC0")
        )


def test_export_symlink_publishes_category_tree(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="symlink")
    assert result.published == 3
    links = list((dest / "identity" / "Albert_Einstein").glob("*"))
    assert len(links) == 2
    assert all(p.is_symlink() and p.resolve().exists() for p in links)
    # non-identity subject lands under its category subfolder
    assert (dest / "wardrobe" / "Red_dress").is_dir()
    assert len(list((dest / "wardrobe" / "Red_dress").glob("*"))) == 1


def test_export_copy_and_licence_filter(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="copy", licences=["CC0"])
    # both CC0 images (one identity, one wardrobe) publish; the PD one is filtered out
    assert result.published == 2
    files = list((dest / "identity" / "Albert_Einstein").glob("*"))
    assert len(files) == 1
    assert not files[0].is_symlink()  # real copy


def test_export_category_filter(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="symlink", category="wardrobe")
    assert result.published == 1
    assert not (dest / "identity").exists()
    assert (dest / "wardrobe" / "Red_dress").is_dir()
