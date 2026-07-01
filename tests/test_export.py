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
        }
    )
    with ProvenanceStore(config.db_path) as store:
        engine = IngestEngine(config, store, net)
        engine.ingest_record(make_record("https://img/cc0.jpg", licence="CC0"))
        engine.ingest_record(make_record("https://img/pd.jpg", licence="Public domain"))


def test_export_symlink_publishes_all(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="symlink")
    assert result.published == 2
    links = list((dest / "Albert_Einstein").glob("*"))
    assert len(links) == 2
    assert all(p.is_symlink() and p.resolve().exists() for p in links)


def test_export_copy_and_licence_filter(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="copy", licences=["CC0"])
    assert result.published == 1
    files = list((dest / "Albert_Einstein").glob("*"))
    assert len(files) == 1
    assert not files[0].is_symlink()  # real copy
