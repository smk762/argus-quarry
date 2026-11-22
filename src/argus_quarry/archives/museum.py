"""Exact object dates from museum APIs for PD12M items (#52). The Met first.

PD12M keeps only the institution and its own S3 copy of each image, but it names that copy
``uuid5(NAMESPACE_URL, <the museum's image URL>)``: the Met object whose ``primaryImage`` (or
one of its ``additionalImages``) hashes to a PD12M filename is the object that image came
from. So matching needs no image download and no fuzzy title search: harvest the Met's
objects, hash their image URLs, join on the filename.

The dates are the museum's catalogue dates (``objectBeginDate`` / ``objectEndDate`` /
``objectDate``) for the **object**, which for a photograph is when it was made, and for a
painting when it was painted. Every column says ``museum``, and the matched object id and
page are kept for provenance.

The harvest is the only write: a resumable JSONL cache and the compiled
``<archive>.museum.parquet`` under ``$QUARRY_HOME/cache/archives/``, both written by
``archives museum``. Every request goes through :class:`~argus_quarry.net.NetClient`
(paced, polite User-Agent).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argus_quarry.net import NetClient, RemoteGone

MET = "met"
MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
#: Rate-limit bucket (``QuarryConfig.rates``). The Met's docs allow 80 requests a second, but its
#: firewall (Imperva) answers 403 once an IP has made a few hundred requests in a short window,
#: at 10/s and at 2/s alike, then lets it back in after a pause. A harvest waits blocks out.
MET_RATE_SOURCE = "met-api"
DEFAULT_RATE = 1.0
#: Seconds a harvest pauses after a block before asking again (``--block-wait``).
DEFAULT_BLOCK_WAIT = 300
#: Consecutive failed requests (retries exhausted) after which a harvest stops rather than burn through ids.
MAX_CONSECUTIVE_FAILURES = 20
#: PD12M's ``collection`` for the Met.
MET_COLLECTION = "Metropolitan Museum of Art"
#: Where the columns say their date came from (facets' ``date_sources``).
DATE_SOURCE = "museum-catalogue"
#: Columns the museum join adds to an archive's items.
COLUMNS = (
    "museum", "museum_object_id", "museum_object_url", "museum_date", "museum_year_begin", "museum_year_end",
)  # fmt: skip
NO_MUSEUM_SQL = (
    "CAST(NULL AS VARCHAR) AS museum, CAST(NULL AS VARCHAR) AS museum_object_id, "
    "CAST(NULL AS VARCHAR) AS museum_object_url, CAST(NULL AS VARCHAR) AS museum_date, "
    "CAST(NULL AS INTEGER) AS museum_year_begin, CAST(NULL AS INTEGER) AS museum_year_end"
)


class MuseumUnavailable(RuntimeError):
    """The museum API answered something this harvester can't read."""


class MuseumBlocked(MuseumUnavailable):
    """The API refused us (403/410, or a firewall page instead of JSON): stop, cache nothing, retry later slower."""


