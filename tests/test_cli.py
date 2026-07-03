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
