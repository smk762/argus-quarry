"""Tests for the PD12M / Megalith-10m metadata archives: index, search, slice, fetch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="archives extra not installed")

from conftest import FakeNet, make_image_bytes  # noqa: E402

from argus_quarry.archives import ArchiveQuery, ArchiveUnavailable, SliceSpec, get_archive  # noqa: E402
from argus_quarry.config import QuarryConfig  # noqa: E402
from argus_quarry.models import normalise_licence  # noqa: E402

PD_ZERO = "https://creativecommons.org/publicdomain/zero/1.0/"
PD_MARK = "https://creativecommons.org/publicdomain/mark/1.0/"


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


@pytest.fixture
def pd12m_dir(tmp_path: Path) -> Path:
    root = tmp_path / "pd12m"
    rows = [
        # id, url, caption, width, height, mime_type, hash, license, source
        (
            "a1",
            "https://s3.example/a1.jpeg",
            "A black and white photo of a harbour with fishing boats at dawn, 1923.",
            2400,
            1600,
            "image/jpeg",
            "h1",
            PD_ZERO,
            "Rijksmuseum",
        ),
        (
            "a2",
            "https://s3.example/a2.jpeg",
            "A painting of a woman in a wool coat with a red scarf",
            1200,
            1800,
            "image/jpeg",
            "h2",
            PD_MARK,
            "Metropolitan Museum of Art",
        ),
        (
            "a3",
            "https://s3.example/a3.png",
            "A photo of a green beetle on a leaf. Taken in the 1990s",
            600,
            600,
            "image/png",
            "h3",
            PD_ZERO,
            "iNaturalist",
        ),
        (
            "a4",
            "https://s3.example/a4.jpeg",
            "Harbour lighthouse in fog",
            3000,
            2000,
            "image/jpeg",
            "h4",
            PD_MARK,
            "Rijksmuseum",
        ),
    ]
    cols = ("id", "url", "caption", "width", "height", "mime_type", "hash", "license", "source")
    _write_parquet(root / "metadata" / "pd12m.000.parquet", [dict(zip(cols, r, strict=True)) for r in rows])
    return root


@pytest.fixture
def megalith_dir(tmp_path: Path) -> Path:
    root = tmp_path / "megalith"
    photos = [("111@N01", "9001"), ("111@N01", "9002"), ("222@N02", "9003")]
    links = [
        {
            "url": f"https://farm1.staticflickr.com/1/{pid}_abc.jpg",
            "url_highres": f"https://farm1.staticflickr.com/1/{pid}_abc_b.jpg",
            "url_source": f"https://www.flickr.com/photos/{owner}/{pid}",
        }
        for owner, pid in photos
    ]
    _write_parquet(root / "links" / "data" / "megalith-00001-of-00010.parquet", links)
    caps = root / "captions" / "metadata.csv"
    caps.parent.mkdir(parents=True)
    # Same shape as the real file: quoted captions that start with a newline.
    caps.write_text(
        "url_source,caption\n"
        'https://www.flickr.com/photos/111@N01/9001,"\nTwo children crouching by the stairs.\n"\n'
        'https://www.flickr.com/photos/222@N02/9003,"\nA red tram on a ""busy"" street in 1975.\n"\n'
    )
    _write_parquet(
        root / "drawthings" / "00000.parquet",
        [
            {"url": links[0]["url_highres"], "status": "success", "width": 1024, "height": 683},
            {"url": links[2]["url_highres"], "status": "success", "width": 768, "height": 1024},
            {"url": links[1]["url_highres"], "status": "failed_to_download", "width": None, "height": None},
        ],
    )
    return root


@pytest.fixture
def archives_config(tmp_path: Path, pd12m_dir: Path, megalith_dir: Path) -> QuarryConfig:
    cfg = QuarryConfig(home=tmp_path / "quarry", archive_dirs={"pd12m": pd12m_dir, "megalith": megalith_dir})
    cfg.ensure_dirs()
    for name in ("pd12m", "megalith"):
        get_archive(name, cfg).build_index()
    return cfg


def ids(page: dict) -> set[str]:
    return {i["id"] for i in page["items"]}


def test_unindexed_archive_says_how_to_fix(tmp_path: Path, pd12m_dir: Path):
    cfg = QuarryConfig(home=tmp_path / "q", archive_dirs={"pd12m": pd12m_dir})
    archive = get_archive("pd12m", cfg)
    assert archive.status()["indexed"] is False
    with pytest.raises(ArchiveUnavailable, match="archives index pd12m"):
        archive.search(ArchiveQuery())


def test_unconfigured_archive_cannot_index(tmp_path: Path):
    with pytest.raises(ArchiveUnavailable, match="QUARRY_MEGALITH_DIR"):
        get_archive("megalith", QuarryConfig(home=tmp_path)).build_index()


def test_pd12m_index_and_filters(archives_config):
    pd = get_archive("pd12m", archives_config)
    assert pd.status()["items"] == 4
    assert pd.search(ArchiveQuery())["total"] == 4
    assert ids(pd.search(ArchiveQuery(q="harbour"))) == {"a1", "a4"}
    assert ids(pd.search(ArchiveQuery(q="harbour fog"))) == {"a4"}  # every word must match
    assert ids(pd.search(ArchiveQuery(collections=["iNaturalist"]))) == {"a3"}
    assert ids(pd.search(ArchiveQuery(licences=["CC0"]))) == {"a1", "a3"}
    assert ids(pd.search(ArchiveQuery(mimes=["image/png"]))) == {"a3"}
    assert ids(pd.search(ArchiveQuery(min_short_side=1500))) == {"a1", "a4"}
    assert ids(pd.search(ArchiveQuery(orientation="portrait"))) == {"a2"}
    assert ids(pd.search(ArchiveQuery(orientation="square"))) == {"a3"}
    assert ids(pd.search(ArchiveQuery(ids=["a2", "a3"], licences=["PDM"]))) == {"a2"}
    largest = pd.search(ArchiveQuery(sort="largest"))["items"]
    assert [i["id"] for i in largest][:2] == ["a4", "a1"]
    # shuffle order is fixed by the index, so paging is stable
    assert pd.search(ArchiveQuery(), limit=2)["items"] == pd.search(ArchiveQuery(), limit=2)["items"]


def test_caption_exclusions_and_phrases(archives_config):
    pd = get_archive("pd12m", archives_config)
    assert ids(pd.search(ArchiveQuery(q="harbour", exclude="fog"))) == {"a1"}
    assert ids(pd.search(ArchiveQuery(q="harbour -fog"))) == {"a1"}  # inline negative
    assert ids(pd.search(ArchiveQuery(exclude="harbour beetle"))) == {"a2"}  # every exclude term is negative
    assert ids(pd.search(ArchiveQuery(q='"fishing boats"'))) == {"a1"}  # phrase kept whole
    assert ids(pd.search(ArchiveQuery(q='"boats fishing"'))) == set()
    assert ids(pd.search(ArchiveQuery(q='-"wool coat"'))) == {"a1", "a3", "a4"}
    # unbalanced quotes fall back to whitespace splitting instead of raising
    assert ids(pd.search(ArchiveQuery(q='"unbalanced harbour'))) == set()
    # LIKE wildcards in a term are literal, not patterns ("1923" is in a1's caption)
    assert ids(pd.search(ArchiveQuery(q="1923"))) == {"a1"}
    assert ids(pd.search(ArchiveQuery(q="19_3"))) == set()
    assert ids(pd.search(ArchiveQuery(q="19%3"))) == set()
    assert ids(pd.search(ArchiveQuery(q="harbour -19_3"))) == {"a1", "a4"}
    # uncaptioned items survive an exclusion (they do not mention the term)
    mg = get_archive("megalith", archives_config)
    assert ids(mg.search(ArchiveQuery(exclude="tram"))) == {"9001", "9002"}


def test_caption_terms_parsing():
    from argus_quarry.archives.base import caption_terms

    assert caption_terms('red "wool coat" -painting -"black and white"') == (
        ["red", "wool coat"],
        ["painting", "black and white"],
    )
    assert caption_terms(None) == ([], [])
    assert caption_terms('"oops harbor') == (['"oops', "harbor"], [])
    assert caption_terms("- lone dash") == (["lone", "dash"], [])
    # apostrophes are literal, not quotes
    assert caption_terms("children's book -men's") == (["children's", "book"], ["men's"])
    assert caption_terms("'single quoted'") == (["'single", "quoted'"], [])


def test_caption_derived_tone_medium_year(archives_config):
    pd = get_archive("pd12m", archives_config)
    by_id = {i["id"]: i for i in pd.search(ArchiveQuery())["items"]}
    assert {k: (v["tone"], v["medium"], v["year"]) for k, v in by_id.items()} == {
        "a1": ("bw", "photo", 1923),
        "a2": ("color", "painting", None),
        "a3": ("color", "photo", 1990),  # a decade counts as its first year
        "a4": (None, None, None),  # caption names neither: unknown, not guessed
    }
    assert ids(pd.search(ArchiveQuery(tone="bw"))) == {"a1"}
    assert ids(pd.search(ArchiveQuery(tone="color", mediums=["photo"]))) == {"a3"}
    assert ids(pd.search(ArchiveQuery(mediums=["painting", "drawing"]))) == {"a2"}
    assert ids(pd.search(ArchiveQuery(year_min=1900, year_max=1950))) == {"a1"}
    assert ids(pd.search(ArchiveQuery(year_min=1950))) == {"a3"}  # undated items never match a range
    f = pd.facets()
    assert {x["name"]: x["count"] for x in f["tones"]} == {"color": 2, "bw": 1}
    assert f["dated"] == 2 and f["year_range"] == [1923, 1990]

    mg = get_archive("megalith", archives_config)
    by_id = {i["id"]: i for i in mg.search(ArchiveQuery())["items"]}
    assert by_id["9002"]["medium"] == "photo"  # Megalith defaults to photo even without a caption
    assert (by_id["9003"]["tone"], by_id["9003"]["year"]) == ("color", 1975)


def test_index_from_older_schema_asks_for_rebuild(archives_config):
    pd = get_archive("pd12m", archives_config)
    con = duckdb.connect(str(pd.index_path))
    con.execute("DROP TABLE meta")
    con.close()
    status = pd.status()
    assert status["indexed"] is False and status["stale"] is True
    with pytest.raises(ArchiveUnavailable, match="older argus-quarry"):
        pd.search(ArchiveQuery())


def test_index_from_newer_schema_says_newer(archives_config):
    from argus_quarry.archives.base import INDEX_SCHEMA

    pd = get_archive("pd12m", archives_config)
    con = duckdb.connect(str(pd.index_path))
    con.execute("UPDATE meta SET schema_version = ?", [INDEX_SCHEMA + 1])
    con.close()
    with pytest.raises(ArchiveUnavailable, match="newer argus-quarry"):
        pd.search(ArchiveQuery())


def test_corrupt_index_is_unavailable_not_a_crash(archives_config, monkeypatch):
    pd = get_archive("pd12m", archives_config)
    pd.index_path.write_bytes(b"not a duckdb file" * 100)
    status = pd.status()
    assert status["indexed"] is False and status["stale"] is False  # unreadable is not "stale"
    with pytest.raises(ArchiveUnavailable, match="unreadable"):
        pd.search(ArchiveQuery())
    resp = _app(archives_config, monkeypatch).get("/archives/pd12m/search")
    assert resp.status_code == 503 and "archives index pd12m" in resp.json()["detail"]


def test_slice_spec_rejects_unsafe_categories_and_unknown_keys():
    from pydantic import ValidationError

    for bad in ("../../etc", "a/b", "..", "x\\y", "/abs"):
        with pytest.raises(ValidationError):
            SliceSpec(archive="pd12m", subject="s", category=bad)
    assert SliceSpec(archive="pd12m", subject="s", category=" Wardrobe ").category == "wardrobe"
    with pytest.raises(ValidationError, match="limt"):
        SliceSpec.model_validate({"archive": "pd12m", "subject": "s", "limt": 5})
    with pytest.raises(ValidationError, match="colections"):
        ArchiveQuery.model_validate({"colections": ["Rijksmuseum"]})
    with pytest.raises(ValidationError, match="unknown medium"):
        ArchiveQuery(mediums=["paintng"])
    assert ArchiveQuery(mediums=[" Photo "]).mediums == ["photo"]


def test_split_collections_keeps_comma_named_institutions():
    from argus_quarry.archives import split_collections

    assert split_collections("Natural History Museum, Berlin") == ["Natural History Museum, Berlin"]
    assert split_collections("Rijksmuseum,Natural History Museum, Berlin") == [
        "Rijksmuseum",
        "Natural History Museum, Berlin",
    ]
    assert split_collections(["a,b", "Visual Media, Belgium"]) == ["a", "b", "Visual Media, Belgium"]
    assert split_collections(None) == []


def test_unlicensed_pd12m_row_is_quarantined_not_fatal(tmp_path):
    from conftest import FakeNet

    from argus_quarry.ingest import IngestEngine, fetch
    from argus_quarry.store import ProvenanceStore

    cols = ("id", "url", "caption", "width", "height", "mime_type", "hash", "license", "source")
    rows = [
        ("n1", "https://s3.example/n1.jpeg", "x", 10, 10, "image/jpeg", "h", None, "Test"),
        ("n2", "https://s3.example/n2.jpeg", "x", 10, 10, "image/jpeg", "h", PD_ZERO, "Test"),
    ]
    root = tmp_path / "pd"
    _write_parquet(root / "metadata" / "x.parquet", [dict(zip(cols, r, strict=True)) for r in rows])
    cfg = QuarryConfig(home=tmp_path / "q", archive_dirs={"pd12m": root})
    cfg.ensure_dirs()
    pd = get_archive("pd12m", cfg)
    pd.build_index()
    records = list(pd.records(SliceSpec(archive="pd12m", subject="s")))
    assert len(records) == 2
    net = FakeNet(assets={"https://s3.example/n2.jpeg": make_image_bytes(32, seed=5)})
    with ProvenanceStore(cfg.db_path) as store:
        summary = fetch(IngestEngine(cfg, store, net), records)
    assert (summary.quarantined, summary.complete) == (1, 1)


def test_largest_sort_does_not_overflow_on_huge_dimensions(tmp_path):
    cols = ("id", "url", "caption", "width", "height", "mime_type", "hash", "license", "source")
    rows = [
        ("big", "https://s3.example/big.jpeg", "x", 50_000, 50_000, "image/jpeg", "h", PD_ZERO, "T"),
        ("small", "https://s3.example/small.jpeg", "x", 10, 10, "image/jpeg", "h", PD_ZERO, "T"),
    ]
    root = tmp_path / "pd"
    _write_parquet(root / "metadata" / "x.parquet", [dict(zip(cols, r, strict=True)) for r in rows])
    pd = get_archive("pd12m", QuarryConfig(home=tmp_path / "q", archive_dirs={"pd12m": root}))
    pd.build_index()
    assert [i["id"] for i in pd.search(ArchiveQuery(sort="largest"))["items"]] == ["big", "small"]


def test_pd12m_facets(archives_config):
    f = get_archive("pd12m", archives_config).facets()
    assert f["total"] == 4 and f["sized"] == 4 and f["n_collections"] == 3
    assert f["collections"][0] == {"name": "Rijksmuseum", "count": 2}
    assert {x["name"] for x in f["licences"]} == {"CC0", "PDM"}


def test_megalith_joins_captions_and_sizes(archives_config):
    mg = get_archive("megalith", archives_config)
    by_id = {i["id"]: i for i in mg.search(ArchiveQuery())["items"]}
    assert set(by_id) == {"9001", "9002", "9003"}
    assert by_id["9001"]["caption"] == "Two children crouching by the stairs."  # CSV newline trimmed
    assert by_id["9003"]["caption"] == 'A red tram on a "busy" street in 1975.'
    assert by_id["9002"]["caption"] is None and by_id["9002"]["width"] is None  # failed in drawthings
    assert (by_id["9001"]["width"], by_id["9001"]["height"]) == (1024, 683)
    assert by_id["9001"]["collection"] == "111@N01"
    assert by_id["9001"]["url"].endswith("_b.jpg") and by_id["9001"]["thumb_url"].endswith("_abc.jpg")
    assert ids(mg.search(ArchiveQuery(collections=["111@N01"]))) == {"9001", "9002"}
    assert ids(mg.search(ArchiveQuery(sized_only=True))) == {"9001", "9003"}
    assert ids(mg.search(ArchiveQuery(q="tram"))) == {"9003"}


def test_records_pass_the_licence_gate_with_provenance(archives_config):
    pd = get_archive("pd12m", archives_config)
    recs = list(
        pd.records(
            SliceSpec(
                archive="pd12m",
                subject="Harbour views",
                category="setting",
                limit=10,
                query=ArchiveQuery(q="harbour", sort="largest"),
            )
        )
    )
    assert [r.remote_url for r in recs] == ["https://s3.example/a4.jpeg", "https://s3.example/a1.jpeg"]
    assert {normalise_licence(r.licence) for r in recs} == {"PD", "CC0"}
    assert recs[0].subject == "Harbour_views" and recs[0].category == "setting" and recs[0].source == "pd12m"
    assert "Rijksmuseum via PD12M" in recs[0].attribution

    mg = get_archive("megalith", archives_config)
    rec = next(mg.records(SliceSpec(archive="megalith", subject="street", query=ArchiveQuery(q="tram"))))
    assert normalise_licence(rec.licence) == "PD"
    assert rec.source_url == "https://www.flickr.com/photos/222@N02/9003" and rec.photographer == "222@N02"


def test_records_respect_limit_across_pages(archives_config):
    pd = get_archive("pd12m", archives_config)
    assert len(list(pd.records(SliceSpec(archive="pd12m", subject="x", limit=3)))) == 3


def test_rebuild_replaces_index_atomically(archives_config):
    pd = get_archive("pd12m", archives_config)
    first = pd.index_path.stat().st_ino
    assert pd.build_index() == 4
    assert sorted(p.name for p in archives_config.archive_index_dir.iterdir()) == ["megalith.duckdb", "pd12m.duckdb"]
    assert pd.index_path.stat().st_ino != first


def test_build_temp_file_is_not_the_readers_spill_dir(archives_config):
    """DuckDB's default temp dir for a reader of X.duckdb is X.duckdb.tmp; a build must not use it."""
    pd = get_archive("pd12m", archives_config)
    spill = pd.index_path.with_suffix(".duckdb.tmp")
    spill.mkdir()
    (spill / "spilled").write_bytes(b"x")  # a reader spilling while we rebuild
    assert pd.build_index() == 4
    assert (spill / "spilled").exists()


