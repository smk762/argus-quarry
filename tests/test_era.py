"""Zero-shot CLIP era (#19): the pinned prompt set, chunked scoring, the index join and the filters.

No model and no real archive: a synthetic prompt set whose label vectors are basis
directions, and fixture embeddings that sit near one label each.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="archives extra not installed")
np = pytest.importorskip("numpy", reason="embeddings extra not installed")

from argus_quarry.archives import ArchiveQuery, ArchiveUnavailable, get_archive  # noqa: E402
from argus_quarry.archives import era as ze  # noqa: E402
from argus_quarry.archives import semantic as sem  # noqa: E402
from argus_quarry.config import QuarryConfig  # noqa: E402

LABELS = [ze.label_key(k, e) for k, e in ze.LABEL_PROMPTS]
DIM = 32  # > len(LABELS): every label gets its own axis
NPY_SPLIT = (130, 70)
N = sum(NPY_SPLIT)


def _prompts(sha: str = "test-prompts") -> ze.PromptSet:
    matrix = np.eye(len(LABELS), DIM, dtype=np.float32)
    return ze.PromptSet(LABELS, matrix, np.zeros(len(LABELS), np.float32), 100.0, sha256=sha)


def _label_of(i: int) -> str:
    return LABELS[i % len(LABELS)]


def _vectors() -> np.ndarray:
    rng = np.random.default_rng(19)
    v = rng.normal(0, 0.02, (N, DIM)).astype(np.float32)
    for i in range(N):
        v[i, i % len(LABELS)] += 1.0
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
    rows = [
        {
            "id": f"p{i:04d}",
            "url": f"https://s3.example/p{i:04d}.jpg",
            "caption": f"An image, {_label_of(i)}",
            "width": 1000,
            "height": 800,
            "mime_type": "image/jpeg",
            "hash": f"h{i}",
            "license": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": "Somewhere",
        }
        for i in range(N)
    ]
    _write_parquet(root / "metadata" / "pd12m.000.parquet", rows)
    emb = root / "embeddings" / "embeddings"
    emb.mkdir(parents=True)
    v = _vectors()
    np.save(emb / "pd12m.00.npy", v[: NPY_SPLIT[0]])
    np.save(emb / "pd12m.01.npy", v[NPY_SPLIT[0] :])
    return root


@pytest.fixture
def cfg(tmp_path: Path, pd12m_root: Path) -> QuarryConfig:
    c = QuarryConfig(home=tmp_path / "quarry", archive_dirs={"pd12m": pd12m_root})
    c.ensure_dirs()
    get_archive("pd12m", c).build_index()
    return c


def _ids(page: dict) -> set[str]:
    return {item["id"] for item in page["items"]}


def _expected(kind: str | None = None, era: str | None = None) -> set[str]:
    return {
        f"p{i:04d}"
        for i in range(N)
        if (kind is None or _label_of(i).startswith(f"{kind}:")) and (era is None or _label_of(i).endswith(f":{era}"))
    }


# ── the committed prompt set ─────────────────────────────────────────────
def test_committed_prompt_set_is_pinned_and_covers_every_label():
    prompts = ze.load_prompts()
    assert prompts.sha256 == ze.PROMPTS_SHA256
    assert sorted(prompts.labels) == sorted(LABELS)
    assert prompts.matrix.shape == (len(LABELS), 768)  # CLIP ViT-L/14's space, as PD12M published it
    assert prompts.logit_scale == 100.0
    doc = json.loads(ze._prompts_bytes())
    assert doc["model"] == ze.MODEL_REPO == get_archive("pd12m", QuarryConfig()).embedding_model
    for (kind, era), texts in ze.LABEL_PROMPTS.items():
        assert doc["labels"][ze.label_key(kind, era)]["prompts"] == list(texts)
    # Photo captions date their subject, not the photo, so photo labels are never calibrated (docs/era-calibration.md).
    assert all(b == 0 for b, k in zip(prompts.bias, prompts.kinds, strict=True) if k == "photo")


def test_an_edited_prompt_file_is_refused():
    raw = ze._prompts_bytes().replace(b'"bias":0.0', b'"bias":0.5', 1)
    with pytest.raises(ze.EraUnavailable, match="not the pinned prompt set"):
        ze.load_prompts(raw)
    assert ze.load_prompts(raw, expected_sha256=hashlib.sha256(raw).hexdigest()).sha256 != ze.PROMPTS_SHA256


def test_score_marginalises_labels_into_era_and_kind():
    prompts = _prompts()
    s = prompts.score(_vectors() / np.linalg.norm(_vectors(), axis=1, keepdims=True))
    for i in (0, 4, 16, 33):
        kind, era = _label_of(i).split(":")
        assert ze.ERAS[s["era"][i]] == era and ze.KINDS[s["kind"][i]] == kind
        assert 0.9 < s["era_conf"][i] <= 1.0
    # Two photo labels of one era split the era's mass evenly, but its kind stays certain.
    x = np.zeros((1, DIM), np.float32)
    x[0, LABELS.index("photo:1800s")] = x[0, LABELS.index("photo:1900-1945")] = 1 / np.sqrt(2)
    s = prompts.score(x)
    assert ze.KINDS[s["kind"][0]] == "photo" and s["kind_conf"][0] > 0.99
    assert s["era_conf"][0] == pytest.approx(0.5, abs=0.01)


# ── scoring, the index join and the filters ──────────────────────────────
def test_era_scores_join_the_index_and_filter(cfg):
    archive = get_archive("pd12m", cfg)
    assert archive.status()["era"]["scored"] is None
    item = archive.get("p0000")
    assert item["clip_era"] is None and item["clip_kind"] is None  # unscored, not guessed
    result = archive.build_era(prompts=_prompts(), chunk_rows=64)
    assert result == {**result, "rows": N, "total": N}
    archive.build_index()

    status = archive.status()["era"]
    assert status["scored"] == N and status["of"] == N and status["source"] == "clip-zero-shot"
    assert status["current"] is False  # scored with the test prompts, not the pinned set
    for i in (0, 5, 16, 199):
        kind, era = _label_of(i).split(":")
        got = archive.get(f"p{i:04d}")
        assert (got["clip_kind"], got["clip_era"]) == (kind, era)
        assert got["clip_era_conf"] > 0.9

    photo_digital = ArchiveQuery(clip_kinds=["photo"], clip_eras=["2000-now"])
    assert _ids(archive.search(photo_digital, limit=200)) == _expected("photo", "2000-now")
    pre_digital = ArchiveQuery(clip_kinds=["photo"], clip_eras=list(ze.PRE_DIGITAL_ERAS))
    assert _ids(archive.search(pre_digital, limit=200)) == _expected("photo") - _expected("photo", "2000-now")
    assert archive.count(ArchiveQuery(clip_era_min_conf=0.99, clip_kind_min_conf=0.99)) == N

    facets = archive.facets()
    assert facets["clip_scored"] == N
    assert [e["name"] for e in facets["clip_eras"]] == list(ze.ERAS)  # oldest first
    assert {k["name"] for k in facets["clip_kinds"]} == set(ze.KINDS)
    assert facets["date_sources"] == {
        "year": "caption",
        "clip_era": "clip-zero-shot",
        "clip_kind": "clip-zero-shot",
        "upload_year": "flickr-upload",
    }


def test_unknown_era_or_kind_is_a_validation_error():
    with pytest.raises(ValueError, match="unknown era"):
        ArchiveQuery(clip_eras=["1990s"])
    with pytest.raises(ValueError, match="unknown kind"):
        ArchiveQuery(clip_kinds=["sculpture"])
    assert ArchiveQuery(clip_eras=[" 2000-NOW "]).clip_eras == ["2000-now"]


def test_scoring_reads_one_chunk_at_a_time(cfg, monkeypatch):
    spans: list[int] = []
    real = sem._read_rows

    def spy(sources, starts, lo, hi):
        spans.append(hi - lo)
        return real(sources, starts, lo, hi)

    monkeypatch.setattr(sem, "_read_rows", spy)
    get_archive("pd12m", cfg).build_era(prompts=_prompts(), chunk_rows=48)
    assert max(spans) <= 48 and sum(spans) == N  # the 200 x DIM matrix is never read whole


def test_an_interrupted_run_resumes_and_a_new_prompt_set_starts_over(cfg, monkeypatch):
    archive = get_archive("pd12m", cfg)
    root = cfg.archive_index_dir

    class Stop(Exception):
        pass

    def die_after_two(done, total):
        if done >= 2 * 50:
            raise Stop

    with pytest.raises(Stop):
        archive.build_era(prompts=_prompts(), chunk_rows=50, progress=die_after_two)
    parts = sorted(p.name for p in (root / ".pd12m.era.parts").glob("*.parquet"))
    assert len(parts) == 2 and not ze.scores_path(root, "pd12m").exists()

    reads: list[int] = []
    real = sem._read_rows
    monkeypatch.setattr(sem, "_read_rows", lambda s, st, lo, hi: reads.append(lo) or real(s, st, lo, hi))
    archive.build_era(prompts=_prompts(), chunk_rows=50)
    assert reads == [100, 150]  # the two finished chunks were not scored again
    assert not (root / ".pd12m.era.parts").exists()
    assert ze.read_scores_meta(root, "pd12m")["rows"] == N

    with pytest.raises(Stop):
        archive.build_era(prompts=_prompts(), chunk_rows=50, progress=die_after_two)
    reads.clear()
    archive.build_era(prompts=_prompts("other-prompts"), chunk_rows=50)
    assert reads == [0, 50, 100, 150]  # parts from another prompt set are never mixed in
    assert ze.read_scores_meta(root, "pd12m")["prompts_sha256"] == "other-prompts"


def test_a_sample_run_leaves_the_rest_unscored(cfg):
    archive = get_archive("pd12m", cfg)
    archive.build_era(prompts=_prompts(), chunk_rows=32, limit_rows=40)
    archive.build_index()
    assert archive.status()["era"]["scored"] == 40 and archive.status()["era"]["of"] == N
    assert archive.get("p0039")["clip_era"] is not None
    assert archive.get("p0040")["clip_era"] is None
    assert archive.count(ArchiveQuery(clip_eras=list(ze.ERAS))) == 40


def test_scores_for_other_embeddings_are_never_joined(cfg, tmp_path):
    archive = get_archive("pd12m", cfg)
    other = tmp_path / "other.npy"
    np.save(other, _vectors()[:N])
    archive.build_era(prompts=_prompts(), files=[other])
    con = duckdb.connect()
    path = ze.scores_path(cfg.archive_index_dir, "pd12m")
    # Rewrite the file as if scored over a matrix of a different length.
    kv = "{'era_schema': '1', 'total': '999', 'prompts_sha256': 'x', 'model': 'm', 'built_at': 'now'}"
    con.execute(f"COPY (SELECT * FROM read_parquet('{path}')) TO '{path}.x' (FORMAT parquet, KV_METADATA {kv})")
    con.close()
    Path(f"{path}.x").replace(path)
    archive.build_index()
    assert archive.get("p0000")["clip_era"] is None


def test_era_refuses_a_row_count_mismatch_and_archives_without_embeddings(cfg, tmp_path):
    archive = get_archive("pd12m", cfg)
    short = tmp_path / "short.npy"
    np.save(short, _vectors()[:10])
    with pytest.raises(ArchiveUnavailable, match="rows"):
        archive.build_era(prompts=_prompts(), files=[short])
    with pytest.raises(ArchiveUnavailable, match="768-d|32-d"):
        archive.build_era(prompts=ze.load_prompts())  # 768-d prompts against 32-d fixture embeddings
    megalith = get_archive("megalith", cfg)
    assert megalith.status()["era"] is None
    with pytest.raises(ArchiveUnavailable, match="no embeddings"):
        megalith.build_era()


def test_era_reads_the_local_float16_copy_when_the_vector_index_has_one(cfg):
    pytest.importorskip("faiss", reason="embeddings extra not installed")
    archive = get_archive("pd12m", cfg)
    assert [p.name for p in archive.era_vector_files()] == ["pd12m.00.npy", "pd12m.01.npy"]
    archive.build_embeddings()
    files = archive.era_vector_files()
    assert len(files) == 1 and files[0].name == "vectors.f16.npy"
    archive.build_era(prompts=_prompts())
    archive.build_index()
    assert archive.get("p0016")["clip_era"] == _label_of(16).split(":")[1]


# ── HTTP and CLI ──────────────────────────────────────────────────────────
def test_server_filters_on_clip_era_and_kind(cfg):
    pytest.importorskip("fastapi", reason="server extra not installed")
    from fastapi.testclient import TestClient

    from argus_quarry.server import create_app

    archive = get_archive("pd12m", cfg)
    archive.build_era(prompts=_prompts())
    archive.build_index()
    client = TestClient(create_app(home=cfg.home))
    r = client.get(
        "/archives/pd12m/search",
        params={"clip_kind": "photo", "clip_era": "2000-now", "clip_era_min_conf": 0.5, "limit": 200},
    )
    assert r.status_code == 200
    assert _ids(r.json()) == _expected("photo", "2000-now")
    assert {"clip_era", "clip_era_conf", "clip_kind", "clip_kind_conf"} <= set(r.json()["items"][0])
    assert client.get("/archives/pd12m/search", params={"clip_era": "1990s"}).status_code == 422
    assert client.get("/archives/pd12m/search", params={"clip_era_min_conf": 2}).status_code == 422
    sliced = client.get("/archives/pd12m/slice", params={"clip_era": "2000-now", "subject": "x"}).json()
    assert sliced["slice"]["query"]["clip_eras"] == ["2000-now"]
    assert sliced["matches"] == len(_expected(era="2000-now"))


def test_cli_era_scores_reindexes_and_filters(cfg, monkeypatch):
    pytest.importorskip("typer", reason="cli extra not installed")
    from typer.testing import CliRunner

    from argus_quarry.cli import app

    monkeypatch.setenv("QUARRY_HOME", str(cfg.home))
    monkeypatch.setenv("QUARRY_PD12M_DIR", str(cfg.archive_dirs["pd12m"]))
    monkeypatch.setattr(ze, "load_prompts", lambda *a, **k: _prompts())
    runner = CliRunner()
    r = runner.invoke(app, ["archives", "era", "pd12m", "--chunk-rows", "1024"])
    assert r.exit_code == 0, r.output
    assert f"{N:,} of {N:,} rows scored" in r.output and "reindexed" in r.output
    r = runner.invoke(app, ["archives", "status"])
    assert f"CLIP era {N:,}/{N:,}" in r.output
    r = runner.invoke(app, ["archives", "search", "pd12m", "--clip-kind", "photo", "--clip-era", "2000-now"])
    assert r.exit_code == 0 and f"{len(_expected('photo', '2000-now'))} match(es)" in r.output
    r = runner.invoke(app, ["archives", "era", "megalith"])
    assert r.exit_code == 2 and "error:" in r.output
