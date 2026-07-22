"""argus-quarry CLI (Typer): run / fetch / export / list / stats / verify / subjects / serve.

Everything is idempotent — reruns resume partials and skip completed work. The
``run`` command is the compose entrypoint: fetch into the raw pool, then publish
a filtered, category-sorted tree into ``DATASET_DIR``.
"""

from __future__ import annotations

import sys
from itertools import chain
from pathlib import Path

try:
    import typer
    from typer import Option
except ImportError as _exc:  # pragma: no cover
    print("CLI requires: pip install argus-quarry[cli]", file=sys.stderr)
    raise SystemExit(1) from _exc

app = typer.Typer(
    name="argus-quarry",
    help="Provenance-first acquisition of public-domain / CC0 images (identity / wardrobe / setting / concept).",
    no_args_is_help=True,
)


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _open_reader(config):
    """Open the pool for a read-only command, tolerating a `:ro` $QUARRY_HOME.

    `list`/`stats`/`export`/`verify` never write, so they must not fail just
    because the pool is mounted read-only (issue #5). Falls back to a normal
    read-write open when no read-only mode works, which keeps the fresh-pool
    behaviour (an absent DB is created and reported as empty).
    """
    from argus_quarry.store import ProvenanceStore, ProvenanceUnavailable

    try:
        return ProvenanceStore.open_readable(config.db_path)
    except (OSError, ProvenanceUnavailable):
        return ProvenanceStore(config.db_path)


def _harvest_records(config, net, sources: list[str], subjects, per_subject_limit: int):
    """Flatten (source x subject) harvests into one SourceRecord stream."""
    from argus_quarry.downloaders import get_downloader

    streams = []
    for source in sources:
        downloader = get_downloader(source)(config, net)
        for subject in subjects:
            streams.append(downloader.harvest(subject, per_subject_limit))
    return chain.from_iterable(streams)


def _do_fetch(config, sources: list[str], subjects, per_subject_limit: int):
    from argus_quarry.ingest import IngestEngine, fetch
    from argus_quarry.net import NetClient
    from argus_quarry.store import ProvenanceStore

    with ProvenanceStore(config.db_path) as store, NetClient(config) as net:
        engine = IngestEngine(config, store, net)
        records = _harvest_records(config, net, sources, subjects, per_subject_limit)
        summary = fetch(engine, records)
    return summary


@app.command()
def run(
    source: str = Option("commons", "--source", help="Comma-separated source(s), e.g. commons"),
    limit: int = Option(50, "--limit", help="Max candidates to harvest per subject per source"),
    category: str | None = Option(None, "--category", help="Only this category (identity/wardrobe/setting/concept)"),
    subject_limit: int | None = Option(None, "--subject-limit", help="Cap the number of seed subjects used"),
    seed: Path | None = Option(None, "--seed", help="Override the seed YAML path (single category)"),
    from_wikidata: bool = Option(False, "--from-wikidata", help="(Phase 2) harvest identity from Wikidata SPARQL"),
    export: bool = Option(False, "--export", help="Publish DATASET_DIR after fetching"),
    dest: Path | None = Option(None, "--dest", help="Publish destination (default: $DATASET_DIR or ./data)"),
    copy: bool = Option(False, "--copy", help="Copy instead of symlink when exporting"),
    licence: str | None = Option(None, "--licence", help="Only publish these licences (e.g. CC0,PD)"),
) -> None:
    """Fetch into the raw pool, then (optionally) publish a curator-ready tree."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.subjects import load_subjects

    config = QuarryConfig.from_env()
    sources = _split_csv(source) or ["commons"]
    subjects = load_subjects(category=category, from_wikidata=from_wikidata, seed_path=seed, limit=subject_limit)

    typer.echo(f"Fetching: sources={sources} subjects={len(subjects)} limit/subject={limit}")
    summary = _do_fetch(config, sources, subjects, limit)
    _print_fetch_summary(summary)

    if export:
        _do_export(config, dest, copy=copy, licences=_split_csv(licence), category=category)


@app.command()
def fetch(
    source: str = Option("commons", "--source", help="Comma-separated source(s)"),
    limit: int = Option(50, "--limit", help="Max candidates per subject per source"),
    category: str | None = Option(None, "--category", help="Only this category (identity/wardrobe/setting/concept)"),
    subject_limit: int | None = Option(None, "--subject-limit", help="Cap the number of seed subjects used"),
    seed: Path | None = Option(None, "--seed", help="Override the seed YAML path (single category)"),
) -> None:
    """Fetch candidates into the raw pool (no publish)."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.subjects import load_subjects

    config = QuarryConfig.from_env()
    sources = _split_csv(source) or ["commons"]
    subjects = load_subjects(category=category, seed_path=seed, limit=subject_limit)
    typer.echo(f"Fetching: sources={sources} subjects={len(subjects)} limit/subject={limit}")
    summary = _do_fetch(config, sources, subjects, limit)
    _print_fetch_summary(summary)


