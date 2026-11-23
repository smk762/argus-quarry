from __future__ import annotations

from typer.testing import CliRunner

from argus_quarry.cli import app

runner = CliRunner()


def test_subjects_command_lists_all_categories():
    result = runner.invoke(app, ["subjects"])
    assert result.exit_code == 0
    assert "Albert_Einstein" in result.stdout
    # a subject from each non-identity category shows up too
    assert "wardrobe" in result.stdout
    assert "setting" in result.stdout
    assert "concept" in result.stdout


def test_subjects_command_category_filter():
    result = runner.invoke(app, ["subjects", "--category", "identity"])
    assert result.exit_code == 0
    assert "Albert_Einstein" in result.stdout
    assert "wardrobe" not in result.stdout


def test_people_alias_still_works():
    result = runner.invoke(app, ["people"])
    assert result.exit_code == 0
    assert "Albert_Einstein" in result.stdout


def test_stats_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "quarry"))
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "Photographs:" in result.stdout


def _landed_pool(home):
    """A pool with one landed photo, its file then overwritten with other (valid) image bytes."""
    from conftest import FakeNet, make_image_bytes, make_record

    from argus_quarry.config import QuarryConfig
    from argus_quarry.ingest import IngestEngine
    from argus_quarry.store import ProvenanceStore

    cfg = QuarryConfig(home=home)
    cfg.ensure_dirs()
    url = "https://img/a.jpg"
    with ProvenanceStore(cfg.db_path) as store:
        out = IngestEngine(cfg, store, FakeNet(assets={url: make_image_bytes(seed=1)})).ingest_record(make_record(url))
    path = cfg.images_dir / "identity" / "Albert_Einstein" / out.filename
    return cfg, url, path


def test_verify_passes_a_clean_pool(tmp_path, monkeypatch):
    _landed_pool(tmp_path / "quarry")
    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "quarry"))
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0 and "ok=1" in result.stdout


def test_verify_fails_and_repair_marks_a_sha_mismatch(tmp_path, monkeypatch):
    from conftest import make_image_bytes

    from argus_quarry.store import ProvenanceStore

    cfg, url, path = _landed_pool(tmp_path / "quarry")
    path.write_bytes(make_image_bytes(seed=2))  # decodes fine, but not the recorded bytes
    monkeypatch.setenv("QUARRY_HOME", str(cfg.home))
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1 and "sha_mismatch=1" in result.stdout
    assert runner.invoke(app, ["verify", "--repair"]).exit_code == 0
    with ProvenanceStore(cfg.db_path) as store:
        assert store.get_by_remote_url(url).status == "failed"


def test_museum_rate_flag_overrides_a_configured_rate(tmp_path, monkeypatch):
    from argus_quarry.archives import museum as mu
    from argus_quarry.config import QuarryConfig, SourceRate

    cfg = QuarryConfig(home=tmp_path / "q")
    cfg.rates[mu.MET_RATE_SOURCE] = SourceRate(rate_per_sec=8, burst=8)
    monkeypatch.setattr(QuarryConfig, "from_env", staticmethod(lambda: cfg))

    def stop(*_a, **_k):
        raise LookupError("stopped before harvest")

    monkeypatch.setattr("argus_quarry.archives.get_archive", stop)
    kept = runner.invoke(app, ["archives", "museum", "pd12m"])
    assert kept.exit_code == 2 and cfg.rates[mu.MET_RATE_SOURCE].rate_per_sec == 8
    overridden = runner.invoke(app, ["archives", "museum", "pd12m", "--rate", "0.5"])
    assert overridden.exit_code == 2 and cfg.rates[mu.MET_RATE_SOURCE].rate_per_sec == 0.5