def image_uuid(url: str) -> str:
    """The PD12M S3 filename (minus extension) of the image PD12M fetched from ``url``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, url))


@dataclass(frozen=True)
class MuseumObject:
    object_id: int
    public_domain: bool
    images: tuple[str, ...]
    date: str | None
    year_begin: int | None
    year_end: int | None
    url: str | None

    def record(self) -> dict[str, Any]:
        """The harvest cache line: what the parquet is compiled from (image URLs hashed once, here)."""
        return {
            "id": self.object_id,
            "pd": self.public_domain,
            "images": list(self.images),
            "uuids": [image_uuid(u) for u in self.images],
            "date": self.date,
            "begin": self.year_begin,
            "end": self.year_end,
            "url": self.url,
        }


def parse_met_object(data: dict[str, Any]) -> MuseumObject:
    """A Met ``/objects/{id}`` answer as a :class:`MuseumObject`.

    The Met writes ``0`` / ``0`` for an undated object; with no ``objectDate`` text either,
    that reads as no date rather than the year 0.
    """
    try:
        object_id = int(data["objectID"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MuseumUnavailable(f"Met object answer has no objectID: {sorted(data)[:8]}") from exc
    images = tuple(u for u in [data.get("primaryImage"), *(data.get("additionalImages") or [])] if u)
    text = (data.get("objectDate") or "").strip() or None
    begin, end = data.get("objectBeginDate"), data.get("objectEndDate")
    if not isinstance(begin, int) or not isinstance(end, int) or (text is None and begin == 0 and end == 0):
        begin = end = None
    elif begin > end:
        begin, end = end, begin
    return MuseumObject(
        object_id=object_id,
        public_domain=bool(data.get("isPublicDomain")),
        images=images,
        date=text,
        year_begin=begin,
        year_end=end,
        url=data.get("objectURL") or None,
    )


class MetClient:
    """The Met's open collection API (no key)."""

    def __init__(self, net: NetClient, *, api: str = MET_API) -> None:
        self.net = net
        self.api = api.rstrip("/")

    def object_ids(self) -> list[int]:
        """Every object id in the collection (one request: the paginated search caps at 10,000 hits)."""
        data = self.net.get_json(f"{self.api}/objects", source=MET_RATE_SOURCE)
        ids = data.get("objectIDs")
        if not isinstance(ids, list):
            raise MuseumUnavailable("Met /objects answer has no objectIDs list")
        return sorted({int(i) for i in ids})

    def object(self, object_id: int) -> MuseumObject | None:
        """One object, or None when the Met says it does not exist (404 only).

        A 403/410 is the firewall refusing us, not the object gone: it raises :class:`MuseumBlocked`,
        as does a 200 whose body is not JSON (a challenge page), so nothing is cached as gone.
        """
        try:
            data = self.net.get_json(f"{self.api}/objects/{object_id}", source=MET_RATE_SOURCE)
        except RemoteGone as exc:
            if exc.status == 404:
                return None
            raise MuseumBlocked(f"Met API answered {exc.status} for object {object_id}: slow down (--rate)") from exc
        except ValueError as exc:  # json decode: an HTML page where JSON belongs
            raise MuseumBlocked(f"Met API sent a non-JSON page for object {object_id} (a firewall challenge?)") from exc
        return parse_met_object(data)


# ── files (all under $QUARRY_HOME/cache/archives/) ──────────────────────
def museum_path(index_dir: Path, archive: str) -> Path:
    """The compiled image -> object table the index joins."""
    return index_dir / f"{archive}.museum.parquet"


def cache_path(index_dir: Path, archive: str, museum: str = MET) -> Path:
    """The raw harvest: one JSON line per object asked for (resumable)."""
    return index_dir / f"{archive}.{museum}.jsonl"


def ids_path(index_dir: Path, archive: str, museum: str = MET) -> Path:
    return index_dir / f"{archive}.{museum}-ids.json"


def read_cache(path: Path) -> dict[int, dict[str, Any]]:
    done: dict[int, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                done[int(rec["id"])] = rec
            except (ValueError, KeyError, TypeError):
                continue  # a line cut short by an interrupted write: that id is simply asked again
    return done


def object_ids(client: MetClient, index_dir: Path, archive: str, *, refresh: bool = False) -> list[int]:
    """The collection's object ids, fetched once and cached beside the harvest."""
    path = ids_path(index_dir, archive)
    if not refresh and path.is_file():
        return json.loads(path.read_text())["ids"]
    ids = client.object_ids()
    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(timespec="seconds"), "ids": ids}))
    os.replace(tmp, path)
    return ids


