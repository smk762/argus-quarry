"""Megalith-10m's Drawthings mirror: 1024px JPEGs in tar shards, read one member at a time.

Flickr photos that owners have since deleted or made private return 404/410 (or
403). ``drawthingsai/megalith-10m`` holds ~9.4M of the 9.58M images as 1024px
JPEGs in 959 tar shards (~1.9 GB each). Hugging Face serves byte ranges, so one
image costs one ranged GET once its member's offset in the shard is known.

The offset index is built here, under ``$QUARRY_HOME/cache/archives/``:

    megalith-mirror/REVISION       the dataset revision the offsets belong to
    megalith-mirror/shards/N.tsv   key, offset, size of every image member of shard N
    megalith-mirror.duckdb         members(url, shard, key, offset, size), the lookup table

Scanning a shard walks its tar headers with range reads (about one ~8 KB read
per sample: each sample is a ``.jpg``, ``.json`` and ``.txt`` member, each with
a PAX header in front). A shard's ``.tsv`` is written atomically once the whole
shard is scanned, so an interrupted build resumes at the first unscanned shard.
The lookup table joins those offsets to the archived ``drawthings/*.parquet``
(``url`` -> ``key`` per shard) and is rebuilt atomically after every run.

The offsets are only valid for one revision of the dataset, so the revision is
pinned from the ``SOURCE.json`` the metadata script wrote, and shards scanned
for another revision are discarded.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from argus_quarry.archives.base import ArchiveUnavailable, _duckdb, sql_path
from argus_quarry.config import QuarryConfig
from argus_quarry.net import NetClient, RemoteGone, byte_range_url

logger = structlog.get_logger()

MIRROR_REPO = "drawthingsai/megalith-10m"
#: Rate-limit bucket for the mirror (``NetClient`` paces each source separately).
RATE_SOURCE = "megalith-mirror"
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
#: Bytes per range read while scanning headers: covers a sample's small .json/.txt
#: members and the next image's headers, so a sample costs about one request.
SCAN_WINDOW = 8192

_BLOCK = 512


def shard_url(revision: str, shard: str) -> str:
    """The pinned Hugging Face URL of one tar shard."""
    return f"https://huggingface.co/datasets/{MIRROR_REPO}/resolve/{revision}/{shard}.tar"


# ── tar header scan ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TarMember:
    name: str
    offset: int  # of the member's data, not its header
    size: int


def _octal(field: bytes) -> int:
    if field and field[0] & 0x80:  # GNU base-256 for sizes past 8 GB
        return int.from_bytes(field[1:], "big")
    text = field.rstrip(b"\0 ").strip()
    return int(text, 8) if text else 0


def _checksum_ok(header: bytes) -> bool:
    try:
        stored = _octal(header[148:156])
    except ValueError:  # not octal: not a header at all (a misaligned read)
        return False
    unsigned = sum(header[:148]) + 8 * 32 + sum(header[156:])
    return stored == unsigned


def _pax_records(data: bytes) -> dict[str, str]:
    """``"<len> key=value\\n"`` records of a PAX extended header."""
    out: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        space = data.find(b" ", pos)
        if space < 0:
            break
        try:
            length = int(data[pos:space])
        except ValueError:
            break
        if length <= 0:
            break
        key, _, value = data[space + 1 : pos + length - 1].partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        pos += length
    return out


def scan_tar(read: Callable[[int, int], bytes], *, window: int = SCAN_WINDOW) -> Iterator[TarMember]:
    """Yield every regular-file member of a tar, reading only headers.

    ``read(offset, length)`` returns up to ``length`` bytes at ``offset`` (fewer,
    or ``b""``, at the end of the file). Reads are made ``window`` bytes at a
    time and member data is skipped, never read, so a remote scan costs about
    one request per member whose data outgrows the window.
    """
    buf_start, buf = 0, b""

    def get(pos: int, n: int) -> bytes:
        nonlocal buf_start, buf
        if not (buf_start <= pos and pos + n <= buf_start + len(buf)):
            buf_start, buf = pos, read(pos, max(window, n))
        return buf[pos - buf_start : pos - buf_start + n]

    pos = 0
    pending: dict[str, str] = {}  # PAX / GNU long-name overrides for the next member
    while True:
        header = get(pos, _BLOCK)
        if len(header) < _BLOCK or header == bytes(_BLOCK):
            return  # end of archive (two zero blocks) or end of file
        if not _checksum_ok(header):
            raise ValueError(f"bad tar header checksum at offset {pos}")
        size = _octal(header[124:136])
        typeflag = header[156:157]
        data = pos + _BLOCK
        if typeflag == b"x":
            pending.update(_pax_records(get(data, size)))
        elif typeflag == b"L":
            pending["path"] = get(data, size).rstrip(b"\0").decode("utf-8", "replace")
        elif typeflag in (b"0", b"\0", b"7"):
            name = header[:100].rstrip(b"\0").decode("utf-8", "replace")
            if header[257:262] == b"ustar" and header[345:500].strip(b"\0"):
                name = header[345:500].rstrip(b"\0").decode("utf-8", "replace") + "/" + name
            size = int(pending.get("size", size))
            yield TarMember(pending.get("path", name), data, size)
            pending = {}
        elif typeflag != b"g":  # directories, links, …: nothing to fetch
            pending = {}
        pos = data + -(-size // _BLOCK) * _BLOCK


# ── offset index ─────────────────────────────────────────────────────


class MegalithMirror:
    """Builds and reads the member-offset index for Megalith's Drawthings mirror."""

    def __init__(self, config: QuarryConfig) -> None:
        self.config = config

    # ── locations ─────────────────────────────────────────────────────
    @property
    def root(self) -> Path | None:
        return self.config.archive_dirs.get("megalith")

    @property
    def work_dir(self) -> Path:
        return self.config.archive_index_dir / "megalith-mirror"

    @property
    def shards_dir(self) -> Path:
        return self.work_dir / "shards"

    @property
    def db_path(self) -> Path:
        return self.config.archive_index_dir / "megalith-mirror.duckdb"

    def _drawthings(self) -> Path:
        root = self.root
        if root is None or not (root / "drawthings").is_dir():
            raise ArchiveUnavailable(
                "megalith mirror: set QUARRY_MEGALITH_DIR to archived metadata that includes drawthings/"
            )
        return root / "drawthings"

    def revision(self) -> str:
        """The dataset revision the archived ``drawthings/`` metadata was fetched at."""
        manifest = self._drawthings().parent / "SOURCE.json"
        try:
            revision = json.loads(manifest.read_text())["parts"]["drawthings"]["revision"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArchiveUnavailable(
                f"megalith mirror: no drawthings revision in {manifest}; re-run "
                "scripts/fetch_public_domain_metadata.py --source megalith --megalith-parts drawthings"
            ) from exc
        if not isinstance(revision, str) or not revision:
            raise ArchiveUnavailable(f"megalith mirror: empty drawthings revision in {manifest}")
        return revision

    def shards(self) -> list[str]:
        """Every shard the archived metadata lists (``00000`` … ``00958``)."""
        return sorted(p.stem for p in self._drawthings().glob("*.parquet") if p.stem.isdigit())

    def scanned(self) -> set[str]:
        return {p.stem for p in self.shards_dir.glob("*.tsv")}

    # ── build (CLI-driven; writes only under $QUARRY_HOME) ────────────
    def scan_shard(self, net: NetClient, shard: str, revision: str) -> int:
        """Walk one shard's tar headers and write its offsets; returns the image-member count."""
        url = shard_url(revision, shard)
        cdn = net.resolve_redirect(url, source=RATE_SOURCE)

        def read(offset: int, length: int) -> bytes:
            nonlocal cdn
            try:
                return net.read_range(cdn, offset, length, source=RATE_SOURCE)
            except RemoteGone:  # the signed CDN URL expired mid-shard: resolve it again
                cdn = net.resolve_redirect(url, source=RATE_SOURCE)
                return net.read_range(cdn, offset, length, source=RATE_SOURCE)

        rows = [
            (Path(m.name).stem, m.offset, m.size)
            for m in scan_tar(read)
            if Path(m.name).suffix.lower() in IMAGE_SUFFIXES
        ]
        self.shards_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.shards_dir / f".{shard}.{os.getpid()}-{uuid.uuid4().hex[:8]}.part"
        tmp.write_text("key\toffset\tsize\n" + "".join(f"{k}\t{o}\t{s}\n" for k, o, s in rows))
        os.replace(tmp, self.shards_dir / f"{shard}.tsv")
        return len(rows)

    def build(
        self,
        net: NetClient,
        *,
        shards: Iterable[str] | None = None,
        workers: int = 4,
        progress: Callable[[str, int | None, str | None], None] | None = None,
    ) -> dict[str, Any]:
        """Scan every shard not yet scanned (resumable), then rebuild the lookup table.

        ``progress(shard, members, error)`` is called as each shard finishes. A shard
        that fails is reported and left for the next run; it never aborts the others.
        """
        revision = self.revision()
        known = self.shards()
        wanted = known if shards is None else sorted(set(shards) & set(known))
        marker = self.work_dir / "REVISION"
        if marker.is_file() and marker.read_text().strip() != revision:
            logger.warning("mirror_revision_changed", old=marker.read_text().strip(), new=revision)
            shutil.rmtree(self.shards_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(revision + "\n")

        done = self.scanned()
        todo = [s for s in wanted if s not in done]
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.scan_shard, net, s, revision): s for s in todo}
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    n = future.result()
                except Exception as exc:  # network, or a malformed shard: retry next run
                    failed.append(shard)
                    logger.warning("mirror_shard_failed", shard=shard, error=str(exc))
                    if progress:
                        progress(shard, None, str(exc))
                else:
                    if progress:
                        progress(shard, n, None)
        members = self.consolidate()
        return {
            "revision": revision,
            "shards": len(known),
            "scanned": len(self.scanned() & set(known)),
            "this_run": len(todo) - len(failed),
            "failed": sorted(failed),
            "members": members,
        }

    def consolidate(self) -> int:
        """Rebuild the ``members`` lookup table from the scanned shards, atomically."""
        revision = self.revision()
        drawthings = self._drawthings()
        duckdb = _duckdb()
        self.config.archive_index_dir.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp = self.config.archive_index_dir / f".megalith-mirror.{token}.building.duckdb"
        con = duckdb.connect(str(tmp))
        built = False
        try:
            if any(self.shards_dir.glob("*.tsv")):
                offsets = f"""
                    SELECT regexp_extract(filename, '([0-9]+)\\.tsv$', 1) AS shard, key, "offset", size
                    FROM read_csv({sql_path(self.shards_dir / "*.tsv")}, header = true, delim = '\t',
                                  filename = true,
                                  columns = {{'key': 'VARCHAR', 'offset': 'BIGINT', 'size': 'BIGINT'}})
                """
            else:
                offsets = (
                    'SELECT NULL::VARCHAR AS shard, NULL::VARCHAR AS key, NULL::BIGINT AS "offset", '
                    "NULL::BIGINT AS size WHERE false"
                )
            # A url archived twice (two shards) keeps one copy: the lowest shard, then key.
            con.execute(f"""
                CREATE TABLE members AS
                SELECT DISTINCT ON (p.url) p.url, o.shard, o.key, o."offset", o.size
                FROM ({offsets}) o
                JOIN (
                    SELECT url, CAST(key AS VARCHAR) AS key,
                           regexp_extract(filename, '([0-9]+)\\.parquet$', 1) AS shard
                    FROM read_parquet({sql_path(drawthings / "*.parquet")}, filename = true, union_by_name = true)
                    WHERE status = 'success'
                ) p ON p.shard = o.shard AND p.key = o.key
                ORDER BY p.url, o.shard, o.key
            """)
            con.execute(
                "CREATE TABLE meta AS SELECT ? AS repo, ? AS revision, ? AS shards, now() AS built_at",
                [MIRROR_REPO, revision, len(self.scanned())],
            )
            n = con.execute("SELECT count(*) FROM members").fetchone()[0]
            built = True
        except duckdb.Error as exc:
            raise ArchiveUnavailable(f"megalith mirror: index build failed: {exc}") from exc
        finally:
            con.close()
            if not built:
                tmp.unlink(missing_ok=True)
                tmp.with_name(tmp.name + ".wal").unlink(missing_ok=True)
        os.replace(tmp, self.db_path)
        return n

    # ── read path ─────────────────────────────────────────────────────
    def lookup(self, urls: Iterable[str]) -> dict[str, str]:
        """``Flickr url -> mirror URL with its #bytes= range``, for the urls the mirror holds.

        Empty when no offset index has been built: a slice then simply has no fallback.
        """
        urls = list(dict.fromkeys(urls))
        if not urls or not self.db_path.is_file():
            return {}
        duckdb = _duckdb()
        try:
            with duckdb.connect(str(self.db_path), read_only=True) as con:
                revision = con.execute("SELECT revision FROM meta").fetchone()[0]
                rows = con.execute(
                    f'SELECT url, shard, "offset", size FROM members WHERE url IN ({", ".join("?" * len(urls))})',
                    urls,
                ).fetchall()
        except duckdb.Error as exc:
            raise ArchiveUnavailable(
                f"megalith mirror: index is unreadable ({exc}) — rebuild with `argus-quarry archives mirror-index`"
            ) from exc
        return {url: byte_range_url(shard_url(revision, shard), offset, size) for url, shard, offset, size in rows}

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"repo": MIRROR_REPO, "indexed": self.db_path.is_file(), "members": None}
        try:
            info["revision"] = self.revision()
            info["shards"] = len(self.shards())
        except ArchiveUnavailable:
            info["revision"], info["shards"] = None, 0
        info["scanned"] = len(self.scanned())
        if info["indexed"]:
            try:
                with _duckdb().connect(str(self.db_path), read_only=True) as con:
                    info["members"] = con.execute("SELECT count(*) FROM members").fetchone()[0]
            except Exception:  # half-written or foreign file
                info["indexed"] = False
        return info
