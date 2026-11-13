"""Semantic / similar_to search over a small synthetic embedding matrix (no torch: a fake text encoder)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="archives extra not installed")
np = pytest.importorskip("numpy", reason="embeddings extra not installed")
pytest.importorskip("faiss", reason="embeddings extra not installed")

from argus_quarry.archives import (  # noqa: E402
    ArchiveQuery,
    ArchiveUnavailable,
    ItemNotFound,
    SliceSpec,
    get_archive,
)
from argus_quarry.archives import semantic as sem  # noqa: E402
from argus_quarry.config import QuarryConfig  # noqa: E402

DIM = 16
TOPICS = ("cat", "dog", "boat")
#: Deliberately misaligned with the parquet files (250/250/100), so pos must cross file boundaries.
NPY_SPLIT = (400, 200)
PARQUET_SPLIT = (250, 250, 100)
N = sum(PARQUET_SPLIT)
DUP_OF, DUP = 5, 7  # row 7 is a near-duplicate of row 5


def _vectors() -> np.ndarray:
    rng = np.random.default_rng(15)
    v = rng.normal(0, 0.3, (N, DIM)).astype(np.float32)
    for i in range(N):
        v[i, i % 3] += 3.0
    v[DUP] = v[DUP_OF] * 1.0001
    return v


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
def pd12m_root(tmp_path: Path) -> Path:
    root = tmp_path / "pd12m"
    start = 0
    for f, n in enumerate(PARQUET_SPLIT):
        rows = [
            {
                "id": f"p{i:04d}",
                "url": f"https://s3.example/p{i:04d}.jpg",
                "caption": f"A photo of a {TOPICS[i % 3]}",
                "width": 1000 + i,
                "height": 800,
                "mime_type": "image/jpeg",
                "hash": f"h{i}",
                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                "source": "Even" if i % 2 == 0 else "Odd",
            }
            for i in range(start, start + n)
        ]
        _write_parquet(root / "metadata" / "metadata" / f"pd12m.{f:03d}.parquet", rows)
        start += n
    emb = root / "embeddings" / "embeddings"
    emb.mkdir(parents=True)
    v = _vectors()
    np.save(emb / "pd12m.00.npy", v[: NPY_SPLIT[0]])
    np.save(emb / "pd12m.01.npy", v[NPY_SPLIT[0] :])
    # The Hugging Face cache sits alongside; anything in a hidden dir is not part of the matrix.
    (root / "embeddings" / ".cache").mkdir()
    np.save(root / "embeddings" / ".cache" / "stray.npy", v[:3])
    return root


class FakeEncoder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def encode(self, text: str):
        self.calls.append(text)
        out = np.zeros(DIM, dtype=np.float32)
        for t, word in enumerate(TOPICS):
            if word in text:
                out[t] = 1.0
        return out


@pytest.fixture
def encoder():
    enc = FakeEncoder()
    sem.set_text_encoder(enc)
    yield enc
    sem.set_text_encoder(None)


@pytest.fixture
def cfg(tmp_path: Path, pd12m_root: Path) -> QuarryConfig:
    c = QuarryConfig(home=tmp_path / "quarry", archive_dirs={"pd12m": pd12m_root})
    c.ensure_dirs()
    get_archive("pd12m", c).build_index()
    return c


@pytest.fixture
def built(cfg: QuarryConfig) -> QuarryConfig:
    get_archive("pd12m", cfg).build_embeddings()
    return cfg


def _truth(query_vec: np.ndarray, mask: np.ndarray | None = None) -> list[str]:
    v = _vectors()
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    v = v.astype(np.float16).astype(np.float32)  # scored from the float16 copy, as the index does
    q = query_vec / np.linalg.norm(query_vec)
    scores = v @ q
    order = [i for i in np.lexsort((np.arange(N), -scores)) if mask is None or mask[i]]
    return [f"p{i:04d}" for i in order]


def test_pos_maps_items_onto_embedding_rows_across_files(cfg):
    with get_archive("pd12m", cfg)._connect() as con:
        rows = dict(con.execute("SELECT id, pos FROM items").fetchall())
    assert rows == {f"p{i:04d}": i for i in range(N)}


def test_status_before_and_after_build(cfg):
    archive = get_archive("pd12m", cfg)
    assert archive.status()["semantic"]["indexed"] is False
    meta = archive.build_embeddings()
    assert meta["rows"] == N and meta["factory"] == "Flat"
    assert meta["sources"] == ["pd12m.00.npy", "pd12m.01.npy"]  # the hidden .cache/stray.npy is ignored
    status = archive.status()["semantic"]
    assert status["indexed"] is True and status["vectors"] == N
    assert status["model"] == "openai/clip-vit-large-patch14"
    assert get_archive("megalith", cfg).status()["semantic"] is None  # publishes no embeddings


def test_small_chunks_within_and_across_files(cfg, encoder):
    """Chunks that sit inside one (read-only, memory-mapped) file as well as ones that straddle two."""
    archive = get_archive("pd12m", cfg)
    archive.build_embeddings(chunk_rows=64)
    page = archive.search(ArchiveQuery(semantic="cat"), limit=10)
    assert [i["id"] for i in page["items"]] == _truth(encoder.encode("cat"))[:10]


def test_semantic_query_ranks_exactly(built, encoder):
    archive = get_archive("pd12m", built)
    page = archive.search(ArchiveQuery(semantic="a cat"), limit=10)
    assert encoder.calls == ["a cat"]
    assert page["ranked"] is True and page["total"] == N
    assert [i["id"] for i in page["items"]] == _truth(encoder.encode("cat"))[:10]
    scores = [i["score"] for i in page["items"]]
    assert scores == sorted(scores, reverse=True) and scores[0] > 0.9
    assert all("cat" in i["caption"] for i in page["items"])
    # Paging continues the same ranking.
    nxt = archive.search(ArchiveQuery(semantic="a cat"), limit=10, offset=10)
    assert [i["id"] for i in nxt["items"]] == _truth(encoder.encode("cat"))[10:20]


def test_semantic_combines_with_filters(built, encoder):
    archive = get_archive("pd12m", built)
    query = ArchiveQuery(semantic="dog", collections=["Even"], min_width=1100)
    page = archive.search(query, limit=200)
    mask = np.array([i % 2 == 0 and 1000 + i >= 1100 for i in range(N)])
    assert page["total"] == int(mask.sum())
    assert [i["id"] for i in page["items"]] == _truth(encoder.encode("dog"), mask)[:200]
    calls = len(encoder.calls)
    assert archive.count(query) == int(mask.sum())
    assert len(encoder.calls) == calls  # counting never encodes
    assert archive.count(ArchiveQuery(similar_to="p0000", collections=["Even"])) == N // 2 - 1


def test_similar_to_finds_near_duplicates_and_excludes_itself(built):
    archive = get_archive("pd12m", built)
    page = archive.search(ArchiveQuery(similar_to=f"p{DUP_OF:04d}"), limit=5)
    got = [i["id"] for i in page["items"]]
    assert f"p{DUP_OF:04d}" not in got
    assert got[0] == f"p{DUP:04d}" and page["items"][0]["score"] > 0.999
    assert page["total"] == N - 1
    assert all(int(i[1:]) % 3 == DUP_OF % 3 for i in got[1:])  # then the same topic


def test_similar_to_unknown_id(built):
    with pytest.raises(ItemNotFound):
        get_archive("pd12m", built).search(ArchiveQuery(similar_to="nope"))


def test_semantic_and_similar_to_are_exclusive():
    with pytest.raises(ValueError, match="exclusive"):
        ArchiveQuery(semantic="cat", similar_to="p0001")
    assert ArchiveQuery(semantic="  ").ranked is False


def test_ivf_pq_path_with_selector(cfg, encoder, monkeypatch):
    """The large-archive path: IVF-PQ candidates (filter as a FAISS selector), exact rescoring."""
    archive = get_archive("pd12m", cfg)
    meta = archive.build_embeddings(factory="IVF8,PQ4x4", train_rows=N)
    assert meta["factory"] == "IVF8,PQ4x4"
    monkeypatch.setattr(sem, "EXACT_MAX", 0)  # force FAISS even for small filtered sets
    monkeypatch.setattr(sem, "POOL_MIN", 64)
    page = archive.search(ArchiveQuery(semantic="boat", collections=["Odd"]), limit=20)
    assert len(page["items"]) == 20
    assert all(i["collection"] == "Odd" and "boat" in i["caption"] for i in page["items"])
    scores = [i["score"] for i in page["items"]]
    assert scores == sorted(scores, reverse=True)
    unfiltered = archive.search(ArchiveQuery(semantic="boat"), limit=20)
    assert all("boat" in i["caption"] for i in unfiltered["items"])


def test_slice_records_follow_the_ranking(built, encoder):
    archive = get_archive("pd12m", built)
    spec = SliceSpec(archive="pd12m", query=ArchiveQuery(semantic="cat"), subject="cats", limit=7)
    records = list(archive.records(spec))
    assert [r.remote_url.rsplit("/", 1)[1][:-4] for r in records] == _truth(encoder.encode("cat"))[:7]
    assert all(r.licence == "CC0" and r.subject == "cats" for r in records)


def test_unbuilt_or_unsupported_is_unavailable(cfg, encoder):
    with pytest.raises(ArchiveUnavailable, match="--embeddings"):
        get_archive("pd12m", cfg).search(ArchiveQuery(semantic="cat"))
    with pytest.raises(ArchiveUnavailable, match="publishes no embeddings"):
        get_archive("megalith", cfg).build_embeddings()


def test_row_count_mismatch_writes_nothing(cfg, pd12m_root):
    np.save(pd12m_root / "embeddings" / "embeddings" / "pd12m.02.npy", _vectors()[:5])
    with pytest.raises(ArchiveUnavailable, match="not row-aligned"):
        get_archive("pd12m", cfg).build_embeddings()
    assert not any(p.name.startswith(("pd12m.semantic", ".pd12m.semantic")) for p in cfg.archive_index_dir.iterdir())


def test_rebuild_flips_the_pointer_and_drops_the_old_build(built, encoder):
    archive = get_archive("pd12m", built)
    root = built.archive_index_dir
    first = json.loads((root / "pd12m.semantic.json").read_text())["dir"]
    archive.search(ArchiveQuery(semantic="cat"))  # loads (and caches) the first build
    second = archive.build_embeddings()["dir"]
    assert second != first and not (root / first).exists() and (root / second).is_dir()
    assert sem.load_vector_index(root, "pd12m").meta["dir"] == second
    assert archive.search(ArchiveQuery(semantic="cat"), limit=3)["items"]


def test_stale_pointer_reads_as_not_indexed(built):
    pointer = built.archive_index_dir / "pd12m.semantic.json"
    meta = json.loads(pointer.read_text())
    pointer.write_text(json.dumps({**meta, "schema": sem.SEMANTIC_SCHEMA + 1}))
    assert get_archive("pd12m", built).status()["semantic"]["indexed"] is False


# ── server ───────────────────────────────────────────────────────────────
pytest.importorskip("fastapi", reason="server extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from argus_quarry.server import create_app  # noqa: E402


def _client(cfg: QuarryConfig, monkeypatch) -> TestClient:
    monkeypatch.setenv("QUARRY_PD12M_DIR", str(cfg.archive_dirs["pd12m"]))
    return TestClient(create_app(home=cfg.home))


def test_server_semantic_and_similar_to(built, encoder, monkeypatch):
    client = _client(built, monkeypatch)
    page = client.get("/archives/pd12m/search", params={"semantic": "cat", "collection": "Odd", "limit": 5}).json()
    assert page["ranked"] is True and len(page["items"]) == 5
    assert all(i["collection"] == "Odd" and "score" in i for i in page["items"])
    more = client.get("/archives/pd12m/search", params={"similar_to": f"p{DUP_OF:04d}", "limit": 1}).json()
    assert more["items"][0]["id"] == f"p{DUP:04d}"
    assert client.get("/archives/pd12m/search", params={"similar_to": "nope"}).status_code == 404
    both = client.get("/archives/pd12m/search", params={"semantic": "cat", "similar_to": "p0001"})
    assert both.status_code == 422
    sl = client.get("/archives/pd12m/slice", params={"semantic": "cat", "subject": "cats", "limit": 10}).json()
    assert sl["slice"]["query"]["semantic"] == "cat" and sl["matches"] == N
    listing = {a["name"]: a for a in client.get("/archives").json()["archives"]}
    assert listing["pd12m"]["semantic"]["vectors"] == N


def test_server_semantic_without_index_is_503(cfg, encoder, monkeypatch):
    resp = _client(cfg, monkeypatch).get("/archives/pd12m/search", params={"semantic": "cat"})
    assert resp.status_code == 503 and "--embeddings" in resp.json()["detail"]


def test_metadata_rebuilt_over_more_rows_is_unavailable_not_misaligned(built, encoder, pd12m_root):
    # Re-archived metadata gains a file; the vector index still has the old row count.
    _write_parquet(
        pd12m_root / "metadata" / "metadata" / "pd12m.003.parquet",
        [{"id": "p9999", "url": "https://s3.example/p9999.jpg", "caption": "A photo of a cat", "width": 1,
          "height": 1, "mime_type": "image/jpeg", "hash": "h", "license": "x", "source": "Even"}],
    )  # fmt: skip
    archive = get_archive("pd12m", built)
    archive.build_index()
    for query in (ArchiveQuery(semantic="cat", collections=["Even"]), ArchiveQuery(similar_to="p9999")):
        with pytest.raises(ArchiveUnavailable, match="--embeddings-only"):
            archive.search(query)
