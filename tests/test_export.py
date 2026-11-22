from __future__ import annotations

import pytest
from conftest import FakeNet, make_exif_jpeg, make_image_bytes, make_record

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
    links = list((dest / "identity" / "Albert_Einstein").glob("*.jpg"))
    assert len(links) == 2
    assert all(p.is_symlink() and p.resolve().exists() for p in links)
    # non-identity subject lands under its category subfolder
    assert (dest / "wardrobe" / "Red_dress").is_dir()
    assert len(list((dest / "wardrobe" / "Red_dress").glob("*.jpg"))) == 1


def test_export_copy_and_licence_filter(config, tmp_path):
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="copy", licences=["CC0"])
    # both CC0 images (one identity, one wardrobe) publish; the PD one is filtered out
    assert result.published == 2
    files = list((dest / "identity" / "Albert_Einstein").glob("*.jpg"))
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


def test_export_writes_a_provenance_sidecar_per_file(config, tmp_path):
    import json

    from argus_cortex.provenance import Provenance

    from argus_quarry.export import PRODUCER, SIDECAR_SCHEMA

    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        export_tree(config, store, dest, mode="symlink", category="wardrobe")
        (row,) = store.iter_photographs(category="wardrobe")
    (img,) = [p for p in (dest / "wardrobe" / "Red_dress").iterdir() if p.suffix != ".json"]
    # <stem>.json: the name curator's --carry-sidecars carries on to its own export
    meta = json.loads(img.with_suffix(".json").read_text())
    assert meta["producer"] == PRODUCER and meta["schema"] == SIDECAR_SCHEMA
    assert meta["licence"] == "CC0" and meta["source_url"] == row.source_url
    assert meta["remote_url"] == "https://img/dress.jpg" and meta["sha256"] == row.sha256
    assert meta["attribution"] == "Someone"
    prov = Provenance.model_validate(meta["provenance"])  # the suite's shared shape, round-trips
    assert prov.kind == "stock" and prov.license == "CC0" and prov.source == row.source_url
    assert "likeness_consent" not in meta  # not a person category: no mark


def test_export_marks_a_person_photo_as_not_consented(config, tmp_path):
    import json

    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        export_tree(config, store, dest, mode="symlink", category="identity")
    sidecars = list((dest / "identity").rglob("*.json"))
    assert sidecars
    for sidecar in sidecars:
        meta = json.loads(sidecar.read_text())
        assert meta["likeness_consent"] is False  # explicit, never inferred from a missing field


def test_export_refuses_a_row_whose_path_climbs_out(config, tmp_path):
    import shutil

    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        (row,) = store.iter_photographs(category="wardrobe")
        store._conn.execute("UPDATE photographs SET category = '../../escape' WHERE id = ?", (row.id,))
        store._conn.commit()
        # Make the escaped source exist too, so only the path check can stop it.
        escaped_src = config.images_dir / "../../escape" / row.subject / row.filename
        escaped_src.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.images_dir / "wardrobe" / row.subject / row.filename, escaped_src)
        result = export_tree(config, store, dest, mode="copy", category="../../escape")
    assert result.published == 0 and result.skipped == 1
    assert not (dest / "../../escape").exists()


@pytest.mark.parametrize("where", ["inside", "around", "same"])
def test_export_refuses_a_dest_that_overlaps_the_pool(config, tmp_path, where):
    _land_two(config)
    dest = {"inside": config.home / "published", "around": config.home.parent, "same": config.home}[where]
    with ProvenanceStore(config.db_path) as store, pytest.raises(ValueError, match="overlaps"):
        export_tree(config, store, dest, mode="symlink")


def test_a_second_symlink_export_republishes_every_file(config, tmp_path):
    """Re-exporting over an earlier symlink tree must not mistake its links (into the pool) for an escape."""
    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        export_tree(config, store, dest, mode="symlink")
        again = export_tree(config, store, dest, mode="symlink")
    assert again.published == 3 and again.skipped == 0


