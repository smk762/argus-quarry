# CLAUDE.md — argus-quarry

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely. The deeper rationale (and section numbers referenced in the code) lives in [DESIGN.md](DESIGN.md).

## What this is

The **acquisition** stage — first in the Argus suite: it digs up raw material, acquiring public-domain / CC0 images from upstream archives across the LoRA-training taxonomy (identity / wardrobe / setting / concept) and landing them, with full provenance and licence, into a category-sorted raw pool the rest of the suite consumes.

```
argus-quarry -> argus-curator -> argus-lens -> argus-forge -> your trainer -> argus-proof
  acquire        curate/export    caption       configs        LoRA           validate
```

Deliberately lean: **acquisition + provenance only**. Quality scoring, near-dup, faces and captioning are owned downstream (curator/lens) — do not rebuild them here.

## Layout

`src/argus_quarry/`:

- `models.py` — Pydantic v2 data contract. `Subject` (a thing to harvest) and `SourceRecord` (the source-independent record every downloader yields), plus the licence/category/slug helpers (`normalise_licence`, `is_accepted_licence`, `normalise_category`, `slugify_name`). `Photograph` mirrors the DB row.
- `config.py` — `QuarryConfig`; all paths derive from `home` (`$QUARRY_HOME`, default `./quarry`). `from_env()` is what the CLI/compose use. Caps: megapixel, per-file bytes, total-pool budget.
- `store.py` — SQLite provenance DB (`subjects` + `photographs`, WAL). Owns migrations, the `sha256 UNIQUE` dedup key, and the read-only open logic (`open_readable`, `ProvenanceUnavailable`).
- `net.py` — `NetClient`: polite `httpx` with per-source token-bucket rate limits, backoff, and resumable (`Range`) streaming downloads.
- `downloaders/` — one module per archive over `base.Downloader`, registered in `downloaders/__init__.py` (`commons` is the only Phase-1 source). Add a source here.
- `subjects.py` + `seeds/*.yaml` — curated per-category seed lists; every source resolves to the same `Subject` shape.
- `ingest.py` — the per-record pipeline: licence-gate → resume-check → download → verify → cap → SHA256 dedup → land → record. Owns the running byte-budget counter.
- `export.py` — publish a filtered `<category>/<subject>/` tree into `$DATASET_DIR` (symlink by default, `--copy` optional).
- `cli.py` — Typer app (`run`, `fetch`, `export`, `list`, `stats`, `verify`, `subjects`, `serve`).
- `archives/` — searchable local **metadata archives** of large public-domain datasets (`pd12m.py`, `megalith.py` over `base.Archive`; registry in `__init__.py`). `QUARRY_<NAME>_DIR` points at the archived files; `archives index` builds one DuckDB `items` table per archive under `$QUARRY_HOME/cache/archives/` (optional `[archives]` extra). A `SliceSpec` (query + category/subject + limit) becomes a `SourceRecord` stream via `Archive.records()`, so slices land through normal ingest.
- `server/` — FastAPI provenance API on **:8102** (optional `[server]` extra): read-only, plus the `/archives` search routes and one opt-in write (`POST /archives/{name}/fetch`).
- `scripts/fetch_public_domain_metadata.py` — operator script (PEP 723, `uv run`) that archives PD12M / Megalith metadata from Hugging Face into `<root>/<source>/<part>/` with a `SOURCE.json` of pinned revisions. No photos.

## Commands

```bash
make dev     # uv venv + editable install with [dev,cli]
make test    # uv run --no-sync pytest --tb=short -q
make lint    # ruff check (CI pins ruff 0.15.20; format --check too)
make fmt     # ruff format + --fix
make check   # lint + test + build
```

Run one test: `uv run --no-sync pytest tests/test_ingest.py::test_name -q`.

## Conventions & gotchas

- **Provenance-first is the whole point.** Licence *policy* lives in exactly one place: the ingest licence-gate (`is_accepted_licence`). Downloaders faithfully report the raw licence string on *every* candidate and never filter — ingest quarantines the unaccepted ones centrally. Don't move that decision into a downloader.
- **`sha256` is the only dedup key** (`UNIQUE` in the DB → idempotent reruns). `phash` is opportunistic, informational-only metadata; it must **never** drive dedup here (near-dup is argus-curator's job).
- **Read paths must not mutate the pool** (DESIGN.md §9). `list`/`stats`/`export`/`verify` (without `--repair`) and the server go through `ProvenanceStore.open_readable` / the CLI's `_open_reader`, which tolerate a `:ro`-mounted `$QUARRY_HOME` (issue #5). `open_readable` picks `mode=ro` vs `immutable=1` and **refuses to read past an un-checkpointed WAL** (`ProvenanceUnavailable`) rather than serving stale data — don't loosen this or fall back to a read-write open on a read path.
- **The server is read-only except one opt-in route.** `POST /archives/{name}/fetch` lands an archive slice and is refused (403, with the slice JSON + CLI command in the body) unless `QUARRY_ALLOW_FETCH=1`, the demo-safe posture forge/proof/atlas share. It writes only through `IngestEngine`, one fetch at a time (409 otherwise). Every other route, the provenance routes especially, stays read-only; `tests/test_server.py::OPT_IN_WRITE_ROUTES` is the allow-list, so a new write route has to be added there on purpose. CORS is off by default; with `--cors` it allows `GET`/`HEAD`, plus `POST` only when fetch is enabled, and `allow_credentials` is deliberately false unless an explicit origin list is given (never reflect `*` with credentials). A short-lived connection per request keeps a concurrent `fetch`'s WAL visible.
- **Archives never write outside `$QUARRY_HOME`.** The archive dirs are treated as read-only (mount them `:ro`); the index is rebuilt atomically (temp file, then `os.replace`) so a server reading it never sees a half-written file. Megalith has no per-photo licence, so its records state the dataset filter as the licence label; keep that label free of the substring `cc0`, or `normalise_licence` would record Megalith items as CC0 rather than PD.
- **`$QUARRY_HOME` owns the whole raw pool** (`images/`, `metadata/`, `cache/`, `logs/`) as side-car dirs the curator never scans. `export` publishes a *separate* clean tree into `$DATASET_DIR` — keep the two separate. Files land under `images/<category>/<subject>/`.
- **Categories mirror the suite taxonomy** (curator `TargetCategory`): identity / wardrobe / setting / concept. Categories are normalised but not hard-validated so downstream can extend them.
- **Full-res `remote_url` is always retained** even when the megapixel/byte caps downscale a landed file, so it can be re-fetched at original size. The total-pool budget (`$QUARRY_MAX_GB`) stops a run *cleanly* and resumably (`BudgetReached`).
- **Backward-compat surface is intentional.** Pre-0.2 was person/portrait-only: keep the `Person`/`PortraitRecord` aliases, the hidden `people` command, and `load_people` importable. `store._migrate` upgrades legacy pools (people→subjects, flat→category layout) in place — preserve that.
- **Versioning is git-tag-derived** (`hatch-vcs`); `src/argus_quarry/_version.py` is generated (gitignored). Never hand-edit a version; tag `vX.Y.Z` to release (PyPI via OIDC + GHCR image). Ruff line-length 120, target py311; `structlog` for logging; Pydantic v2 everywhere.
