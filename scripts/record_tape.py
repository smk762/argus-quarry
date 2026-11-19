"""Record a real Commons run as the hosted demo's tape (quarry.dragonhound.dev).

    # 1. Run quarry's own harvest + ingest over the demo subjects, against Wikimedia Commons,
    #    into a throwaway pool. Writes run.json and a 640 px WebP of every landed file.
    uv run --no-sync python scripts/record_tape.py record --subjects docs/demo/subjects.yaml --limit 10

    # 2. Turn that recording into the static site's tape (web/public/tape/). Offline.
    uv run --no-sync python scripts/record_tape.py build

The recording lives outside the repo, in $QUARRY_DEMO_CACHE (default ~/.cache/dh-demo/quarry):
the pool under home/, run.json, and thumbs/. Only `build`'s output is committed.

`record` drives the same code `argus-quarry fetch --source commons` runs: CommonsDownloader.harvest
for each subject, then IngestEngine.ingest_record for each candidate. It subclasses both only to
write down what they saw (the Commons file's mime type and original size, the search string,
the decoded size before the megapixel cap); it changes no decision. It then runs the same harvest
a second time over the same pool, to record that a rerun downloads nothing.

What the tape never holds (scripts/demo_tape.py enforces it, tests/test_demo_tape.py tests it):
an identity subject; an image file for anything that did not land with a PD or CC0 licence;
an absolute path, private address or email address. Every candidate the run saw is on the tape.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import yaml
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_tape  # noqa: E402

from argus_quarry import __version__ as QUARRY_VERSION  # noqa: E402
from argus_quarry.config import DEFAULT_USER_AGENT, QuarryConfig  # noqa: E402
from argus_quarry.downloaders.commons import CommonsDownloader, _clean, _meta  # noqa: E402
from argus_quarry.ingest import BudgetReached, IngestEngine  # noqa: E402
from argus_quarry.models import Subject  # noqa: E402
from argus_quarry.net import NetClient  # noqa: E402
from argus_quarry.store import ProvenanceStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = Path(os.environ.get("QUARRY_DEMO_CACHE") or Path.home() / ".cache" / "dh-demo" / "quarry")
DEFAULT_OUT = ROOT / "web" / "public" / "tape"
THUMB_PX = 640


class ObservedCommons(CommonsDownloader):
    """CommonsDownloader that notes what each search returned, including the files it skips."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.query: str | None = None
        self.info: dict[str, dict] = {}
        self.skipped: list[dict] = []

    def _search(self, subject, query, wanted):
        self.query = query
        yield from super()._search(subject, query, wanted)

    def _record(self, subject, page):
        record = super()._record(subject, page)
        info = (page.get("imageinfo") or [{}])[0]
        seen = {
            "query": self.query,
            "rank": page.get("index"),
            "mime": info.get("mime"),
            "width": info.get("width"),
            "height": info.get("height"),
            "bytes": info.get("size"),
        }
        if record is not None:
            self.info.setdefault(record.remote_url, seen)
        elif info:
            ext = info.get("extmetadata") or {}
            raw = _meta(ext, "LicenseShortName") or _meta(ext, "License") or _meta(ext, "UsageTerms")
            artist = _clean(_meta(ext, "Artist"))
            self.skipped.append(
                {
                    **seen,
                    "title": (page.get("title") or "").removeprefix("File:") or None,
                    "source_url": info.get("descriptionurl"),
                    "remote_url": info.get("url"),
                    "licence": (raw or "unknown").strip(),
                    "attribution": artist or _clean(_meta(ext, "Credit")),
                    "photographer": artist,
                }
            )
        return record


class ObservedEngine(IngestEngine):
    """IngestEngine that notes the downloaded file's size and decoded dimensions before the cap."""

    original: dict | None = None

    def _verify_and_cap(self, src, ext):
        try:
            with Image.open(src) as im:
                self.original = {
                    "width": im.width,
                    "height": im.height,
                    "format": im.format,
                    "bytes": src.stat().st_size,
                }
        except Exception:
            self.original = {"bytes": src.stat().st_size if src.exists() else None}
        return super()._verify_and_cap(src, ext)