@app.command()
def export(
    dest: Path | None = Option(None, "--dest", help="Publish destination (default: $DATASET_DIR or ./data)"),
    copy: bool = Option(False, "--copy", help="Copy instead of symlink"),
    licence: str | None = Option(None, "--licence", help="Only publish these licences (e.g. CC0,PD)"),
    category: str | None = Option(None, "--category", help="Only publish one category"),
    subject: str | None = Option(None, "--subject", help="Only publish one subject (folder name)"),
) -> None:
    """Publish a filtered <category>/<subject>/ tree into DATASET_DIR from the raw pool."""
    from argus_quarry.config import QuarryConfig

    config = QuarryConfig.from_env()
    _do_export(config, dest, copy=copy, licences=_split_csv(licence), subject=subject, category=category)


@app.command("list")
def list_cmd(
    source: str | None = Option(None, "--source", help="Filter by source"),
    licence: str | None = Option(None, "--licence", help="Filter by licence (e.g. CC0,PD)"),
    category: str | None = Option(None, "--category", help="Filter by category"),
    subject: str | None = Option(None, "--subject", help="Filter by subject (folder name)"),
    status: str = Option("complete", "--status", help="Filter by status"),
    limit: int = Option(50, "--limit", help="Max rows to print"),
) -> None:
    """List landed photographs with provenance."""
    from argus_quarry.config import QuarryConfig

    config = QuarryConfig.from_env()
    with _open_reader(config) as store:
        photos = store.iter_photographs(
            status=status, source=source, licences=_split_csv(licence), subject=subject, category=category
        )
    for ph in photos[:limit]:
        year = ph.year or "----"
        typer.echo(
            f"  [{ph.licence:<4}] {ph.category:<9} {ph.subject:<22} {year}  "
            f"{ph.source:<10} {ph.filename or ph.remote_url}"
        )
    typer.echo(f"\n{len(photos)} row(s) (showing up to {limit}).")


@app.command()
def stats() -> None:
    """Summarise the provenance DB: counts by status/category/source/licence + pool size."""
    from argus_quarry.config import QuarryConfig

    config = QuarryConfig.from_env()
    with _open_reader(config) as store:
        s = store.stats()

    gb = s["total_bytes"] / (1024**3)
    typer.echo("=" * 48)
    typer.echo(f"  Subjects:      {s['subjects']}")
    typer.echo(f"  Photographs:   {s['photographs']}")
    typer.echo(f"  Pool size:     {gb:.2f} GB ({s['total_bytes']} bytes)")
    typer.echo("=" * 48)
    for title, key in (
        ("By status", "by_status"),
        ("By category", "by_category"),
        ("By source", "by_source"),
        ("By licence", "by_licence"),
    ):
        if s[key]:
            typer.echo(f"\n{title}:")
            for k, n in sorted(s[key].items(), key=lambda kv: -kv[1]):
                typer.echo(f"  {n:6d}  {k}")


@app.command()
def verify(
    repair: bool = Option(False, "--repair", help="Mark rows whose files are missing/corrupt as failed"),
) -> None:
    """Verify landed files still exist, decode, and match their recorded SHA256."""
    import hashlib

    from PIL import Image

    from argus_quarry.config import QuarryConfig
    from argus_quarry.store import ProvenanceStore

    config = QuarryConfig.from_env()
    ok = missing = corrupt = mismatch = 0
    # --repair writes, so it needs a read-write pool; a plain verify does not.
    store_cm = ProvenanceStore(config.db_path) if repair else _open_reader(config)
    with store_cm as store:
        for ph in store.iter_photographs(status="complete"):
            if not ph.filename:
                continue
            path = config.images_dir / ph.category / ph.subject / ph.filename
            if not path.exists():
                missing += 1
                if repair and ph.id:
                    store.set_status(ph.id, "failed")
                continue
            try:
                with Image.open(path) as im:
                    im.verify()
            except Exception:
                corrupt += 1
                if repair and ph.id:
                    store.set_status(ph.id, "failed")
                continue
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if ph.sha256 and h.hexdigest() != ph.sha256:
                mismatch += 1
            else:
                ok += 1

    typer.echo(f"ok={ok} missing={missing} corrupt={corrupt} sha_mismatch={mismatch}")
    if (missing or corrupt or mismatch) and not repair:
        typer.echo("(run with --repair to mark broken rows as failed for re-fetch)")


