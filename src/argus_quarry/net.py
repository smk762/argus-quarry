"""HTTP layer — polite, rate-limited, retrying, resumable.

A single :class:`NetClient` wraps ``httpx`` with the things every downloader
needs: a descriptive ``User-Agent``, a per-source token-bucket rate limit,
exponential backoff with jitter on transient errors, and resumable streaming
downloads (HTTP ``Range`` continuation of a ``.part`` file in the cache).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from argus_quarry.config import QuarryConfig

logger = structlog.get_logger()

_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


class TokenBucket:
    """Simple monotonic token bucket for polite per-source pacing."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = max(rate_per_sec, 0.01)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()

    def take(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep((1.0 - self._tokens) / self.rate)


@dataclass
class DownloadResult:
    path: Path
    size: int
    resumed: bool


class NetClient:
    """Thin, retrying wrapper over ``httpx.Client`` with per-source pacing."""

    def __init__(self, config: QuarryConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._buckets: dict[str, TokenBucket] = {}
        self._client = client or httpx.Client(
            headers={"User-Agent": config.user_agent},
            timeout=httpx.Timeout(config.read_timeout, connect=config.connect_timeout),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NetClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _bucket(self, source: str) -> TokenBucket:
        if source not in self._buckets:
            rate = self.config.rate_for(source)
            self._buckets[source] = TokenBucket(rate.rate_per_sec, rate.burst)
        return self._buckets[source]

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self.config.backoff_base * (2**attempt) + random.uniform(0, self.config.backoff_base)
        time.sleep(delay)

    def get_json(self, url: str, *, params: dict | None = None, source: str = "default") -> dict:
        """GET a JSON API response, retrying transient failures."""
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            self._bucket(source).take()
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code in _TRANSIENT_STATUS:
                    logger.warning("http_transient", url=url, status=resp.status_code, attempt=attempt)
                    self._sleep_backoff(attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning("http_error", url=url, error=str(exc), attempt=attempt)
                self._sleep_backoff(attempt)
        raise RuntimeError(f"GET {url} failed after {self.config.max_retries} attempts") from last_exc

    def download(
        self, url: str, dest: Path, *, source: str = "default", max_bytes: int | None = None
    ) -> DownloadResult:
        """Stream ``url`` to ``dest`` with resume + size cap.

        Downloads into ``dest.with_suffix(dest.suffix + '.part')`` so an
        interrupted transfer can resume via a ``Range`` request, then atomically
        renames on success. If ``max_bytes`` is exceeded mid-stream the transfer
        aborts (the caller decides whether to re-fetch at a lower rendition).
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        last_exc: Exception | None = None

        for attempt in range(self.config.max_retries):
            self._bucket(source).take()
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in _TRANSIENT_STATUS:
                        logger.warning("download_transient", url=url, status=resp.status_code, attempt=attempt)
                        self._sleep_backoff(attempt)
                        continue
                    # Server ignored the Range and restarted from 0 -> discard partial.
                    if have and resp.status_code == 200:
                        part.unlink(missing_ok=True)
                        have = 0
                    resp.raise_for_status()

                    mode = "ab" if have else "wb"
                    written = have
                    with part.open(mode) as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            fh.write(chunk)
                            written += len(chunk)
                            if max_bytes is not None and written > max_bytes:
                                raise _SizeCapExceeded(written)

                part.replace(dest)
                return DownloadResult(path=dest, size=written, resumed=bool(have))
            except _SizeCapExceeded:
                part.unlink(missing_ok=True)
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning("download_error", url=url, error=str(exc), attempt=attempt)
                self._sleep_backoff(attempt)
        raise RuntimeError(f"download {url} failed after {self.config.max_retries} attempts") from last_exc


class _SizeCapExceeded(Exception):
    """Internal: raised when a stream exceeds ``max_bytes``."""

    def __init__(self, size: int) -> None:
        super().__init__(f"exceeded size cap at {size} bytes")
        self.size = size


# Public alias so callers can catch the size-cap condition without importing a
# name-mangled private symbol.
SizeCapExceeded = _SizeCapExceeded
