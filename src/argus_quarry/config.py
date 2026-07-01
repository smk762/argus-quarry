"""Runtime configuration — caps, paths, and per-source politeness.

``QuarryConfig`` centralises everything a run needs: where the raw pool lives
(``QUARRY_HOME``), the per-file resolution/size ceilings, the total-archive
budget, and the polite ``User-Agent`` sources ask for. Construct it from the
environment with :meth:`QuarryConfig.from_env` (what the CLI/compose use) or
directly for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_USER_AGENT = "argus-quarry/0.1 (https://github.com/smk762/argus-quarry)"


class SourceRate(BaseModel):
    """Per-source token-bucket rate limit (requests per second + burst)."""

    rate_per_sec: float = 2.0
    burst: int = 4


class QuarryConfig(BaseModel):
    """All knobs for an acquisition run.

    Paths are derived from :attr:`home` (``QUARRY_HOME``) so the DB, cache and
    logs stay in a side-car dir the curator never scans.
    """

    home: Path = Field(default_factory=lambda: Path("./quarry"))

    # Per-file resolution / size ceilings (Q3). The full-res remote_url is always
    # kept in the DB, so a capped image can be re-fetched at original size later.
    max_megapixels: float = 12.0
    max_file_bytes: int = 15 * 1024 * 1024

    # Total raw-pool ceiling in bytes. 0 = unlimited.
    max_total_bytes: int = 40 * 1024 * 1024 * 1024

    # Network politeness / resilience.
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout: float = 15.0
    read_timeout: float = 60.0
    max_retries: int = 4
    backoff_base: float = 0.5
    rates: dict[str, SourceRate] = Field(default_factory=dict)

    def rate_for(self, source: str) -> SourceRate:
        return self.rates.get(source, SourceRate())

    # ── Derived side-car paths (never inside the published image tree) ──
    @property
    def images_dir(self) -> Path:
        return self.home / "images"

    @property
    def metadata_dir(self) -> Path:
        return self.home / "metadata"

    @property
    def db_path(self) -> Path:
        return self.metadata_dir / "portraits.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.images_dir, self.metadata_dir, self.cache_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, **overrides: object) -> QuarryConfig:
        """Build config from environment, with explicit ``overrides`` winning."""
        data: dict[str, object] = {}

        home = os.environ.get("QUARRY_HOME")
        if home:
            data["home"] = Path(home)

        max_gb = os.environ.get("QUARRY_MAX_GB")
        if max_gb is not None and max_gb.strip() != "":
            gb = float(max_gb)
            # 0 (or unset) means unlimited.
            data["max_total_bytes"] = int(gb * 1024 * 1024 * 1024)

        ua = os.environ.get("COMMONS_USER_AGENT") or os.environ.get("QUARRY_USER_AGENT")
        if ua:
            data["user_agent"] = ua

        mp = os.environ.get("QUARRY_MAX_MEGAPIXELS")
        if mp:
            data["max_megapixels"] = float(mp)

        data.update(overrides)
        return cls(**data)
