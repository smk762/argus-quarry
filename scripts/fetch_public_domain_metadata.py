#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["huggingface_hub>=0.34"]
# ///
"""Archive the metadata of two large public-domain image datasets — no photos.

Downloads links, captions, per-image metadata and (optionally) CLIP embeddings
so slices of the actual images can be fetched later, on demand:

  pd12m     Spawning/PD12M — 12.4M CC0 / Public Domain Mark images from 640
            institutions. Parquet with url, caption, width, height, mime_type,
            hash, license and source; images live in the public s3://pd12m bucket.
              metadata    ~2.3 GB   parquet + datasheet + tutorials
              embeddings  ~38 GB    CLIP ViT-L/14, 13 x (1M x 768 float32) .npy

  megalith  madebyollin/megalith-10m — 9.58M Flickr links marked CC0, Public
            Domain Mark, Flickr Commons or US Gov. Shuffled; no structure beyond
            the owner account in url_source.
              links       ~0.75 GB  url, url_highres, url_source
              captions    ~10 GB    Florence-2 captions for 8.94M links
                                    (aipicasso/megalith-10m-florence2)
              drawthings  ~1.7 GB   per-shard parquet (width, height, exif,
                                    sha256) for the ~9.4M images archived as
                                    1024px tars in drawthingsai/megalith-10m
                                    (959 shards, ~1.9 TB); the tars are skipped

Output goes to <root>/<source>/<part>/, plus <root>/<source>/SOURCE.json
recording the exact dataset revisions fetched. Re-running resumes and skips
files already complete.

    uv run scripts/fetch_public_domain_metadata.py --dry-run
    HF_TOKEN=hf_... uv run scripts/fetch_public_domain_metadata.py
    uv run scripts/fetch_public_domain_metadata.py --source pd12m --no-embeddings
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = "/media/photos/public_domain"


@dataclass(frozen=True)
class Part:
    """One Hugging Face dataset repo (or a pattern-selected slice of one)."""

    name: str
    repo_id: str
    allow: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    note: str = ""


#: Photos and the shards that hold them. Never fetched, whatever a part's own patterns say: this
#: script archives metadata only, and photos land one at a time through ingest's licence gate.
#: huggingface_hub matches with case-sensitive ``fnmatch``, so each extension is listed in both cases.
NEVER_FETCH: tuple[str, ...] = tuple(
    f"*.{e}"
    for ext in ("tar", "jpg", "jpeg", "png", "webp", "gif", "tif", "tiff", "bmp", "avif", "heic")
    for e in (ext, ext.upper())
)


def ignore_patterns(part: Part) -> list[str]:
    """What *part* must not fetch: ``.gitattributes``, its own ignores, and every photo or shard."""
    return [".gitattributes", *(part.ignore or ()), *NEVER_FETCH]


SOURCES: dict[str, dict[str, Part]] = {
    "pd12m": {
        "metadata": Part(
            "metadata",
            "Spawning/PD12M",
            ignore=("embeddings/*",),
            note="parquet: url, caption, width, height, mime_type, hash, license, source",
        ),
        "embeddings": Part(
            "embeddings",
            "Spawning/PD12M",
            allow=("embeddings/*",),
            note="CLIP ViT-L/14 image embeddings, row-aligned with the metadata",
        ),
    },
    "megalith": {
        "links": Part("links", "madebyollin/megalith-10m", note="url, url_highres, url_source"),
        "captions": Part(
            "captions", "aipicasso/megalith-10m-florence2", note="Florence-2 captions keyed by url_source"
        ),
        "drawthings": Part(
            "drawthings",
            "drawthingsai/megalith-10m",
            allow=("*.parquet", "*.json"),
            note="metadata of the ~9.4M images archived as 1024px tars in this repo (tars not fetched)",
        ),
    },
}


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def selected_parts(args: argparse.Namespace) -> list[tuple[str, Part]]:
    chosen: list[tuple[str, Part]] = []
    for source in args.source:
        for name, part in SOURCES[source].items():
            if source == "pd12m" and name == "embeddings" and not args.embeddings:
                continue
            if source == "megalith" and name not in args.megalith_parts:
                continue
            chosen.append((source, part))
    return chosen


def plan(api, part: Part, token: str | None) -> tuple[str, list[tuple[str, int]]]:
    """Resolve the repo's current commit and the files the patterns select."""
    from huggingface_hub.utils import filter_repo_objects

    info = api.dataset_info(part.repo_id, token=token)
    tree = [
        f
        for f in api.list_repo_tree(part.repo_id, repo_type="dataset", recursive=True, revision=info.sha, token=token)
        if getattr(f, "size", None) is not None
    ]
    keep = set(
        filter_repo_objects(
            [f.path for f in tree],
            allow_patterns=list(part.allow) if part.allow else None,
            ignore_patterns=ignore_patterns(part),
        )
    )
    return info.sha, [(f.path, f.size) for f in tree if f.path in keep]


def mount_point(path: Path) -> Path:
    """The mount point of the filesystem holding ``path`` (its nearest existing ancestor)."""
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    while not os.path.ismount(probe):
        probe = probe.parent
    return probe


