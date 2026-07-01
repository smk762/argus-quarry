from __future__ import annotations

from typer.testing import CliRunner

from argus_quarry.cli import app

runner = CliRunner()


def test_people_command_lists_seed():
    result = runner.invoke(app, ["people"])
    assert result.exit_code == 0
    assert "Albert_Einstein" in result.stdout


def test_stats_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_HOME", str(tmp_path / "quarry"))
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "Photographs:" in result.stdout