@app.command()
def subjects(
    category: str | None = Option(None, "--category", help="Only this category (default: all)"),
    seed: Path | None = Option(None, "--seed", help="Override the seed YAML path (single category)"),
    from_wikidata: bool = Option(False, "--from-wikidata", help="(Phase 2) harvest identity from Wikidata SPARQL"),
) -> None:
    """List the subject seed(s) downloaders harvest around."""
    from argus_quarry.subjects import load_subjects

    try:
        rows = load_subjects(category=category, from_wikidata=from_wikidata, seed_path=seed)
    except NotImplementedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for s in rows:
        extra = ""
        if s.category == "identity":
            life = f"{s.birth_year or '?'}–{s.death_year or '?'}"
            wd = f" [{s.wikidata_id}]" if s.wikidata_id else ""
            extra = f"{life:<11} {s.occupation or ''}{wd}"
        else:
            extra = f'search="{s.query}"'
        typer.echo(f"  {s.category:<9} {s.folder:<24} {extra}")
    typer.echo(f"\n{len(rows)} subject(s).")


@app.command()
def serve(
    port: int = Option(8102, "--port", "-p", help="Port to listen on"),
    host: str = Option("0.0.0.0", "--host", help="Host to bind to"),
    cors: bool = Option(False, "--cors", help="Enable CORS (allow all origins)"),
) -> None:
    """Start the read-only provenance API (FastAPI) on :8102.

    The pool root comes from $QUARRY_HOME (default ./quarry), same as every
    other command. Strictly read-only — see DESIGN.md section 9.
    """
    try:
        import uvicorn

        from argus_quarry.server import create_app
    except ImportError as _exc:  # pragma: no cover
        typer.echo("Server requires: pip install argus-quarry[server]", err=True)
        raise typer.Exit(1) from _exc

    application = create_app(cors=cors)
    uvicorn.run(application, host=host, port=port)


# Backward-compatible alias for the pre-0.2 `people` command (identity only).
@app.command("people", hidden=True)
def people(
    seed: Path | None = Option(None, "--seed", help="Override the identity seed YAML path"),
    from_wikidata: bool = Option(False, "--from-wikidata", help="(Phase 2) harvest from Wikidata SPARQL"),
) -> None:
    """Deprecated alias for `subjects --category identity`."""
    subjects(category="identity", seed=seed, from_wikidata=from_wikidata)


# ── shared output / export plumbing ──────────────────────────────────
def _print_fetch_summary(summary) -> None:
    typer.echo("-" * 40)
    typer.echo(f"  landed:       {summary.complete}")
    typer.echo(f"  skipped:      {summary.skipped}")
    typer.echo(f"  duplicate:    {summary.duplicate}")
    typer.echo(f"  quarantined:  {summary.quarantined}")
    typer.echo(f"  failed:       {summary.failed}")
    if summary.budget_reached:
        typer.echo("  ** budget cap reached — run stopped cleanly (rerun to resume) **")
    typer.echo("-" * 40)


def _do_export(config, dest: Path | None, *, copy: bool, licences=None, subject=None, category=None) -> None:
    import os

    from argus_quarry.export import export_tree

    if dest is None:
        dest = Path(os.environ.get("DATASET_DIR", "./data"))
    mode = "copy" if copy else "symlink"
    with _open_reader(config) as store:
        result = export_tree(config, store, dest, mode=mode, licences=licences, subject=subject, category=category)
    typer.echo(
        f"Published {result.published} image(s) ({mode}) across {len(result.subjects)} "
        f"subject(s) -> {result.dest}  (missing={result.missing}, skipped={result.skipped})"
    )


if __name__ == "__main__":
    app()
