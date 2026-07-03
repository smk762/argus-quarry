"""Tests for the read-only provenance server (`argus-quarry serve`, DESIGN.md section 9)."""

from __future__ import annotations

import io

import pytest

pytest.importorskip("fastapi", reason="server extra not installed")

from conftest import make_image_bytes  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from argus_quarry.models import Photograph, Subject  # noqa: E402
from argus_quarry.server import create_app  # noqa: E402
from argus_quarry.store import ProvenanceStore  # noqa: E402

PHOTO_FIELDS = {
    "id",
    "subject",
    "category",
    "title",
    "photographer",
    "year",
    "source",
    "source_url",
    "licence",
    "attribution",
    "width",
    "height",
    "file_size",
    "filename",
    "sha256",
    "remote_url",
    "status",
    "downloaded_at",
}


def _photo(subject_id: int, url: str, *, subject: str, category: str = "identity", **kw) -> Photograph:
    base = dict(
        subject_id=subject_id,
        subject=subject,
        category=category,
        source="commons",
        source_url="https://example.org/page",
        licence="PD",
        remote_url=url,
        status="pending",
    )
    base.update(kw)
    return Photograph(**base)


@pytest.fixture
def seeded(config):
    """A tmp QUARRY_HOME with two subjects, three landed photos and one pending.

    Photo ids returned as a dict; ``with_file`` has real JPEG bytes in the pool,
    ``no_file`` is complete in the DB but missing on disk.
    """
    with ProvenanceStore(config.db_path) as store:
        ein = store.upsert_subject(Subject(name="Albert Einstein"))
        kim = store.upsert_subject(Subject(name="Kimono", category="wardrobe"))

        with_file = store.insert_pending(_photo(ein, "https://img/a.jpg", subject="Albert_Einstein", licence="CC0"))
        data = make_image_bytes(size=256, seed=1)
        store.mark_complete(with_file, filename="a.jpg", sha256="aa", width=256, height=256, file_size=len(data))
        pool_dir = config.images_dir / "identity" / "Albert_Einstein"
        pool_dir.mkdir(parents=True, exist_ok=True)
        (pool_dir / "a.jpg").write_bytes(data)

        no_file = store.insert_pending(_photo(ein, "https://img/b.jpg", subject="Albert_Einstein", licence="PD"))
        store.mark_complete(no_file, filename="b.jpg", sha256="bb", width=64, height=64, file_size=100)

        wardrobe = store.insert_pending(
            _photo(kim, "https://img/c.jpg", subject="Kimono", category="wardrobe", licence="CC0", source="loc")
        )
        store.mark_complete(wardrobe, filename="c.jpg", sha256="cc", width=64, height=64, file_size=200)

        pending = store.insert_pending(_photo(ein, "https://img/d.jpg", subject="Albert_Einstein"))

    return {"with_file": with_file, "no_file": no_file, "wardrobe": wardrobe, "pending": pending}


@pytest.fixture
def client(config, seeded) -> TestClient:
    return TestClient(create_app(home=config.home))


def test_health(config, client):
    body = client.get("/health").json()
    assert body == {
        "status": "ok",
        "service": "argus-quarry",
        "version": body["version"],
        "quarry_home": str(config.home.resolve()),
    }
    assert isinstance(body["version"], str) and body["version"]


def test_health_respects_quarry_home_env(config, monkeypatch):
    monkeypatch.setenv("QUARRY_HOME", str(config.home))
    body = TestClient(create_app()).get("/health").json()
    assert body["quarry_home"] == str(config.home.resolve())


def test_stats(client):
    body = client.get("/stats").json()
    assert body["subjects"] == 2
    assert body["photographs"] == 4  # all rows, any status
    assert body["total_bytes"] > 0
    assert body["by_status"] == {"complete": 3, "pending": 1}
    assert body["by_category"] == {"identity": 2, "wardrobe": 1}  # complete only
    assert body["by_source"] == {"commons": 2, "loc": 1}
    assert body["by_licence"] == {"CC0": 2, "PD": 1}


def test_subjects_counts_landed_photos_only(client):
    body = client.get("/subjects").json()
    assert body == {
        "subjects": [
            {"folder": "Albert_Einstein", "category": "identity", "photo_count": 2},  # pending excluded
            {"folder": "Kimono", "category": "wardrobe", "photo_count": 1},
        ]
    }

    only_wardrobe = client.get("/subjects", params={"category": "wardrobe"}).json()
    assert [s["folder"] for s in only_wardrobe["subjects"]] == ["Kimono"]


def test_photos_default_status_and_shape(client, seeded):
    body = client.get("/photos").json()
    assert body["total"] == 3  # complete only by default
    assert body["offset"] == 0 and body["limit"] == 100
    assert len(body["photos"]) == 3
    photo = next(p for p in body["photos"] if p["id"] == seeded["with_file"])
    assert set(photo) == PHOTO_FIELDS
    assert photo["subject"] == "Albert_Einstein"
    assert photo["licence"] == "CC0"
    assert photo["sha256"] == "aa"
    assert photo["status"] == "complete"
    assert photo["downloaded_at"]


def test_photos_filters(client, seeded):
    def ids(**params):
        return [p["id"] for p in client.get("/photos", params=params).json()["photos"]]

    assert ids(subject="Kimono") == [seeded["wardrobe"]]
    assert ids(category="wardrobe") == [seeded["wardrobe"]]
    assert ids(licence="PD") == [seeded["no_file"]]
    assert ids(licence="CC0,PD") and len(ids(licence="CC0,PD")) == 3
    assert ids(source="loc") == [seeded["wardrobe"]]
    assert ids(status="pending") == [seeded["pending"]]
    assert len(ids(status="")) == 4  # empty status = all statuses


def test_photos_pagination(client):
    page = client.get("/photos", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3  # total matches the filters, not the page
    assert page["limit"] == 1 and page["offset"] == 1
    assert len(page["photos"]) == 1

    all_ids = [p["id"] for p in client.get("/photos").json()["photos"]]
    paged = [client.get("/photos", params={"limit": 1, "offset": i}).json()["photos"][0]["id"] for i in range(3)]
    assert paged == all_ids

    assert client.get("/photos", params={"limit": 501}).status_code == 422  # over PAGE_MAX
    assert client.get("/photos", params={"offset": -1}).status_code == 422


def test_photo_by_id(client, seeded):
    resp = client.get(f"/photos/{seeded['wardrobe']}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == PHOTO_FIELDS
    assert body["subject"] == "Kimono" and body["category"] == "wardrobe"

    assert client.get("/photos/999999").status_code == 404


def test_thumb_happy_path(client, seeded):
    resp = client.get("/thumb", params={"id": seeded["with_file"], "size": 64})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(resp.content)) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= 64


def test_thumb_caps_size(client, seeded):
    resp = client.get("/thumb", params={"id": seeded["with_file"], "size": 4096})
    assert resp.status_code == 200
    with Image.open(io.BytesIO(resp.content)) as img:
        assert max(img.size) <= 1024


def test_thumb_404(client, seeded):
    assert client.get("/thumb", params={"id": 999999}).status_code == 404  # unknown id
    assert client.get("/thumb", params={"id": seeded["no_file"]}).status_code == 404  # file missing on disk
    assert client.get("/thumb", params={"id": seeded["pending"]}).status_code == 404  # never landed


def test_no_mutation_endpoints(client):
    routes = {getattr(r, "path", None): getattr(r, "methods", set()) for r in client.app.routes}
    for path, methods in routes.items():
        if path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"} or methods is None:
            continue
        assert methods <= {"GET", "HEAD"}, f"{path} exposes {methods}"
