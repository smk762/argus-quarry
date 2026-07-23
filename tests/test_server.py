"""Tests for the read-only provenance server (`argus-quarry serve`, DESIGN.md section 9)."""

from __future__ import annotations

import io
import shutil
import sqlite3

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
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "service": "argus-quarry",
        "version": body["version"],
        "quarry_home": str(config.home.resolve()),
    }
    assert isinstance(body["version"], str) and body["version"]


def test_ready_reports_a_serveable_pool(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "service": "argus-quarry", "database": "ok"}


def test_health_is_live_even_with_no_database(tmp_path):
    """Liveness must not depend on the pool: an empty QUARRY_HOME is still live (issue #10)."""
    home = tmp_path / "empty"
    client = TestClient(create_app(home=home), raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    # ...but readiness reflects that there is nothing to serve, and nothing was created.
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "unavailable",
        "service": "argus-quarry",
        "database": "provenance database unavailable",  # generic reason, no path leaked
    }
    assert not home.exists()


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


def _pool_fingerprint(home) -> dict[str, tuple]:
    """Every path under the pool with its size+mtime, to prove nothing was touched."""
    return {
        str(p.relative_to(home)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(home.rglob("*"))
        if p.is_file()
    }


def test_serves_a_read_only_quarry_home(config, seeded, read_only_dir):
    """Issue #5: a `:ro` QUARRY_HOME must still answer every data route."""
    read_only_dir(config.home)
    before = _pool_fingerprint(config.home)
    client = TestClient(create_app(home=config.home))

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/stats").json()["photographs"] == 4
    assert len(client.get("/subjects").json()["subjects"]) == 2
    assert client.get("/photos").json()["total"] == 3
    assert client.get("/photos/{}".format(seeded["with_file"])).status_code == 200
    assert client.get("/thumb", params={"id": seeded["with_file"], "size": 64}).status_code == 200
    # Not one byte of the pool changed, and no sidecar appeared, to answer them.
    assert _pool_fingerprint(config.home) == before


def test_serving_never_opens_the_pool_read_write(config, seeded):
    """The read path must never migrate or create, even where it *could* write."""
    opened: list[str] = []
    real_init = ProvenanceStore.__init__

    def _spy(self, db_path, **kw):
        real_init(self, db_path, **kw)
        opened.append(self.open_mode)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ProvenanceStore, "__init__", _spy)
        client = TestClient(create_app(home=config.home))  # writable home
        assert client.get("/stats").status_code == 200
        assert client.get("/photos").status_code == 200

    assert opened, "no store was opened"
    assert "read_write" not in opened, f"read path opened read-write: {opened}"


def test_writable_home_without_a_db_is_unavailable_and_creates_nothing(tmp_path):
    """A typo'd QUARRY_HOME must 503, not silently materialise an empty pool."""
    home = tmp_path / "typo"
    resp = TestClient(create_app(home=home), raise_server_exceptions=False).get("/stats")
    assert resp.status_code == 503
    assert not home.exists()


def test_read_only_home_without_a_db_is_unavailable(tmp_path, read_only_dir):
    home = tmp_path / "empty"
    (home / "metadata").mkdir(parents=True)
    read_only_dir(home)
    resp = TestClient(create_app(home=home), raise_server_exceptions=False).get("/stats")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "provenance database unavailable"  # no server paths leaked


def test_read_only_home_with_a_hot_wal_is_unavailable_not_stale(config, tmp_path, read_only_dir):
    """`immutable=1` cannot see a WAL, so serving stale counts is worse than 503."""
    with ProvenanceStore(config.db_path) as store:
        store.upsert_subject(Subject(name="Albert Einstein"))

    # A commit left only in the -wal, as a SIGKILLed fetch or a live snapshot would.
    conn = sqlite3.connect(config.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO subjects (name, category) VALUES ('Marie Curie', 'identity')")
    conn.commit()
    home = tmp_path / "snapshot"
    (home / "metadata").mkdir(parents=True)
    for name in ("portraits.sqlite", "portraits.sqlite-wal"):  # note: no -shm
        shutil.copy2(config.db_path.parent / name, home / "metadata" / name)
    conn.close()

    assert (home / "metadata" / "portraits.sqlite-wal").stat().st_size > 0
    read_only_dir(home)
    config = config.model_copy(update={"home": home})

    client = TestClient(create_app(home=config.home), raise_server_exceptions=False)
    stats = client.get("/stats")
    assert stats.status_code == 503, f"served {stats.json()} past an unreadable WAL"
    assert client.get("/ready").status_code == 503  # readiness reflects the unreadable WAL too


def test_legacy_pool_served_read_only_is_unavailable_not_500(config, read_only_dir):
    """An unmigrated pool cannot be migrated read-only, so it must 503, not 500."""
    conn = sqlite3.connect(config.db_path)
    conn.executescript(
        "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE photographs (id INTEGER PRIMARY KEY, person_id INTEGER, remote_url TEXT);"
        "INSERT INTO people (name) VALUES ('Albert Einstein');"
    )
    conn.commit()
    conn.close()
    read_only_dir(config.home)

    client = TestClient(create_app(home=config.home), raise_server_exceptions=False)
    for route in ("/stats", "/subjects", "/photos"):
        assert client.get(route).status_code == 503, f"{route} did not report unavailable"
    assert client.get("/health").status_code == 200  # process is live...
    assert client.get("/ready").status_code == 503  # ...but not ready to serve this pool
