"""Shared fixtures: a tmp QuarryConfig and an offline fake network layer."""

from __future__ import annotations

import contextlib
import io
import os
import stat
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from argus_quarry.config import QuarryConfig
from argus_quarry.models import SourceRecord


def make_image_bytes(size: int = 256, seed: int = 0, fmt: str = "JPEG") -> bytes:
    """Deterministic high-frequency noise image encoded to bytes."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, size=(size, size, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, fmt)
    return buf.getvalue()


class FakeNet:
    """Stand-in for :class:`argus_quarry.net.NetClient` — no real HTTP.

    ``assets`` maps a remote_url -> raw bytes; ``json_by_url`` maps an API URL ->
    a canned JSON dict (used by the Commons downloader tests).
    """

    def __init__(self, assets: dict[str, bytes] | None = None, json_by_url: dict[str, dict] | None = None) -> None:
        self.assets = assets or {}
        self.json_by_url = json_by_url or {}
        self.downloads: list[str] = []

    def get_json(self, url: str, *, params: dict | None = None, source: str = "default") -> dict:
        return self.json_by_url.get(url, {})

    def download(self, url: str, dest: Path, *, source: str = "default", max_bytes: int | None = None):
        self.downloads.append(url)
        data = self.assets[url]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def close(self) -> None:  # parity with NetClient
        pass


@pytest.fixture
def config(tmp_path: Path) -> QuarryConfig:
    cfg = QuarryConfig(home=tmp_path / "quarry")
    cfg.ensure_dirs()
    return cfg


def make_record(
    remote_url: str,
    *,
    subject: str = "Albert_Einstein",
    category: str = "identity",
    licence: str = "Public domain",
    year: int | None = 1921,
    source: str = "commons",
) -> SourceRecord:
    return SourceRecord(
        subject=subject,
        category=category,
        title="A portrait",
        photographer="Someone",
        year=year,
        remote_url=remote_url,
        source=source,
        source_url="https://example.org/page",
        licence=licence,
        attribution="Someone",
    )


@pytest.fixture
def read_only_dir():
    """Make a tree read-only for the test, restoring modes on teardown.

    Stands in for a `:ro` bind mount (issue #5), so it must deny writes to
    *files* as well as directories — dirs at 0o555 alone still let an existing
    file be rewritten in place, which a real read-only mount refuses with EROFS.
    Root ignores the mode bits, so tests that need it ``xfail`` there rather than
    passing vacuously — ``xfail`` (not ``skip``) so the lost coverage stays
    visible in the run summary when the suite is run as root (issue #8).
    """
    if os.geteuid() == 0:
        pytest.xfail("root bypasses file permissions; cannot fake a read-only mount")

    restore: list[tuple[Path, int]] = []

    def _apply(root: Path) -> Path:
        # Files first: once a directory is 0o555 its entries can still be
        # chmod'ed by their owner, but collect modes before anything changes.
        for path in [root, *root.rglob("*")]:
            restore.append((path, stat.S_IMODE(path.stat().st_mode)))
        for path, _ in restore:
            path.chmod(0o555 if path.is_dir() else 0o444)
        return root

    yield _apply

    # Restore deepest-first so a parent is never re-locked before its children,
    # and never let one failure strand the rest of the tree as undeletable.
    for path, mode in reversed(restore):
        with contextlib.suppress(OSError):  # the test may have removed it
            path.chmod(mode)