def test_a_failed_re_export_keeps_the_earlier_file_and_its_sidecar(config, tmp_path, monkeypatch):
    """A re-export that fails mid-publish must not strip provenance from the file an earlier run published."""
    import json

    import argus_quarry.export as export_mod

    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        export_tree(config, store, dest, mode="copy", category="wardrobe")
        (img,) = [p for p in (dest / "wardrobe" / "Red_dress").iterdir() if p.suffix != ".json"]
        before = img.read_bytes()

        def broken_copy(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(export_mod.shutil, "copy2", broken_copy)
        again = export_tree(config, store, dest, mode="copy", category="wardrobe")
    assert again.published == 0 and again.skipped == 1
    assert img.read_bytes() == before  # the earlier publication is still in place...
    assert json.loads(img.with_suffix(".json").read_text())["licence"] == "CC0"  # ...with its provenance
    assert not [p for p in img.parent.iterdir() if p.name.startswith(".")]  # no staged leftovers


def test_a_fresh_export_that_fails_leaves_no_orphan_sidecar(config, tmp_path, monkeypatch):
    import argus_quarry.export as export_mod

    _land_two(config)
    dest = tmp_path / "published"

    def broken_copy(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(export_mod.shutil, "copy2", broken_copy)
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode="copy", category="wardrobe")
    assert result.published == 0 and result.skipped == 1
    assert not list((dest / "wardrobe" / "Red_dress").iterdir())


def test_same_stem_files_in_one_subject_never_share_a_sidecar(config, tmp_path):
    """a.jpg and a.png would both own a.json: the second is skipped, never overwrites the first's licence."""
    import json

    _land_two(config)
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        first, second = store.iter_photographs(category="identity")
        subject_dir = config.images_dir / "identity" / second.subject
        clash = f"{first.filename.rsplit('.', 1)[0]}.png"
        (subject_dir / second.filename).rename(subject_dir / clash)
        store._conn.execute("UPDATE photographs SET filename = ? WHERE id = ?", (clash, second.id))
        store._conn.commit()
        result = export_tree(config, store, dest, mode="copy", category="identity")
        # a later export that only matches the clashing row must not overwrite the earlier sidecar either
        later = export_tree(config, store, dest, mode="copy", category="identity", licences=[second.licence])
    assert result.published == 1 and result.skipped == 1
    assert later.published == 0 and later.skipped == 1
    published = dest / "identity" / first.subject
    assert not (published / clash).exists()
    meta = json.loads((published / first.filename).with_suffix(".json").read_text())
    assert meta["sha256"] == first.sha256


def test_the_sidecar_carries_the_capture_date_and_its_source(config, tmp_path):
    import json

    url = "https://img/dated.jpg"
    net = FakeNet(assets={url: make_exif_jpeg(original="1975:03:09 10:11:12")})
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        IngestEngine(config, store, net).ingest_record(make_record(url, licence="CC0"))
        export_tree(config, store, dest, mode="copy")
    (sidecar,) = dest.rglob("*.json")
    meta = json.loads(sidecar.read_text())
    assert meta["captured_at"] == "1975-03-09T10:11:12" and meta["captured_at_source"] == "exif"


def test_the_sidecar_carries_the_capture_device(config, tmp_path):
    import json

    url = "https://img/scanned.jpg"
    net = FakeNet(assets={url: make_exif_jpeg(make="Nikon", model="Nikon SUPER COOLSCAN 5000 ED")})
    dest = tmp_path / "published"
    with ProvenanceStore(config.db_path) as store:
        IngestEngine(config, store, net).ingest_record(make_record(url, licence="CC0"))
        export_tree(config, store, dest, mode="copy")
    (sidecar,) = dest.rglob("*.json")
    meta = json.loads(sidecar.read_text())
    assert meta["camera_make"] == "Nikon" and meta["camera_model"] == "Nikon SUPER COOLSCAN 5000 ED"
    assert meta["capture_medium"] == "scan"
