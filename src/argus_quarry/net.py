"""HTTP layer — polite, rate-limited, retrying, resumable.

A single :class:`NetClient` wraps ``httpx`` with the things every downloader
needs: a descriptive ``User-Agent``, a per-source token-bucket rate limit,
exponential backoff with jitter on transient errors, and resumable streaming
downloads (HTTP ``Range`` continuation of a ``.part`` file in the cache).
"""

from __future__ import annotations

import random
import re
import threading
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
        self._lock = threading.Lock()  # shard scans share one bucket across worker threads

    def take(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


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

        A ``#bytes=START-END`` fragment (see :func:`byte_range_url`) fetches only
        that inclusive span, e.g. one member of a tar shard; the server must answer
        ``206`` for exactly that span, or the transfer is refused rather than
        streaming the whole file. ``403``/``404``/``410`` raise :class:`RemoteGone`
        at once: retrying a deleted or private photo only spends the rate limit.
        """
        url, span = split_byte_range(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        last_exc: Exception | None = None

        for attempt in range(self.config.max_retries):
            self._bucket(source).take()
            have = part.stat().st_size if part.exists() else 0
            if span is not None:
                first, last = span
                if have > last - first + 1:  # a stale .part longer than the span: start over
                    part.unlink()
                    have = 0
                headers = {"Range": f"bytes={first + have}-{last}"}
            else:
                headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in _TRANSIENT_STATUS:
                        logger.warning("download_transient", url=url, status=resp.status_code, attempt=attempt)
                        self._sleep_backoff(attempt)
                        continue
                    if resp.status_code in GONE_STATUS:
                        raise RemoteGone(url, resp.status_code)
                    if span is not None:
                        _check_span(resp, span[0] + have, span[1])
                    # Server ignored the Range and restarted from 0 -> discard partial.
                    elif have and resp.status_code == 200:
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

                if span is not None and written != span[1] - span[0] + 1:
                    raise httpx.TransportError(f"short range read: {written} of {span[1] - span[0] + 1} bytes")
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

    def read_range(self, url: str, start: int, length: int, *, source: str = "default") -> bytes:
        """Read ``length`` bytes at ``start``; fewer (or ``b""``) past the end of the file.

        For index scans that read many small spans of one large file. Raises
        :class:`RemoteGone` on ``403``/``404``/``410`` (a signed URL that expired,
        say) so the caller can re-resolve it.
        """
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            self._bucket(source).take()
            try:
                with self._client.stream("GET", url, headers={"Range": f"bytes={start}-{start + length - 1}"}) as resp:
                    if resp.status_code in _TRANSIENT_STATUS:
                        logger.warning("range_transient", url=url, status=resp.status_code, attempt=attempt)
                        self._sleep_backoff(attempt)
                        continue
                    if resp.status_code == 416:  # starts past the end of the file
                        return b""
                    if resp.status_code in GONE_STATUS:
                        raise RemoteGone(url, resp.status_code)
                    _check_span(resp, start, None)
                    resp.raise_for_status()
                    data = b"".join(resp.iter_bytes(chunk_size=65536))
                return data[:length]
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning("range_error", url=url, error=str(exc), attempt=attempt)
                self._sleep_backoff(attempt)
        raise RuntimeError(f"range read {url} failed after {self.config.max_retries} attempts") from last_exc

    def resolve_redirect(self, url: str, *, source: str = "default") -> str:
        """The URL ``url`` redirects to (one hop), or ``url`` itself when it does not redirect.

        Hugging Face's ``resolve/`` URLs 302 to a signed CDN URL; resolving once per
        shard keeps thousands of range reads off the rate-limited resolver.
        """
        self._bucket(source).take()
        resp = self._client.head(url, follow_redirects=False)
        if resp.status_code in GONE_STATUS:
            raise RemoteGone(url, resp.status_code)
        if resp.is_redirect and resp.headers.get("location"):
            return str(resp.url.join(resp.headers["location"]))
        resp.raise_for_status()
        return url


#: Statuses that mean "these bytes are not here": deleted, gone, or private.
GONE_STATUS = frozenset({403, 404, 410})


class RemoteGone(RuntimeError):
    """The server says the bytes are not available (``403``/``404``/``410``); retrying will not help."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status


_RANGE_FRAGMENT = re.compile(r"#bytes=(\d+)-(\d+)$")


def byte_range_url(url: str, start: int, length: int) -> str:
    """``url`` plus a ``#bytes=START-END`` fragment (inclusive, like HTTP ``Range``).

    The fragment is never sent to the server; :meth:`NetClient.download` reads it
    back so a recorded ``remote_url`` re-fetches exactly the same bytes.
    """
    return f"{url}#bytes={start}-{start + length - 1}"


def split_byte_range(url: str) -> tuple[str, tuple[int, int] | None]:
    """``(url without the fragment, (first, last))``, or ``(url, None)`` without one."""
    m = _RANGE_FRAGMENT.search(url)
    if m is None:
        return url, None
    first, last = int(m.group(1)), int(m.group(2))
    if last < first:
        raise ValueError(f"empty byte range in {url}")
    return url[: m.start()], (first, last)


def _check_span(resp: httpx.Response, first: int, last: int | None) -> None:
    """Refuse a response that is not a ``206`` for exactly the requested span.

    A ``200`` means the server ignored ``Range`` and is about to send the whole
    file (a ~1.9 GB shard); that must never be streamed.
    """
    if resp.status_code != 206:
        if resp.status_code in GONE_STATUS or resp.is_error:
            return  # raise_for_status reports it
        raise httpx.TransportError(f"expected 206 for a byte range, got {resp.status_code}")
    m = re.match(r"bytes (\d+)-(\d+)/", resp.headers.get("content-range", ""))
    if m is None or int(m.group(1)) != first or (last is not None and int(m.group(2)) != last):
        raise httpx.TransportError(f"unexpected Content-Range {resp.headers.get('content-range')!r}")


class _SizeCapExceeded(Exception):
    """Internal: raised when a stream exceeds ``max_bytes``."""

    def __init__(self, size: int) -> None:
        super().__init__(f"exceeded size cap at {size} bytes")
        self.size = size


# Public alias so callers can catch the size-cap condition without importing a
# name-mangled private symbol.
SizeCapExceeded = _SizeCapExceeded
