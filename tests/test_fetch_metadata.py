"""The metadata archiver (scripts/fetch_public_domain_metadata.py): manifest writes and the mount guard."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_meta", ROOT / "scripts" / "fetch_public_domain_metadata.py")
fetch_meta = importlib.util.module_from_spec(_spec)
sys.modules["fetch_meta"] = fetch_meta  # @dataclass resolves its module by name
_spec.loader.exec_module(fetch_meta)


def _rec(part: str) -> dict:
    return {"part": part, "repo_id": "x/y", "revision": "abc"}


def test_manifest_survives_an_interrupted_write(tmp_path, monkeypatch):
    fetch_meta.write_manifest(tmp_path, "pd12m", [_rec("metadata")])
    path = tmp_path / "pd12m" / "SOURCE.json"
    before = path.read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fetch_meta.os, "replace", boom)
    with pytest.raises(OSError):
        fetch_meta.write_manifest(tmp_path, "pd12m", [_rec("embeddings")])
    assert path.read_text() == before  # the old manifest is untouched, not truncated
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_manifest_merge_waits_for_a_concurrent_writer(tmp_path):
    fetch_meta.write_manifest(tmp_path, "megalith", [_rec("links")])
    path = tmp_path / "megalith" / "SOURCE.json"
    with open(path.with_name(".SOURCE.json.lock"), "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        t = threading.Thread(target=fetch_meta.write_manifest, args=(tmp_path, "megalith", [_rec("captions")]))
        t.start()
        time.sleep(0.3)
        assert "captions" not in json.loads(path.read_text())["parts"]  # blocked on the lock
        fcntl.flock(held, fcntl.LOCK_UN)
    t.join(5)
    assert set(json.loads(path.read_text())["parts"]) == {"links", "captions"}


def test_check_root_refuses_the_root_filesystem(tmp_path, monkeypatch):
    # tmp_path exists (like an unmounted NAS's empty mount-point dir) but lives on "/".
    monkeypatch.setattr(fetch_meta.os.path, "ismount", lambda p: str(p) == "/")
    with pytest.raises(SystemExit):
        fetch_meta.check_root(tmp_path, create=False)
    fetch_meta.check_root(tmp_path, create=False, allow_root_fs=True)


def test_check_root_accepts_a_mounted_volume(tmp_path, monkeypatch):
    mount = tmp_path.resolve()
    monkeypatch.setattr(fetch_meta.os.path, "ismount", lambda p: Path(p) in (mount, Path("/")))
    fetch_meta.check_root(tmp_path / "pd", create=True)
