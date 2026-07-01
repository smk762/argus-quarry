# Argus Quarry

Provenance-first acquisition of public-domain / CC0 portraits — the **input
stage** of the Argus suite. The *quarry* digs up raw material: it downloads
images from upstream archives and lands them, **with full provenance and
licensing**, into a folder the rest of the suite already consumes
(`DATASET_DIR` → `/data/images`).

It is deliberately lean — *acquisition + provenance, nothing more*. Quality
scoring, near-duplicate detection, faces, embeddings and captioning are owned
downstream by [argus-curator](https://github.com/smk762/argus-curator) and
[argus-lens](https://github.com/smk762/argus-lens); quarry never re-implements
them.

```
argus-quarry (NEW)          argus-curator (:8101)        argus-lens (:8100)
─ download  ─┐              ─ scan + score  ─┐           ─ caption ─┐
─ verify    ─┤   images +   ─ near-dup      ─┤  manifest ─ buckets ─┤   dataset → LoRA
─ provenance┤   provenance  ─ face-cluster  ─┤           ─ (ident/ ─┘
─ SHA256    ─┴───────────►  ─ select+export ─┴──────────►  wardrobe)
   /data/images (DATASET_DIR) ────────────────────────────►
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
- **Source-independent.** Every downloader yields the same `PortraitRecord`, so
  the pipeline never learns which archive a file came from.

## Install

```bash
make dev            # editable install with dev + cli extras
# or
pip install -e ".[cli]"
```

## Quickstart

```bash
# Inspect the curated people seed
argus-quarry people

# Fetch portraits from Wikimedia Commons into the raw pool, then publish
# a curator-ready, CC0/PD-only tree into $DATASET_DIR (symlinks by default)
argus-quarry run --source commons --limit 20 --export --licence CC0,PD

# Or split the two stages
argus-quarry fetch --source commons --limit 20
argus-quarry export --dest ./data --licence CC0,PD   # add --copy to avoid symlinks

# Inspect what you have
argus-quarry stats
argus-quarry list --licence CC0
argus-quarry verify              # re-check files decode + match recorded SHA256
```

## Layout produced

Quarry fetches into a **raw pool** it fully owns (`$QUARRY_HOME`, a sibling
side-car dir), then `export` publishes a clean tree into `DATASET_DIR`:

```
$QUARRY_HOME/                    # side-car state — NEVER scanned by curator
├── images/Albert_Einstein/…     # the raw pool (every byte quarry landed)
├── metadata/portraits.sqlite    # provenance DB (people + photographs)
├── cache/  logs/

$DATASET_DIR/                    # published, curator-ready view (via export)
└── Albert_Einstein/…            # symlinks (default) or copies into the pool
```

## Configuration

Copy `.env.example` to `.env`. Key knobs:

| Env | Default | Meaning |
|---|---|---|
| `QUARRY_HOME` | `./quarry` | Raw pool + DB + cache + logs (side-car dir) |
| `QUARRY_MAX_GB` | `40` | Total raw-pool ceiling; `0` = unlimited |
| `COMMONS_USER_AGENT` | descriptive default | Polite UA (Commons/LoC expect one) |
| `DATASET_DIR` | `./data` | Published view `export` writes into |

## Suite integration

Bring quarry up as the `gallery` profile (a run-to-completion job) to fetch then
publish, then curate the published view:

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

## Development

```bash
make lint     # ruff
make test     # pytest
make check    # lint + test + build
```

## Licence

MIT — see [LICENSE](LICENSE). Note: the MIT licence covers *this software*, not
the images it downloads. Image licences are recorded per-record and enforced at
ingest.