def _load_subjects(path: Path) -> list[Subject]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    subjects = [Subject(**entry) for entry in data["subjects"]]
    for subject in subjects:
        if subject.category == "identity":
            raise SystemExit(f"record: {subject.name} is an identity subject; the demo records none")
    return subjects


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def _versions() -> dict:
    def ver(name: str) -> str | None:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None

    return {
        "argus_quarry": QUARRY_VERSION,
        "commit": _git("rev-parse", "HEAD"),
        "src_modified": bool(_git("status", "--porcelain", "--", "src")),
        "python": platform.python_version(),
        "packages": {name: ver(name) for name in ("httpx", "pillow", "pydantic", "pyyaml")},
    }


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _thumb(src: Path, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")  # as displayed, not as stored
        im.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
        im.save(dest, "WEBP", quality=78, method=6)
        return {"width": im.width, "height": im.height}


def _subjects_label(path: Path) -> str:
    """The subjects file as run.json names it: repo-relative when it is in the repo."""
    resolved = path.resolve()
    return str(resolved.relative_to(ROOT)) if resolved.is_relative_to(ROOT) else str(path)


def _search_entries(downloader: ObservedCommons, subject: Subject) -> list[dict]:
    """Drain the downloader's pending search skips, tagged with the subject they came from."""
    entries = []
    for skipped in downloader.skipped:
        entries.append({"stage": "search", "subject": subject.folder, "category": subject.category, **skipped})
        print(f"  search-skip {skipped['mime']}: {skipped['title']}", flush=True)
    downloader.skipped.clear()
    return entries


def record(args: argparse.Namespace) -> None:
    cache: Path = args.cache.expanduser().resolve()
    home = cache / "home"
    subjects_file = _subjects_label(args.subjects)  # before the run, so it can't fail after it
    if home.exists():
        if not args.fresh:
            raise SystemExit(f"record: a pool already exists in {cache}/home; pass --fresh to start over")
        shutil.rmtree(home)
    shutil.rmtree(cache / "thumbs", ignore_errors=True)
    subjects = _load_subjects(args.subjects)

    # The tool's own defaults (user agent, rate limits, 12 MP cap, 40 GB budget). The user agent
    # must not carry anyone's contact details: the default names the repo.
    config = QuarryConfig(home=home)
    if config.user_agent != DEFAULT_USER_AGENT:
        raise SystemExit("record: expected quarry's default user agent")
    started = _now()
    entries: list[dict] = []
    rerun: dict[str, int] = {}
    t0 = time.monotonic()

    with ProvenanceStore(config.db_path) as store, NetClient(config) as net:
        engine = ObservedEngine(config, store, net)
        downloader = ObservedCommons(config, net)
        budget_hit = False
        for subject in subjects:
            print(f"== {subject.category}/{subject.folder}: {subject.query!r}", flush=True)
            for rec in downloader.harvest(subject, args.limit):
                entries.extend(_search_entries(downloader, subject))
                engine.original = None
                try:
                    outcome = engine.ingest_record(rec)
                except BudgetReached:
                    print("budget reached; stopping", flush=True)
                    budget_hit = True
                    break
                row = store.get_by_remote_url(rec.remote_url)
                entry = {
                    "stage": "ingest",
                    "subject": rec.subject,
                    "category": rec.category,
                    **downloader.info.get(rec.remote_url, {}),
                    "title": rec.title,
                    "source_url": rec.source_url,
                    "remote_url": rec.remote_url,
                    "licence": rec.licence,
                    "attribution": rec.attribution,
                    "photographer": rec.photographer,
                    "year": rec.year,
                    "outcome": {
                        "status": outcome.status,
                        "reason": outcome.reason,
                        "fetched_from": outcome.fetched_from,
                    },
                    "downloaded": engine.original,
                    "row": None
                    if row is None
                    else {
                        "status": row.status,
                        "licence": row.licence,
                        "sha256": row.sha256,
                        "width": row.width,
                        "height": row.height,
                        "file_size": row.file_size,
                        "path": f"images/{row.category}/{row.subject}/{row.filename}" if row.filename else None,
                        "fetched_from": row.fetched_from,
                        "downloaded_at": row.downloaded_at,
                    },
                }
                if outcome.status == "complete" and row is not None and row.filename:
                    pool_file = config.images_dir / row.category / row.subject / row.filename
                    entry["thumb"] = _thumb(pool_file, cache / "thumbs" / f"{row.sha256[:16]}.webp")
                entries.append(entry)
                print(f"  {outcome.status:11} {rec.licence[:20]:20} {rec.title}", flush=True)
            # Skips seen after this subject's last record (or with no record at all) are still its own.
            entries.extend(_search_entries(downloader, subject))
            if budget_hit:
                break

        # A rerun of the same harvest over the same pool: everything already recorded is skipped.
        # Candidates the budget cut off were never recorded, so the rerun would hit it again.
        print("== rerun", flush=True)
        again = ObservedEngine(config, store, net)
        try:
            for subject in subjects:
                for rec in ObservedCommons(config, net).harvest(subject, args.limit):
                    status = again.ingest_record(rec).status
                    rerun[status] = rerun.get(status, 0) + 1
        except BudgetReached:
            rerun["budget_reached"] = rerun.get("budget_reached", 0) + 1
        print(f"  {rerun}", flush=True)

    run = {
        "format": "argus-quarry-run",
        "started_at": started,
        "finished_at": _now(),
        "seconds": round(time.monotonic() - t0),
        "versions": _versions(),
        "settings": {
            "source": "commons",
            "limit_per_subject": args.limit,
            "user_agent": config.user_agent,
            "rate_per_sec": config.rate_for("commons").rate_per_sec,
            "burst": config.rate_for("commons").burst,
            "max_megapixels": config.max_megapixels,
            "max_total_gb": round(config.max_total_bytes / 1024**3, 2),
            "thumb_px": THUMB_PX,
        },
        "subjects_file": subjects_file,
        "subjects": [
            s.model_dump(include={"name", "category", "search", "aliases"}) | {"folder": s.folder} for s in subjects
        ],
        "entries": entries,
        "rerun": rerun,
    }
    run = demo_tape.strip_paths(run, {str(home): "$QUARRY_HOME", str(cache): "$QUARRY_DEMO_CACHE"})
    (cache / "run.json").write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {cache / 'run.json'}: {len(entries)} entries")


def _check_out(out: Path, cache: Path) -> Path:
    """Resolve ``--out`` and refuse a directory whose wiping would lose something else."""
    out = out.expanduser().resolve()
    cache = cache.expanduser().resolve()
    protected = {Path.cwd().resolve(), Path.home().resolve(), ROOT, Path(out.anchor)}
    if out in protected or cache.is_relative_to(out):
        raise SystemExit(f"build: refusing to replace {out}: it is or holds the cache, cwd, home or repo")
    if out.exists() and any(out.iterdir()) and not (out / "tape.json").exists():
        raise SystemExit(f"build: refusing to replace {out}: not empty and holds no tape.json")
    return out


def build(args: argparse.Namespace) -> None:
    cache: Path = args.cache.expanduser().resolve()
    out = _check_out(args.out, cache)
    run = json.loads((cache / "run.json").read_text(encoding="utf-8"))
    tape, files = demo_tape.build_tape(run)
    if out.exists():
        shutil.rmtree(out)
    (out / "thumbs").mkdir(parents=True)
    for record_id, thumb in files.items():
        shutil.copyfile(cache / "thumbs" / thumb, out / "thumbs" / f"{record_id}.webp")
    (out / "tape.json").write_text(json.dumps(tape, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"wrote {out}: {len(tape['records'])} records, {len(files)} images, {size / 1024:.0f} KiB")
    print(json.dumps(tape["summary"]["by_verdict"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="run harvest + ingest against Commons and write run.json")
    rec.add_argument("--subjects", type=Path, default=ROOT / "docs" / "demo" / "subjects.yaml")
    rec.add_argument("--limit", type=int, default=10, help="candidates per subject (quarry's --limit)")
    rec.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    rec.add_argument("--fresh", action="store_true", help="delete an earlier recording's pool first")
    bld = sub.add_parser("build", help="turn run.json into web/public/tape/")
    bld.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    bld.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    {"record": record, "build": build}[args.cmd](args)


if __name__ == "__main__":
    main()