def harvest(
    client: MetClient,
    index_dir: Path,
    archive: str,
    ids: Iterable[int],
    *,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Ask the Met for every id in ``ids`` not yet in the cache (at most ``limit``), appending as it goes.

    A gone object (404) is cached as gone. A request that keeps failing (retries exhausted) is
    left out of the cache, so the next run asks again. A block (:class:`MuseumBlocked`), or
    :data:`MAX_CONSECUTIVE_FAILURES` failures in a row, stops the run with ``blocked`` set: going
    on would only record the block, id after id.
    """
    path = cache_path(index_dir, archive)
    index_dir.mkdir(parents=True, exist_ok=True)
    done = read_cache(path)
    todo = [i for i in ids if i not in done][: limit if limit is not None else None]
    counts = {"cached": len(done), "asked": 0, "gone": 0, "failed": 0, "blocked": 0}
    streak = 0
    with path.open("a") as fh:
        for n, object_id in enumerate(todo, 1):
            try:
                obj = client.object(object_id)
            except MuseumBlocked:
                counts["blocked"] = 1
                break
            except (RuntimeError, MuseumUnavailable):
                counts["failed"] += 1
                streak += 1
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    counts["blocked"] = 1
                    break
                continue
            streak = 0
            fh.write(json.dumps(obj.record() if obj else {"id": object_id, "gone": True}) + "\n")
            fh.flush()
            counts["asked"] += 1
            counts["gone"] += obj is None
            if progress:
                progress(n, len(todo))
    return counts


def harvest_patiently(
    client: MetClient,
    index_dir: Path,
    archive: str,
    ids: list[int],
    *,
    limit: int | None = None,
    block_wait: float = DEFAULT_BLOCK_WAIT,
    max_blocks: int = 50,
    progress: Callable[[int, int], None] | None = None,
    on_block: Callable[[int, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, int]:
    """:func:`harvest`, pausing ``block_wait`` seconds after each block and resuming from the cache.

    ``block_wait <= 0`` stops at the first block instead. Gives up (``blocked`` set) after
    ``max_blocks`` blocks, so a firewall that never lets go doesn't keep it waiting forever.
    """
    import time

    sleep = sleep or time.sleep
    total = {"cached": len(read_cache(cache_path(index_dir, archive))), "asked": 0, "gone": 0, "failed": 0}
    blocks = 0
    while True:
        remaining = None if limit is None else limit - total["asked"]
        if remaining is not None and remaining <= 0:
            return {**total, "blocked": 0, "blocks": blocks}
        counts = harvest(client, index_dir, archive, ids, limit=remaining, progress=progress)
        for k in ("asked", "gone", "failed"):
            total[k] += counts[k]
        if not counts["blocked"]:
            return {**total, "blocked": 0, "blocks": blocks}
        blocks += 1
        if block_wait <= 0 or blocks >= max_blocks:
            return {**total, "blocked": 1, "blocks": blocks}
        if on_block:
            on_block(blocks, block_wait)
        sleep(block_wait)


def compile_table(index_dir: Path, archive: str) -> dict[str, int]:
    """Compile the harvest into ``<archive>.museum.parquet`` (image uuid -> object), atomically.

    Only public-domain objects with images count. An image shared by two objects keeps the
    lower object id, so the join stays one row per image.
    """
    import duckdb

    src = cache_path(index_dir, archive)
    out = museum_path(index_dir, archive)
    if not src.is_file():
        raise MuseumUnavailable(f"no harvest at {src}: run `argus-quarry archives museum {archive}` first")
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
                SELECT image_uuid,
                       '{MET}' AS museum,
                       arg_min(CAST(id AS VARCHAR), id) AS museum_object_id,
                       arg_min(url, id) AS museum_object_url,
                       arg_min(date, id) AS museum_date,
                       CAST(arg_min("begin", id) AS INTEGER) AS museum_year_begin,
                       CAST(arg_min("end", id) AS INTEGER) AS museum_year_end
                FROM (
                    SELECT id, url, date, "begin", "end", unnest(uuids) AS image_uuid
                    FROM read_json(?, format = 'newline_delimited',
                                   columns = {{'id': 'BIGINT', 'pd': 'BOOLEAN', 'uuids': 'VARCHAR[]', 'date': 'VARCHAR',
                                              'begin': 'BIGINT', 'end': 'BIGINT', 'url': 'VARCHAR'}})
                    WHERE pd AND uuids IS NOT NULL
                )
                GROUP BY image_uuid
            ) TO '{tmp}' (FORMAT parquet)
            """,
            [str(src)],
        )
        images, objects = con.execute(
            f"SELECT count(*), count(DISTINCT museum_object_id) FROM read_parquet('{tmp}')"
        ).fetchone()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    os.replace(tmp, out)
    return {"images": images, "objects": objects}


def join_sql(path: Path) -> str:
    """A SELECT over the compiled table for an archive's index build to LEFT JOIN on ``image_uuid``."""
    quoted = "'" + str(path).replace("'", "''") + "'"
    return f"SELECT image_uuid, {', '.join(COLUMNS)} FROM read_parquet({quoted})"
