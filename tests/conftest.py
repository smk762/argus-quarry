"""Shared fixtures: a tmp QuarryConfig and an offline fake network layer."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from argus_quarry.config import QuarryConfig
from argus_quarry.models import PortraitRecord


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
    person: str = "Albert_Einstein",
    licence: str = "Public domain",
    year: int | None = 1921,
    source: str = "commons",
) -> PortraitRecord:
    return PortraitRecord(
        person_name=person,
        title="A portrait",
        photographer="Someone",
        year=year,
        remote_url=remote_url,
        source=source,
        source_url="https://example.org/page",
        licence=licence,
        attribution="Someone",
    )
