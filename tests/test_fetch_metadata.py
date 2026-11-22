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


@pytest.mark.parametrize("path", ["images/0001.tar", "a/b.jpg", "x.JPEG", "thumbs/p.png", "s.webp"])
def test_no_part_ever_fetches_a_photo(path):
    """The script archives metadata only (CLAUDE.md): every part's filter refuses photos and shards."""
    from fnmatch import fnmatch

    def kept(part, name: str) -> bool:  # huggingface_hub.filter_repo_objects' rule
        allowed = part.allow is None or any(fnmatch(name, p) for p in part.allow)
        return allowed and not any(fnmatch(name, p) for p in fetch_meta.ignore_patterns(part))

    for parts in fetch_meta.SOURCES.values():
        for part in parts.values():
            assert not kept(part, path) and not kept(part, path.lower()), (part.name, path)
            assert kept(part, "data/part-00001.parquet") or part.allow == ("embeddings/*",)


def test_manifest_keeps_its_permissions_across_rewrites(tmp_path):
    """mkstemp makes 0600 files: an atomic rewrite must not tighten SOURCE.json to owner-only."""
    import os
    import stat

    old = os.umask(0o022)
    try:
        fetch_meta.write_manifest(tmp_path, "pd12m", [_rec("metadata")])
        path = tmp_path / "pd12m" / "SOURCE.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o644  # a new manifest gets the umask's mode
        path.chmod(0o664)  # an operator widened it for a group
        fetch_meta.write_manifest(tmp_path, "pd12m", [_rec("embeddings")])
        assert stat.S_IMODE(path.stat().st_mode) == 0o664  # and a rewrite keeps it
    finally:
        os.umask(old)
