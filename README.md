# Argus Quarry

[![PyPI](https://img.shields.io/pypi/v/argus-quarry)](https://pypi.org/project/argus-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/argus-quarry)](https://pypi.org/project/argus-quarry/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/smk762/argus-quarry/actions/workflows/ci.yml/badge.svg)](https://github.com/smk762/argus-quarry/actions/workflows/ci.yml)

**Provenance-first acquisition of public-domain / CC0 images** — the *input
stage* of the Argus suite. The quarry digs up raw material: it downloads images
from upstream archives and lands them, **with full provenance and licensing**,
into a folder the rest of the suite already consumes (`DATASET_DIR` →
`/data/images`).

Subjects are grouped into LoRA-training **categories** — `identity` (people),
`wardrobe` (garments), `setting` (scenes/environments) and `concept`
(styles/themes) — and everything lands sorted into `<category>/<subject>/`
subfolders. A category is independent of identity: a subject can be *Mark Twain*,
a *red dress*, a *modern kitchen*, or *cyberpunk*.

It is deliberately lean — *acquisition + provenance, nothing more*. Quality
scoring, near-duplicate detection, faces, embeddings and captioning are owned
downstream by [argus-curator](https://github.com/smk762/argus-curator) and
[argus-lens](https://github.com/smk762/argus-lens); quarry never re-implements
them.

> **Want a UI?** Quarry is CLI-only by design (see [DESIGN.md](DESIGN.md) §9).
> The suite's web frontend — [**argus-studio**](https://github.com/smk762/argus-studio)
> — surfaces the curation and captioning stages that consume quarry's output
> (e.g. its `/curate` view scans the `<category>/<subject>/` tree quarry publishes).

```
argus-quarry (NEW)          argus-curator (:8101)        argus-lens (:8100)        argus-studio
─ download  ─┐              ─ scan + score  ─┐           ─ caption ─┐              ─ web UI (:3000)
─ verify    ─┤   images +   ─ near-dup      ─┤  manifest ─ buckets ─┤   dataset    ─ /curate
─ provenance┤   provenance  ─ face-cluster  ─┤           ─ (ident/ ─┤   → LoRA     ─ caption
─ SHA256    ─┴───────────►  ─ select+export ─┴──────────►  wardrobe)─┘
   /data/images (DATASET_DIR) ─────────────────────────────────────────────────►
```

See [DESIGN.md](DESIGN.md) for the full rationale and phased plan.

## Why it exists

- **Provenance-first.** Every image carries its source URL, landing page,
  licence and attribution. A record with no *accepted* licence (PD / CC0) is
  **quarantined, never landed**.
- **Idempotent.** Exact-dedup by SHA256 (`UNIQUE` in the DB) and a `status`
  lifecycle mean reruns resume partials and never duplicate bytes.
- **Bounded.** A per-file resolution/size cap and a total-archive GB budget keep
  the pool predictable; the full-resolution URL is always retained for later
  re-fetch.
- **Source-independent.** Every downloader yields the same `SourceRecord`, so
  the pipeline never learns which archive a file came from.
- **Category-sorted.** Subjects carry a category (identity / wardrobe / setting /
  concept) and land under `<category>/<subject>/`, so one pool serves every LoRA
  workflow.

## Install

```bash
pip install argus-quarry            # library + downloaders
pip install "argus-quarry[cli]"     # + the argus-quarry command
pip install "argus-quarry[phash]"   # + opportunistic perceptual-hash metadata
pip install "argus-quarry[server]"  # + the read-only provenance HTTP API (serve)
```

For development the suite uses [uv](https://docs.astral.sh/uv/) (works on PEP 668
"externally managed" system Pythons):

```bash
make dev                            # uv venv + editable install (dev + cli extras)
# or, manually:
uv venv && uv pip install -e ".[dev,cli]"
```

## Quickstart

```bash
# Inspect the curated subject seeds (all categories, or one)
argus-quarry subjects
argus-quarry subjects --category wardrobe

# Fetch from Wikimedia Commons into the raw pool, then publish a curator-ready,
# CC0/PD-only tree into $DATASET_DIR (symlinks by default). Omit --category to
# harvest every category (identity + wardrobe + setting + concept).
argus-quarry run --source commons --limit 20 --export --licence CC0,PD
argus-quarry run --category concept --limit 20 --export   # just one category

# Or split the two stages
argus-quarry fetch --source commons --limit 20
argus-quarry export --dest ./data --licence CC0,PD   # add --copy to avoid symlinks

# Inspect what you have
argus-quarry stats
argus-quarry list --category setting --licence CC0
argus-quarry verify              # re-check files decode + match recorded SHA256
```

> Installed into a `uv` venv? Prefix commands with `uv run` (e.g.
> `uv run argus-quarry stats`) or `source .venv/bin/activate` first.

## CLI

| Command | What it does |
|---|---|
| `run`    | Fetch into the raw pool, then (optionally) publish — the compose entrypoint |
| `fetch`  | Download candidates into the raw pool (no publish) |
| `export` | Publish a filtered `<category>/<subject>/` tree into `DATASET_DIR` (symlink / `--copy`) |
| `list`   | List landed photographs with provenance (filter by source / licence / category / subject) |
| `stats`  | Counts by status / category / source / licence + raw-pool size |
| `verify` | Re-check landed files exist, decode, and match their recorded SHA256 |
| `subjects` | Show the subject seed(s) downloaders harvest around (filter by `--category`) |
| `serve`  | Start the read-only provenance HTTP API on `:8102` (needs the `server` extra) |

`run`, `fetch`, `export` and `list` all accept `--category`
(`identity` / `wardrobe` / `setting` / `concept`); with none given they span every category.

## Layout produced

Quarry fetches into a **raw pool** it fully owns (`$QUARRY_HOME`, a sibling
side-car dir), then `export` publishes a clean tree into `DATASET_DIR`:

```
$QUARRY_HOME/                          # side-car state — NEVER scanned by curator
├── images/
│   ├── identity/Albert_Einstein/…      # the raw pool, sorted by <category>/<subject>/
│   ├── wardrobe/Red_dress/…
│   ├── setting/Modern_kitchen/…
│   └── concept/Cyberpunk/…
├── metadata/portraits.sqlite          # provenance DB (subjects + photographs)
├── cache/  logs/

$DATASET_DIR/                          # published, curator-ready view (via export)
├── identity/Albert_Einstein/…          # symlinks (default) or copies into the pool
└── wardrobe/Red_dress/…
```

## Provenance model

A single SQLite database (`portraits.sqlite`, WAL mode) with two tables:

- **`subjects`** — `name · category · wikidata_id · birth_year · death_year · occupation`
  (the identity-only columns stay `NULL` for wardrobe / setting / concept subjects)
- **`photographs`** — `category · title · photographer · year · source · source_url ·
  licence · attribution · width · height · file_size · filename ·
  **sha256 (UNIQUE)** · phash · remote_url · status · downloaded_at`

`sha256` is the exact-dedup key (idempotent reruns); `status`
(`pending → downloading → complete`, plus `duplicate` / `quarantined` / `failed`)
tracks resumability. `phash` is recorded opportunistically and is *informational
only* — it never drives dedup here (that's [argus-curator](https://github.com/smk762/argus-curator)'s job).

## Configuration

Copy `.env.example` to `.env`. Key knobs:

| Env | Default | Meaning |
|---|---|---|
| `QUARRY_HOME` | `./quarry` | Raw pool + DB + cache + logs (side-car dir) |
| `QUARRY_MAX_GB` | `40` | Total raw-pool ceiling; `0` = unlimited |
| `COMMONS_USER_AGENT` | descriptive default | Polite UA (Commons/LoC expect one) |
| `DATASET_DIR` | `./data` | Published view `export` writes into |
| `QUARRY_PD12M_DIR` / `QUARRY_MEGALITH_DIR` | _(unset)_ | Archived metadata for the [archives](#public-domain-archives) (`<root>/pd12m`, `<root>/megalith`) |
| `QUARRY_ALLOW_FETCH` | _(unset = off)_ | Let `POST /archives/{name}/fetch` land a slice from the server |

## HTTP server

A tiny provenance API (FastAPI) over the raw pool. It is read-only apart from
one opt-in route, the archive slice fetch, which is refused unless
`QUARRY_ALLOW_FETCH=1` (see [Public-domain archives](#public-domain-archives)).
It powers the
[argus-studio](https://github.com/smk762/argus-studio) `/gallery` view, the same
way `/curate` talks to argus-curator on `:8101`.

```bash
pip install "argus-quarry[server]"          # fastapi + uvicorn
argus-quarry serve --host 0.0.0.0 --port 8102 --cors
```

The published image ships the `server` extra and serves by default on `:8102`:

```bash
docker run --rm -p 8102:8102 -v "$PWD/quarry:/data/quarry" \
  -e QUARRY_HOME=/data/quarry ghcr.io/smk762/argus-quarry:latest
```

The entrypoint is still `argus-quarry`, so any acquisition subcommand works by
overriding the command (`docker run ... ghcr.io/smk762/argus-quarry fetch …`).

The pool root comes from `$QUARRY_HOME` (default `./quarry`), exactly like every
other command — the compose service just sets `QUARRY_HOME=/data/quarry`.

Serving never mutates the provenance data — on any mount. The DB is opened
through a `mode=ro` SQLite URI, which still tracks a concurrently running `fetch`
through the WAL; the server never migrates, relocates, or writes a row, so
pointing it at an empty or unmigrated pool reports `503` rather than quietly
building one.

It is not wholly side-effect-free on a *writable* mount, though: `mode=ro` lets
SQLite create the `-wal`/`-shm` sidecars it uses to follow the WAL, and a
read-only connection can never checkpoint or unlink them, so those two files
persist beside the DB (reused across opens, not accumulated — two files, not
unbounded growth; issue #9). Mounting the pool `:ro` avoids them entirely:

```bash
docker run --rm -p 8102:8102 -v "$PWD/quarry:/data/quarry:ro" \
  -e QUARRY_HOME=/data/quarry ghcr.io/smk762/argus-quarry:latest
```

A read-only *directory* also stops SQLite creating the `-shm` sidecar `mode=ro`
needs, so on that mount quarry falls back to `immutable=1`, which needs no
sidecars. `immutable=1` cannot see a `-wal`, so it is used only when there is
none: a pool snapshotted mid-write, or left behind by a killed `fetch`, answers
`503` instead of silently serving stale counts. Checkpoint it
(`sqlite3 portraits.sqlite 'PRAGMA wal_checkpoint(TRUNCATE)'`), or mount the pool
writable, and it serves normally.

The image runs as a non-root user (uid `10001`), so a bind-mounted `$QUARRY_HOME`
must be readable by that uid to serve, and writable by it for the acquisition
subcommands (`fetch`, `run`) — e.g. `chown -R 10001:10001 quarry/` on the host.
This keeps the read-only sidecars above, and anything the acquisition commands
land, owned by an unprivileged uid rather than root (issues #8/#9).

The read-only CLI commands (`stats`, `list`, `export`, `verify` without
`--repair`) open the pool the same way, so they work against a `:ro` mount too.

Liveness and readiness are separate. `GET /health` answers `200` whenever the
process is up — it never touches the DB — so a container mounted at an empty
`QUARRY_HOME` that is seeded later still comes up live. `GET /ready` opens the
pool and answers `503` with `{"status": "unavailable", "service": "argus-quarry",
"database": "..."}` when it cannot be served (missing, unmigrated, or an
unreadable WAL); the `database` value is a single generic reason
(`"provenance database unavailable"`), deliberately not a per-cause code, so it
never leaks a filesystem path. Point an orchestrator's *readiness* probe at
`/ready` to shed a container that is up but has no data to serve, and its
*liveness* probe at `/health`.

| Endpoint | Returns |
|---|---|
| `GET /health` | Liveness: `200 {status, service, version, quarry_home}` while the process is up |
| `GET /ready` | Readiness: `200 {status: ready, service, database: ok}`, or `503 {status: unavailable, service, database}` when the pool cannot be served |
| `GET /stats` | Counts by status / category / source / licence + `total_bytes` (mirrors `stats`) |
| `GET /subjects?category=` | Distinct subjects with landed photo counts: `{subjects: [{folder, category, photo_count}]}` |
| `GET /photos?category=&subject=&licence=&source=&status=&limit=&offset=` | Paginated provenance rows: `{total, offset, limit, photos: […]}`; `status` defaults to `complete` (pass empty for all), `licence` accepts CSV (`CC0,PD`), `limit` ≤ 500 |
| `GET /photos/{id}` | One photograph with full provenance (404 if unknown) |
| `GET /thumb?id=&size=384` | WEBP thumbnail rendered from the pooled file (`size` = longest edge, capped at 1024) |

## Public-domain archives

Two large public-domain datasets can be searched and sliced without downloading
their images. Quarry keeps their **metadata** locally (links, captions, sizes,
licences) and lands only the slices you choose, through the same ingest
pipeline as every other source (licence gate, SHA256 dedup, provenance).

| Archive | Items | Filter on | Images come from |
|---|---|---|---|
| [PD12M](https://huggingface.co/datasets/Spawning/PD12M) | 12.4M | caption, institution (640), licence (CC0 / PDM), size, MIME type | the public `s3://pd12m` bucket |
| [Megalith-10m](https://huggingface.co/datasets/madebyollin/megalith-10m) | 9.58M | Florence-2 caption (8.94M), Flickr account, size (~9.4M) | Flickr (1024px) |

Megalith's links are a shuffled sample, so the Flickr account is its only built-in
grouping. It also has no per-photo licence column, so each record states the
dataset's licence filter as its licence label.

```bash
# 1. archive the metadata (~45 GB incl. PD12M's CLIP embeddings; resumable; no photos)
HF_TOKEN=hf_... uv run scripts/fetch_public_domain_metadata.py --root /media/photos/public_domain
# 2. point quarry at it and build the search index (DuckDB, under $QUARRY_HOME/cache/archives/)
export QUARRY_PD12M_DIR=/media/photos/public_domain/pd12m QUARRY_MEGALITH_DIR=/media/photos/public_domain/megalith
pip install "argus-quarry[cli,archives]"
argus-quarry archives index
argus-quarry archives search pd12m --q "harbour boats" --min-short-side 1024
# 3. land a slice saved from argus-studio's /gallery -> Sources (or written by hand)
argus-quarry archives fetch slice.json            # --dry-run lists the URLs first
```

A slice is a query plus where it lands:

```json
{"archive": "pd12m", "category": "setting", "subject": "Harbour_views", "limit": 200,
 "query": {"q": "harbour boats", "collections": ["Rijksmuseum"], "min_short_side": 1024, "sort": "largest"}}
```

The server exposes the same thing over HTTP. Every route is `GET` except the fetch:

| Route | Returns |
|---|---|
| `GET /archives` | each archive's status (`configured`, `present`, `indexed`, `items`) and `fetch_allowed` |
| `GET /archives/{name}/facets` | collections, licences and MIME types with counts |
| `GET /archives/{name}/search?q=&collection=&licence=&mime=&min_width=&min_height=&min_short_side=&orientation=&sized_only=&ids=&sort=&limit=&offset=` | `{total, offset, limit, items}`; `sort` is `shuffle` (fixed per index) or `largest` |
| `GET /archives/{name}/items/{id}` | one item |
| `GET /archives/{name}/thumb?id=&size=` | WEBP rendered in memory (PD12M), or a 307 to Flickr's own 500px rendition (Megalith) |
| `GET /archives/{name}/slice?<search filters>&category=&subject=&limit=` | `{slice, matches, will_fetch, command, fetch_allowed}` |
| `POST /archives/{name}/fetch` (slice JSON body) | NDJSON `start` / `item` / `done` or `error` frames; **403 unless `QUARRY_ALLOW_FETCH=1`**, with the slice and CLI command in the body; 409 while another fetch runs |

## Suite integration

Quarry ships a `gallery` profile in the suite's
[argus-studio](https://github.com/smk762/argus-studio) `compose.yaml`.
It's a run-to-completion job: fetch into the pool, publish into `DATASET_DIR`,
then let curator/lens (and the web UI's `/curate` view) consume the result.

```bash
docker compose --profile gallery up --build   # fetch → pool → publish DATASET_DIR
docker compose --profile curator up --build    # then curate the published images
```

> The published tree symlinks back into `QUARRY_HOME/images`. For those links to
> resolve inside the curator/lens containers, mount `QUARRY_HOME` read-only there
> too, or run `export --copy`.

## Sources

| Source | Status |
|---|---|
| Wikimedia Commons | **Phase 1 (implemented)** |
| Library of Congress, Smithsonian, Rijksmuseum, LAC (Karsh allow-list) | Phase 2 |
| Europeana, Flickr Commons (strict per-record rights) | Phase 3 |
| PD12M, Megalith-10m | **Archives (implemented)**: searched locally, landed by slice |

New sources register in `downloaders/` behind a common `Downloader` contract, so
adding one never touches ingest, storage, or export.

## Development

```bash
make lint     # ruff
make test     # pytest
make check    # lint + test + build
```

## Related projects

- [**argus-studio**](https://github.com/smk762/argus-studio) — the suite's Next.js web UI (captioning + `/curate`).
- [**argus-curator**](https://github.com/smk762/argus-curator) — training-suitability scoring, near-dup dedup, face clustering.
- [**argus-lens**](https://github.com/smk762/argus-lens) — intent-aware, multi-model captioning.

## Licence

MIT — see [LICENSE](LICENSE). Note: the MIT licence covers *this software*, not
the images it downloads. Image licences are recorded per-record and enforced at
ingest.
