"""argus-quarry CLI (Typer): run / fetch / export / list / stats / verify / people.

Everything is idempotent — reruns resume partials and skip completed work. The
``run`` command is the compose entrypoint: fetch into the raw pool, then publish
a filtered tree into ``DATASET_DIR``.
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
    help="Provenance-first acquisition of public-domain / CC0 portrait images.",
    no_args_is_help=True,
)


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _harvest_records(config, net, sources: list[str], people, per_person_limit: int):
    """Flatten (source x person) harvests into one PortraitRecord stream."""
    from argus_quarry.downloaders import get_downloader

    streams = []
    for source in sources:
        downloader = get_downloader(source)(config, net)
        for person in people:
            streams.append(downloader.harvest(person, per_person_limit))
    return chain.from_iterable(streams)


def _do_fetch(config, sources: list[str], people, per_person_limit: int):
    from argus_quarry.ingest import IngestEngine, fetch
    from argus_quarry.net import NetClient
    from argus_quarry.store import ProvenanceStore

    with ProvenanceStore(config.db_path) as store, NetClient(config) as net:
        engine = IngestEngine(config, store, net)
        records = _harvest_records(config, net, sources, people, per_person_limit)
        summary = fetch(engine, records)
    return summary


@app.command()
def run(
    source: str = Option("commons", "--source", help="Comma-separated source(s), e.g. commons"),
    limit: int = Option(50, "--limit", help="Max candidates to harvest per person per source"),
    people_limit: int | None = Option(None, "--people-limit", help="Cap the number of seed people used"),
    seed: Path | None = Option(None, "--seed", help="Override seed people.yaml path"),
    from_wikidata: bool = Option(False, "--from-wikidata", help="(Phase 2) harvest people from Wikidata SPARQL"),
    export: bool = Option(False, "--export", help="Publish DATASET_DIR after fetching"),
    dest: Path | None = Option(None, "--dest", help="Publish destination (default: $DATASET_DIR or ./data)"),
    copy: bool = Option(False, "--copy", help="Copy instead of symlink when exporting"),
    licence: str | None = Option(None, "--licence", help="Only publish these licences (e.g. CC0,PD)"),
) -> None:
    """Fetch into the raw pool, then (optionally) publish a curator-ready tree."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.people import load_people

    config = QuarryConfig.from_env()
    sources = _split_csv(source) or ["commons"]
    people = load_people(from_wikidata=from_wikidata, seed_path=seed, limit=people_limit)

    typer.echo(f"Fetching: sources={sources} people={len(people)} limit/person={limit}")
    summary = _do_fetch(config, sources, people, limit)
    _print_fetch_summary(summary)

    if export:
        _do_export(config, dest, copy=copy, licences=_split_csv(licence))


@app.command()
def fetch(
    source: str = Option("commons", "--source", help="Comma-separated source(s)"),
    limit: int = Option(50, "--limit", help="Max candidates per person per source"),
    people_limit: int | None = Option(None, "--people-limit", help="Cap the number of seed people used"),
    seed: Path | None = Option(None, "--seed", help="Override seed people.yaml path"),
) -> None:
    """Fetch candidates into the raw pool (no publish)."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.people import load_people

    config = QuarryConfig.from_env()
    sources = _split_csv(source) or ["commons"]
    people = load_people(seed_path=seed, limit=people_limit)
    typer.echo(f"Fetching: sources={sources} people={len(people)} limit/person={limit}")
    summary = _do_fetch(config, sources, people, limit)
    _print_fetch_summary(summary)


@app.command()
def export(
    dest: Path | None = Option(None, "--dest", help="Publish destination (default: $DATASET_DIR or ./data)"),
    copy: bool = Option(False, "--copy", help="Copy instead of symlink"),
    licence: str | None = Option(None, "--licence", help="Only publish these licences (e.g. CC0,PD)"),
    person: str | None = Option(None, "--person", help="Only publish one person (folder name)"),
) -> None:
    """Publish a filtered Person_Name/ tree into DATASET_DIR from the raw pool."""
    from argus_quarry.config import QuarryConfig

    config = QuarryConfig.from_env()
    _do_export(config, dest, copy=copy, licences=_split_csv(licence), person=person)


@app.command("list")
def list_cmd(
    source: str | None = Option(None, "--source", help="Filter by source"),
    licence: str | None = Option(None, "--licence", help="Filter by licence (e.g. CC0,PD)"),
    person: str | None = Option(None, "--person", help="Filter by person (folder name)"),
    status: str = Option("complete", "--status", help="Filter by status"),
    limit: int = Option(50, "--limit", help="Max rows to print"),
) -> None:
    """List landed photographs with provenance."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.store import ProvenanceStore

    config = QuarryConfig.from_env()
    with ProvenanceStore(config.db_path) as store:
        photos = store.iter_photographs(status=status, source=source, licences=_split_csv(licence), person=person)
    for ph in photos[:limit]:
        year = ph.year or "----"
        typer.echo(f"  [{ph.licence:<4}] {ph.person_name:<24} {year}  {ph.source:<10} {ph.filename or ph.remote_url}")
    typer.echo(f"\n{len(photos)} row(s) (showing up to {limit}).")


@app.command()
def stats() -> None:
    """Summarise the provenance DB: counts by status/source/licence + pool size."""
    from argus_quarry.config import QuarryConfig
    from argus_quarry.store import ProvenanceStore

    config = QuarryConfig.from_env()
    with ProvenanceStore(config.db_path) as store:
        s = store.stats()

    gb = s["total_bytes"] / (1024**3)
    typer.echo("=" * 48)
    typer.echo(f"  People:        {s['people']}")
    typer.echo(f"  Photographs:   {s['photographs']}")
    typer.echo(f"  Pool size:     {gb:.2f} GB ({s['total_bytes']} bytes)")
    typer.echo("=" * 48)
    for title, key in (("By status", "by_status"), ("By source", "by_source"), ("By licence", "by_licence")):
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
    with ProvenanceStore(config.db_path) as store:
        for ph in store.iter_photographs(status="complete"):
            if not ph.filename:
                continue
            path = config.images_dir / ph.person_name / ph.filename
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
def people(
    seed: Path | None = Option(None, "--seed", help="Override seed people.yaml path"),
    from_wikidata: bool = Option(False, "--from-wikidata", help="(Phase 2) harvest from Wikidata SPARQL"),
) -> None:
    """List the people seed downloaders harvest around."""
    from argus_quarry.people import load_people

    try:
        rows = load_people(from_wikidata=from_wikidata, seed_path=seed)
    except NotImplementedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for p in rows:
        life = f"{p.birth_year or '?'}–{p.death_year or '?'}"
        wd = f" [{p.wikidata_id}]" if p.wikidata_id else ""
        typer.echo(f"  {p.folder:<24} {life:<11} {p.occupation or ''}{wd}")
    typer.echo(f"\n{len(rows)} people.")


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


def _do_export(config, dest: Path | None, *, copy: bool, licences=None, person=None) -> None:
    import os

    from argus_quarry.export import export_tree
    from argus_quarry.store import ProvenanceStore

    if dest is None:
        dest = Path(os.environ.get("DATASET_DIR", "./data"))
    mode = "copy" if copy else "symlink"
    with ProvenanceStore(config.db_path) as store:
        result = export_tree(config, store, dest, mode=mode, licences=licences, person=person)
    typer.echo(
        f"Published {result.published} image(s) ({mode}) across {len(result.people)} "
        f"people -> {result.dest}  (missing={result.missing}, skipped={result.skipped})"
    )


if __name__ == "__main__":
    app()