def check_root(root: Path, create: bool, allow_root_fs: bool = False) -> None:
    """Refuse to silently fill the wrong disk when the NAS isn't mounted."""
    probe = root
    while not probe.exists():
        probe = probe.parent
    if probe != root and not create:
        sys.exit(
            f"error: {root} does not exist (nearest existing parent: {probe}). "
            "Is the NAS mounted? Pass --create-root to create it anyway, or --root elsewhere."
        )
    # An unmounted NAS usually leaves its (empty, writable) mount-point directory on the
    # local disk, so existence alone proves nothing: check what filesystem `root` is on.
    if mount_point(root) == Path("/") and not allow_root_fs:
        sys.exit(
            f"error: {root} is on the root filesystem, not a mounted volume. "
            "Is the NAS mounted? Pass --allow-root-fs to write there anyway."
        )
    if not os.access(probe, os.W_OK):
        sys.exit(f"error: {probe} is not writable by this user")


def _current_umask() -> int:
    """The process umask, read without the racy set-and-restore of ``os.umask`` where Linux allows."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _manifest_mode(path: Path) -> int:
    """The mode SOURCE.json should keep: its current one, or what a plain ``open()`` would give a new file.

    ``mkstemp`` creates 0600; installing that inode as-is would make the manifest owner-only
    for every other account (API service, curator, mirror sync) that reads it.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o666 & ~_current_umask()


def write_manifest(root: Path, source: str, records: list[dict]) -> None:
    """Merge ``records`` into ``<root>/<source>/SOURCE.json``, atomically and under a lock.

    The read-merge-write holds an exclusive ``flock`` so overlapping runs can't drop each
    other's parts, and the new manifest is fsynced to a temp file then ``os.replace``d, so
    an interruption leaves the previous manifest intact rather than a truncated one.
    """
    path = root / source / "SOURCE.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(".SOURCE.json.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = json.loads(path.read_text()) if path.exists() else {"source": source, "parts": {}}
        for rec in records:
            existing["parts"][rec["part"]] = rec
        existing["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".SOURCE.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(existing, indent=2) + "\n")
                fh.flush()
                os.fchmod(fh.fileno(), _manifest_mode(path))
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)  # make the rename itself durable
            finally:
                os.close(dir_fd)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def main() -> int:
    p = argparse.ArgumentParser(
        description="Archive PD12M / Megalith-10m metadata (links, captions, embeddings) — no photos.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--root", type=Path, default=Path(DEFAULT_ROOT), help="output root; files go to <root>/<source>/<part>/"
    )
    p.add_argument("--source", nargs="+", choices=sorted(SOURCES), default=sorted(SOURCES), help="datasets to fetch")
    p.add_argument(
        "--embeddings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include PD12M's ~38 GB of CLIP ViT-L/14 embeddings",
    )
    p.add_argument(
        "--megalith-parts",
        nargs="+",
        choices=sorted(SOURCES["megalith"]),
        default=sorted(SOURCES["megalith"]),
        help="which Megalith companions to fetch",
    )
    p.add_argument(
        "--hf-token",
        default=None,  # read after parsing: as a default, --help would print the secret
        help="Hugging Face token (default: $HF_TOKEN). Optional, but avoids anonymous rate limits on large pulls",
    )
    p.add_argument("--max-workers", type=int, default=8, help="parallel file downloads per part")
    p.add_argument("--dry-run", action="store_true", help="list what would be fetched, with sizes, and exit")
    p.add_argument("--create-root", action="store_true", help="create --root if it does not exist")
    p.add_argument(
        "--allow-root-fs",
        action="store_true",
        help="allow --root on the root filesystem (by default that means the NAS is not mounted)",
    )
    args = p.parse_args()

    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    token = args.hf_token or os.environ.get("HF_TOKEN") or None
    parts = selected_parts(args)

    planned = []
    grand = 0
    for source, part in parts:
        sha, files = plan(api, part, token)
        total = sum(size for _, size in files)
        grand += total
        planned.append((source, part, sha, files, total))
        print(
            f"{source}/{part.name:<11} {part.repo_id}@{sha[:10]}  {len(files):>5} files  {human(total):>9}  {part.note}"
        )
    print(f"{'total':<20} {human(grand)} -> {args.root}")

    if args.dry_run:
        return 0

    check_root(args.root, args.create_root, args.allow_root_fs)
    by_source: dict[str, list[dict]] = {}
    for source, part, sha, files, total in planned:
        dest = args.root / source / part.name
        dest.mkdir(parents=True, exist_ok=True)
        print(f"\n==> {source}/{part.name}: {len(files)} files, {human(total)} into {dest}")
        snapshot_download(
            repo_id=part.repo_id,
            repo_type="dataset",
            revision=sha,
            local_dir=dest,
            allow_patterns=list(part.allow) if part.allow else None,
            ignore_patterns=ignore_patterns(part),
            max_workers=args.max_workers,
            token=token,
        )
        by_source.setdefault(source, []).append(
            {
                "part": part.name,
                "repo_id": part.repo_id,
                "revision": sha,
                "n_files": len(files),
                "bytes": total,
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "note": part.note,
            }
        )
        write_manifest(args.root, source, by_source[source])
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