# ── server ────────────────────────────────────────────────────────────
fastapi = pytest.importorskip("fastapi", reason="server extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from argus_quarry.server import create_app  # noqa: E402


def _app(cfg: QuarryConfig, monkeypatch, **kw):
    monkeypatch.setenv("QUARRY_PD12M_DIR", str(cfg.archive_dirs["pd12m"]))
    monkeypatch.setenv("QUARRY_MEGALITH_DIR", str(cfg.archive_dirs["megalith"]))
    return TestClient(create_app(home=cfg.home, **kw))


def test_archive_listing_and_search(archives_config, monkeypatch):
    client = _app(archives_config, monkeypatch)
    listing = client.get("/archives").json()
    assert listing["fetch_allowed"] is False
    assert {a["name"]: a["items"] for a in listing["archives"]} == {"megalith": 3, "pd12m": 4}
    page = client.get("/archives/pd12m/search", params={"q": "harbour", "collection": "Rijksmuseum"}).json()
    assert page["total"] == 2
    assert client.get("/archives/pd12m/facets").json()["n_collections"] == 3
    assert client.get("/archives/pd12m/items/a2").json()["collection"] == "Metropolitan Museum of Art"
    assert client.get("/archives/pd12m/items/nope").status_code == 404
    assert client.get("/archives/flickr/search").status_code == 404


def test_slice_returns_spec_and_cli_command(archives_config, monkeypatch):
    client = _app(archives_config, monkeypatch)
    body = client.get(
        "/archives/pd12m/slice", params={"q": "harbour", "subject": "Harbour views", "category": "setting", "limit": 1}
    ).json()
    assert body["matches"] == 2 and body["will_fetch"] == 1
    assert body["command"] == "argus-quarry archives fetch slice.json"
    assert SliceSpec.model_validate(body["slice"]).subject == "Harbour_views"


def test_fetch_refused_returns_slice_and_command(archives_config, monkeypatch):
    client = _app(archives_config, monkeypatch)
    spec = SliceSpec(archive="pd12m", subject="harbour").model_dump(mode="json")
    resp = client.post("/archives/pd12m/fetch", json=spec)
    assert resp.status_code == 403
    body = resp.json()
    assert "QUARRY_ALLOW_FETCH" in body["detail"]
    assert body["slice"]["subject"] == "harbour" and body["command"].startswith("argus-quarry archives fetch")


def test_fetch_allowed_lands_slice_through_ingest(archives_config, monkeypatch):
    assets = {f"https://s3.example/a{i}.jpeg": make_image_bytes(64, seed=i) for i in (1, 4)}
    client = _app(archives_config, monkeypatch, allow_fetch=True, net_factory=lambda _cfg: FakeNet(assets))
    spec = SliceSpec(archive="pd12m", subject="Harbour", category="setting", query=ArchiveQuery(q="harbour"))
    resp = client.post("/archives/pd12m/fetch", json=spec.model_dump(mode="json"))
    assert resp.status_code == 200
    frames = [json.loads(line) for line in resp.text.splitlines()]
    assert frames[0] == {"type": "start", "total": 2, "category": "setting", "subject": "Harbour"}
    assert [f["status"] for f in frames if f["type"] == "item"] == ["complete", "complete"]
    assert frames[-1]["type"] == "done" and frames[-1]["summary"]["complete"] == 2
    landed = sorted(p.name for p in (archives_config.images_dir / "setting" / "Harbour").iterdir())
    assert len(landed) == 2 and all("_pd12m_" in n for n in landed)
    photos = client.get("/photos", params={"source": "pd12m"}).json()
    assert photos["total"] == 2 and {p["licence"] for p in photos["photos"]} == {"CC0", "PD"}
    # rerun is idempotent: ingest skips what already landed
    again = [
        json.loads(x) for x in client.post("/archives/pd12m/fetch", json=spec.model_dump(mode="json")).text.splitlines()
    ]
    assert again[-1]["summary"]["skipped"] == 2


def test_fetch_rejects_mismatched_archive(archives_config, monkeypatch):
    client = _app(archives_config, monkeypatch, allow_fetch=True)
    spec = SliceSpec(archive="megalith", subject="x").model_dump(mode="json")
    assert client.post("/archives/pd12m/fetch", json=spec).status_code == 400


def test_thumbs(archives_config, monkeypatch):
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return make_image_bytes(800, seed=3)

    client = _app(archives_config, monkeypatch, thumb_fetcher=fetcher)
    resp = client.get("/archives/pd12m/thumb", params={"id": "a1", "size": 128})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/webp"
    client.get("/archives/pd12m/thumb", params={"id": "a1", "size": 128})
    assert calls == ["https://s3.example/a1.jpeg"]  # second hit served from memory
    redirect = client.get("/archives/megalith/thumb", params={"id": "9001"}, follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"].endswith("9001_abc.jpg")


def test_cors_allows_post_only_when_fetch_is_enabled(archives_config, monkeypatch):
    def preflight(allow: bool, origins: list[str] | None = None) -> str:
        app = create_app(home=archives_config.home, cors=True, cors_origins=origins, allow_fetch=allow)
        r = TestClient(app).options(
            "/archives/pd12m/fetch",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        return r.headers.get("access-control-allow-methods", "")

    assert "POST" not in preflight(False)
    assert "POST" not in preflight(True)  # "*" never gets the write route
    assert "POST" in preflight(True, ["http://localhost:3000"])
    assert "POST" not in preflight(False, ["http://localhost:3000"])
    assert "POST" not in preflight(True, ["*"])  # a listed "*" is still the read-only wildcard


def test_cors_origins_from_env(archives_config, monkeypatch):
    # The CLI has no other way to hand the server an origin list, so without the
    # env var the studio's fetch button could never pass a POST preflight.
    monkeypatch.setenv("QUARRY_CORS_ORIGINS", "http://localhost:3000/, http://192.168.1.39:3000")
    client = TestClient(create_app(home=archives_config.home, allow_fetch=True))
    r = client.options(
        "/archives/pd12m/fetch",
        headers={"Origin": "http://192.168.1.39:3000", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200 and "POST" in r.headers["access-control-allow-methods"]
    other = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


# ── CLI ───────────────────────────────────────────────────────────────
def test_cli_index_status_and_dry_run(tmp_path, pd12m_dir, monkeypatch):
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    from argus_quarry.cli import app

    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "quarry"))
    monkeypatch.setenv("QUARRY_PD12M_DIR", str(pd12m_dir))
    runner = CliRunner()
    assert "4 items" in runner.invoke(app, ["archives", "index", "pd12m"]).output
    assert "4 items" in runner.invoke(app, ["archives", "status"]).output
    spec = tmp_path / "slice.json"
    spec.write_text(SliceSpec(archive="pd12m", subject="h", query=ArchiveQuery(q="harbour")).model_dump_json())
    out = runner.invoke(app, ["archives", "fetch", str(spec), "--dry-run"]).output
    assert "2 of 2 match(es)" in out and "s3.example/a1.jpeg" in out


def test_failed_build_leaves_no_temp_file_and_keeps_old_index(archives_config, megalith_dir):
    mg = get_archive("megalith", archives_config)
    before = mg.index_path.stat().st_ino
    (megalith_dir / "links" / "data" / "megalith-00001-of-00010.parquet").write_bytes(b"not parquet")
    with pytest.raises(ArchiveUnavailable, match="index build failed"):
        mg.build_index()
    assert sorted(p.name for p in archives_config.archive_index_dir.iterdir()) == ["megalith.duckdb", "pd12m.duckdb"]
    assert mg.index_path.stat().st_ino == before and mg.status()["items"] == 3


def test_server_and_slice_carry_exclusions(archives_config, monkeypatch):
    client = _app(archives_config, monkeypatch)
    page = client.get("/archives/pd12m/search", params={"q": "harbour", "exclude": "fog"}).json()
    assert [i["id"] for i in page["items"]] == ["a1"]
    derived = client.get("/archives/pd12m/search", params={"tone": "color", "medium": "photo,painting"}).json()
    assert {i["id"] for i in derived["items"]} == {"a2", "a3"}  # tone drops a1 (bw), medium drops a4
    assert ids(client.get("/archives/pd12m/search", params={"medium": "painting"}).json()) == {"a2"}
    assert ids(client.get("/archives/pd12m/search", params={"tone": "bw"}).json()) == {"a1"}
    assert ids(client.get("/archives/pd12m/search", params={"year_min": 1980}).json()) == {"a3"}
    assert client.get("/archives/pd12m/search", params={"medium": "paintng"}).status_code == 422
    assert client.get("/archives/pd12m/search", params={"year_min": 2**40}).status_code == 422
    assert client.get("/archives/pd12m/slice", params={"subject": "h", "category": "../x"}).status_code == 422
    plan = client.get("/archives/pd12m/slice", params={"q": "harbour -fog", "subject": "h"}).json()
    assert plan["matches"] == 1 and plan["slice"]["query"]["q"] == "harbour -fog"


def test_medium_is_read_from_the_caption_head_only(tmp_path):
    cols = ("id", "url", "caption", "width", "height", "mime_type", "hash", "license", "source")
    captions = {
        "m1": "The image shows a blue and white porcelain brush pot with a painting of a man.",
        "m2": "The image is a black and white photo of a harbor.",
        "m3": "The image shows an old drawing of a castle on a hill.",
        "m4": "A watercolour of a lake at dusk.",
        "m5": "The photo shows a harbor at dawn.",
        "m6": "This photograph is of an old oil painting of a ship.",
    }
    rows = [
        (k, f"https://s3.example/{k}.jpeg", c, 800, 600, "image/jpeg", k, PD_ZERO, "Test") for k, c in captions.items()
    ]
    root = tmp_path / "pd"
    _write_parquet(root / "metadata" / "x.parquet", [dict(zip(cols, r, strict=True)) for r in rows])
    cfg = QuarryConfig(home=tmp_path / "q", archive_dirs={"pd12m": root})
    pd = get_archive("pd12m", cfg)
    pd.build_index()
    got = {i["id"]: i["medium"] for i in pd.search(ArchiveQuery())["items"]}
    assert got == {"m1": None, "m2": "photo", "m3": "drawing", "m4": "painting", "m5": "photo", "m6": "painting"}


def test_thumb_refuses_huge_non_jpeg_originals(archives_config, monkeypatch):
    import io

    from PIL import Image

    import argus_quarry.server.app as server_app

    monkeypatch.setattr(server_app, "ARCHIVE_THUMB_MAX_PIXELS", 100 * 100)
    buf = io.BytesIO()
    Image.new("RGB", (200, 200)).save(buf, "PNG")  # small file, too many pixels
    client = _app(archives_config, monkeypatch, thumb_fetcher=lambda _url: buf.getvalue())
    resp = client.get("/archives/pd12m/thumb", params={"id": "a3"})
    assert resp.status_code == 502 and "too large" in resp.json()["detail"]
    jpeg = make_image_bytes(200, seed=4)  # JPEG decodes at reduced scale, so the cap does not apply
    client = _app(archives_config, monkeypatch, thumb_fetcher=lambda _url: jpeg)
    assert client.get("/archives/pd12m/thumb", params={"id": "a1"}).status_code == 200


def test_megalith_duplicate_size_rows_do_not_duplicate_items(tmp_path, megalith_dir):
    rows = [
        {"url": "https://farm1.staticflickr.com/1/9001_abc_b.jpg", "status": "success", "width": 1024, "height": 683}
    ]
    _write_parquet(megalith_dir / "drawthings" / "00001.parquet", rows)  # 9001 now listed twice
    mg = get_archive("megalith", QuarryConfig(home=tmp_path / "q", archive_dirs={"megalith": megalith_dir}))
    assert mg.build_index() == 3


def test_cli_archive_errors_are_one_line_not_tracebacks(tmp_path, pd12m_dir, monkeypatch):
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    from argus_quarry.cli import app

    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "quarry"))
    monkeypatch.setenv("QUARRY_PD12M_DIR", str(pd12m_dir))
    runner = CliRunner()
    for args, needle in (
        (["archives", "search", "pd12m"], "archives index pd12m"),  # not indexed yet
        (["archives", "search", "flickr"], "unknown archive"),
        (["archives", "index", "flickr"], "unknown archive"),
        (["archives", "fetch", str(tmp_path / "missing.json")], "missing.json"),
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
        assert needle in result.output and "Traceback" not in result.output, (args, result.output)
        assert isinstance(result.exception, SystemExit), (args, result.exception)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"archive": "pd12m", "subject": "s", "category": "../../x"}))
    result = runner.invoke(app, ["archives", "fetch", str(bad)])
    assert result.exit_code == 2 and "category" in result.output
