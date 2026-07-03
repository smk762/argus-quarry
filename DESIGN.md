# argus-quarry — Design Doc (Draft)

> Status: **proposal / plan only** — no code yet.
> Owner: smk762 · Suite: Argus · Sibling of `argus-lens`, `argus-curator`, `argus-studio`.

The *quarry* is where the suite digs up raw material. `argus-quarry` acquires
public-domain / CC0 images from upstream archives and lands them — **with full
provenance and licensing** — into a folder the rest of the Argus suite already
knows how to consume (`DATASET_DIR` → `/data/images`).

Subjects are grouped into LoRA-training **categories** — `identity` (people),
`wardrobe` (garments), `setting` (scenes/environments) and `concept`
(styles/themes) — and land sorted into `<category>/<subject>/` subfolders. A
category is independent of identity: a subject can be a person, a garment, a
scene, or a visual concept.

It is deliberately **lean**: an *acquisition + provenance* tool, nothing more.
Everything downstream (quality scoring, near-dup, faces, embeddings, selection,
captioning, viewing) is already owned by `argus-curator` and `argus-lens`, and
`argus-quarry` must not re-implement it.

---

## 1. Where it fits in the suite

```
argus-quarry (NEW)          argus-curator (:8101)        argus-lens (:8100)        imogen / kohya
─ download  ─┐              ─ scan + score  ─┐           ─ caption ─┐              ─ train ─
─ verify    ─┤   images +   ─ near-dup      ─┤  manifest ─ buckets ─┤   dataset    ─ LoRA  ─
─ provenance┤   provenance  ─ face-cluster  ─┤           ─ (ident/ ─┴──────────►  ───────►
─ SHA256    ─┴───────────►  ─ select+export ─┴──────────► wardrobe)
   /data/images (DATASET_DIR)  ───────────────────────────────►
```

`argus-quarry` sits **upstream** of everything. It is a *producer* of
`DATASET_DIR`; curator and lens are the *consumers*. The only integration
surface is the shared images folder plus a new `gallery` compose profile —
the exact loosely-coupled pattern the suite already uses.

---

## 2. Scope

### In scope (the genuinely new capability)

- **Source downloader modules** — one per archive, behind a common contract.
- **Provenance & licensing capture** — never lose source URL, licence, or
  attribution. This is the reason the tool exists ("provenance-first").
- **Resumable, rate-limited, retrying downloads** with integrity verification.
- **Exact dedup at ingest** — SHA256 only. Skip bytes we already have.
- **Provenance database** — SQLite: `subjects` + `photographs`.
- **Category-aware subject seeds** (identity / wardrobe / setting / concept).
- **A thin, category-sorted folder layout** that lands cleanly as `DATASET_DIR`.

### Out of scope (delegated — do NOT rebuild)

| Concern | Owned by | Why not here |
|---|---|---|
| Near-duplicate (pHash) detection | `argus-curator` | Curator already keeps the best representative and reports the rest. |
| Quality metrics (sharpness/blur/contrast/entropy/jpeg…) | `argus-curator` | Curator's scoring is *training-suitability* aware; a second stack would diverge. |
| Face detection / clustering / bounding boxes | `argus-curator` | InsightFace clustering already lives there. |
| CLIP / face embeddings | `argus-curator` (`gpu`/`faces`) | Same. |
| Quality/identity search & ranking | `argus-curator` manifest + CSV | Provenance search stays here; *quality* search is curator's. |
| Captioning | `argus-lens` | — |
| Rich gallery UI | `argus-studio` frontend | Avoid a second UI (see §9). |

**Net effect vs. the original brief:** the `quality` table and all CV
(quality/faces/embeddings) sections are dropped from quarry. The DB shrinks to
`subjects` + `photographs`. `phash` is optional metadata only (recorded if cheap,
never the basis of quarry's dedup — SHA256 is). Search is provenance/licence
oriented, not quality/ranking oriented.

---

## 3. Repo layout (mirrors argus-curator conventions)

```
argus-quarry/
├── pyproject.toml            # hatchling, src layout, optional-dependency extras
├── Makefile                  # help/install/dev/lint/fmt/test/build/smoke
├── Dockerfile
├── README.md
├── DESIGN.md                 # this file
├── LICENSE                   # MIT
├── src/argus_quarry/
│   ├── __init__.py           # exports + __version__
│   ├── py.typed
│   ├── models.py             # SourceRecord, Subject, Photograph (pydantic)
│   ├── store.py              # SQLite provenance DB (sqlite3 stdlib, WAL)
│   ├── ingest.py             # download → verify → SHA256 dedup → land → record
│   ├── net.py                # httpx client: rate limit, retry/backoff, resume
│   ├── config.py             # QuarryConfig: per-source settings, resolution + total-GB caps
│   ├── subjects.py           # load per-category seed lists; optional Wikidata SPARQL harvester
│   ├── cli.py                # typer app: run / fetch / export / list / stats / verify / subjects
│   ├── seeds/
│   │   ├── identity.yaml     # curated persons seed (name, wikidata_id, aliases)
│   │   ├── wardrobe.yaml     # garments / outfits
│   │   ├── setting.yaml      # scenes / environments
│   │   └── concept.yaml      # styles / themes / objects
│   ├── downloaders/
│   │   ├── __init__.py       # registry (name -> Downloader)
│   │   ├── base.py           # Downloader protocol / ABC -> yields SourceRecord
│   │   ├── commons.py        # Wikimedia Commons        (Phase 1)
│   │   ├── loc.py            # Library of Congress       (Phase 2)
│   │   ├── smithsonian.py    # Smithsonian Open Access   (Phase 2)
│   │   ├── rijksmuseum.py    # Rijksmuseum Open Data      (Phase 2)
│   │   ├── lac.py            # Library & Archives Canada  (Phase 2, Karsh)
│   │   ├── europeana.py      # Europeana                  (Phase 3, rights-messy)
│   │   └── flickr.py         # Flickr Commons (optional)  (Phase 3)
│   └── server/               # read-only provenance API (`serve`, :8102) — see §9
└── tests/
```

Package name `argus_quarry`, distribution `argus-quarry`, CLI entrypoint
`argus-quarry` (Typer), structlog for logging, pydantic v2 for models —
identical toolchain to curator so the suite stays consistent.

---

## 4. The common contract: `SourceRecord`

Every downloader is source-independent because it yields the same object. The
rest of the pipeline never learns which archive a file came from.

```python
class SourceRecord(BaseModel):
    # subject / category
    subject: str                     # canonical folder name, e.g. "Albert_Einstein" | "Red_dress"
    category: str = "identity"       # identity | wardrobe | setting | concept

    # identity-only metadata (None for non-person subjects)
    wikidata_id: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    occupation: str | None = None

    # the asset
    title: str | None = None
    photographer: str | None = None
    year: int | None = None
    remote_url: str                  # full-resolution source URL

    # provenance / licence  (NEVER optional in spirit — this is the point)
    source: str                      # "commons" | "loc" | ...
    source_url: str                  # human-facing landing page
    licence: str                     # "PD" | "CC0" | "PD-US" | ...
    attribution: str | None = None   # required credit line if any
```

`Downloader.harvest(subject) -> Iterator[SourceRecord]` streams candidates
(searching with `subject.query`, stamping each record's category);
`ingest.py` turns each into bytes on disk + a DB row (idempotently), landing
under `<category>/<subject>/`. (`PortraitRecord` / `Person` remain importable as
backward-compatible aliases of `SourceRecord` / `Subject`.)

### The subject seeds (Q2, resolved: hybrid + per-category)

The subject list is decoupled from the downloaders. `subjects.py` supplies the
subjects each downloader harvests around, one curated YAML per category, from
two interchangeable sources:

- **Curated seeds (default, deterministic):** `seeds/<category>.yaml` — small,
  hand-maintained lists (`name`, optional `search`, `aliases`; plus
  `wikidata_id` / birth / death for `identity`). This is what dev/QA runs against
  so results are reproducible and licence-safe. `load_subjects()` merges all
  categories; `--category` restricts to one.
- **Wikidata SPARQL harvester (optional, `--from-wikidata`, identity only):**
  query "humans with a Commons portrait" (+ filters like occupation / death-year
  for PD likelihood) to scale toward the 5–7k target. Cached under
  `QUARRY_HOME/cache`.

Both resolve to the same `Subject` shape, so downloaders never care which was
used. Seeds ship in Phase 1; SPARQL harvester lands in Phase 2.

---

## 5. Data model (SQLite)

Two tables. Provenance-first; no CV columns.

**subjects**
`id · name · category · wikidata_id · birth_year · death_year · occupation`
(unique on `(name, category)`; identity-only columns are `NULL` elsewhere)

**photographs**
`id · subject_id (fk) · category · title · photographer · year · source · source_url ·
licence · attribution · width · height · file_size · filename · sha256 (unique) ·
phash (nullable, informational) · remote_url · status · downloaded_at`

- `sha256` is `UNIQUE` → exact-dup ingest is a no-op (idempotent reruns).
- `status` tracks resumability: `pending | downloading | complete | failed`.
- `phash` recorded opportunistically (cheap with Pillow+ImageHash) but **never**
  drives dedup here — that's curator's job.
- SQLite in WAL mode; single writer, safe concurrent readers.

Deliberately **no `quality` table** (dropped from the brief — see §2).

---

## 6. Folder structure produced

Two-stage layout: quarry fetches into a **raw pool** it fully owns, then
`export` publishes a clean, curator-ready tree into `DATASET_DIR`.

```
$QUARRY_HOME/                     # sibling ./quarry — side-car state, NEVER scanned
├── images/                       # the RAW POOL — every byte quarry has landed
│   ├── identity/
│   │   └── Albert_Einstein/
│   │       ├── albert_einstein_1921_commons_<sha8>.jpg
│   │       └── ...
│   ├── wardrobe/Red_dress/...
│   ├── setting/Modern_kitchen/...
│   └── concept/Cyberpunk/...
├── metadata/portraits.sqlite
├── cache/                        # HTTP cache / partial downloads (resume)
├── logs/
└── thumbnails/                   # OPTIONAL; curator makes its own previews

$DATASET_DIR/                     # == /data/images — PUBLISHED view (via `export`)
├── identity/Albert_Einstein/     # symlinks (default) or copies into the pool
│   └── albert_einstein_1921_commons_<sha8>.jpg -> $QUARRY_HOME/images/...
├── wardrobe/Red_dress/...
└── ...
```

Key decisions (Q4 + Q5, resolved):

- **`QUARRY_HOME` is a sibling `./quarry` dir**, fully outside the image tree, so
  the DB/cache/logs a curator scan would choke on are never in view.
- **Images land in the raw pool first**, then `argus-quarry export` builds the
  `<category>/<subject>/` tree in `DATASET_DIR` — **symlink by default** (cheap, no
  duplication), `--copy` when a mount can't cross the boundary. This keeps
  quarry's provenance-complete pool separate from the curated view: you can
  re-publish a subset (e.g. only `licence = CC0`) without re-downloading, and a
  curator scan only ever sees clean images.

---

## 7. Downloader requirements

Each source module must:

- **Resume** interrupted downloads (partial-file + `status` in DB).
- **Skip** anything already `complete` (by `remote_url` / expected `sha256`).
- **Respect rate limits** (per-source token bucket in `net.py`; polite `User-Agent`).
- **Retry** transient network errors with exponential backoff + jitter.
- **Verify integrity** (content-length, decodes as an image via Pillow).
- **Record licence + attribution** — a record with no licence is quarantined,
  not landed.
- **Prefer high resolution within a configurable cap** (Q3): request the largest
  rendition the API offers, but downscale/skip past a per-file ceiling
  (`QuarryConfig.max_megapixels` default ~12 MP, `max_file_bytes` default a few
  MB), overridable per run. The **full-resolution `remote_url` is always kept in
  the DB**, so a capped image can be re-fetched at original size on demand
  without losing provenance. Keeps the archive inside the 20–40 GB budget by
  default while never throwing away the ability to go bigger.
- **Respect a total-archive budget** (`QuarryConfig.max_total_gb`, env
  `QUARRY_MAX_GB`, default e.g. `40`; `0`/unset = unlimited): before each write,
  check the current raw-pool size (`QUARRY_HOME/images`, tracked incrementally
  from `photographs.file_size` so it's O(1), not a directory walk). When the
  next file would exceed the ceiling, **stop the run cleanly** — mark remaining
  candidates `pending` (resumable later if the cap is raised), log a
  `budget_reached` event, and exit non-error. This bounds disk use predictably
  for dev/QA regardless of how many sources/subjects are queued.
- **Log all failures** (structlog → `logs/`), never crash the whole run.

Everything is **idempotent**: rerunning `fetch` resumes/repairs, never duplicates.

---

## 8. Suite integration (compose)

Add a `gallery` profile to the demo's `compose.yaml`. It's a run-to-completion
job (not a long-lived server). Quarry fetches into its own pool
(`$QUARRY_HOME/images`) and then publishes into `DATASET_DIR` — both mounts are
present so a single `up` can fetch-then-export:

```yaml
  argus-quarry:
    profiles: ["gallery"]
    build:
      context: ../argus-quarry
    image: argus-quarry:latest
    environment:
      - QUARRY_HOME=/data/quarry
      - QUARRY_MAX_GB=${QUARRY_MAX_GB:-40}       # total raw-pool ceiling; 0 = unlimited
      - COMMONS_USER_AGENT=${COMMONS_USER_AGENT:-argus-quarry/0.1 (contact@example.com)}
    volumes:
      - ${QUARRY_HOME:-./quarry}:/data/quarry    # raw pool + db/cache/logs
      - ${DATASET_DIR:-./data}:/data/images      # published (curator-ready) view
    # fetch into the pool, then publish a symlinked tree into DATASET_DIR
    command: ["run", "--source", "commons", "--limit", "500", "--export", "--licence", "CC0,PD"]
    restart: "no"
```

> Symlinks only resolve inside the container if both targets are mounted; since
> the published tree points back into `/data/quarry/images`, the curator/lens
> containers must also mount `QUARRY_HOME` **or** quarry should publish with
> `--copy`. Simplest for the suite: curator/lens add the same
> `${QUARRY_HOME}:/data/quarry` read-only mount. Documented in `.env.example`.

Usage stays true to the suite's profile idiom:

```bash
docker compose --profile gallery up --build      # fetch -> pool -> publish DATASET_DIR
docker compose --profile curator up --build       # then curate the published view
```

New `.env` knobs (documented in `.env.example`): `QUARRY_HOME` (default
`./quarry`), `QUARRY_MAX_GB` (total raw-pool ceiling, default `40`, `0` =
unlimited), per-source API keys / contact `User-Agent` strings (Commons and LoC
want a real UA; Rijksmuseum and Europeana need API keys).

---

## 9. On the "local viewer"

The brief asks for a Flask/FastAPI viewer. That overlaps with the existing
Next.js frontend, so:

- **Phase 1–2:** no standalone UI. `argus-quarry stats` / `list` on the CLI is
  enough to inspect provenance.
- **Later (optional) — now built:** a tiny read-only FastAPI `server/`
  (`argus-quarry serve`, the `server` extra, `:8102`) exposing provenance
  queries: `/health`, `/stats`, `/subjects`, `/photos` (filter by subject /
  category / source / licence / status, paginated), `/photos/{id}` and a
  `/thumb` WEBP renderer over the pooled files. The demo frontend surfaces it
  as the `/gallery` route — consistent with how `/curate` already talks to
  curator. Strictly read-only: no mutation endpoints.

This keeps us to one real UI (the demo) instead of maintaining a second.

---

## 10. Licensing / feasibility notes (for the 5–7k, 20–40 GB target)

- **Reliable PD/CC0 with real APIs:** Wikimedia Commons, Library of Congress,
  Smithsonian Open Access, Rijksmuseum Open Data. Start here.
- **Messier rights:** Europeana and Flickr Commons mix licences per-item — the
  downloader must read per-record rights and quarantine anything not clearly
  PD/CC0.
- **LAC / Karsh:** many Karsh works are *not* PD (photographer d. 2002); treat
  as a curated allow-list, not a bulk scrape.
- For dev/QA this dataset is plenty; do **not** advertise uniformly clean
  licences across every source — enforce it per-record instead.

---

## 11. Design principles (unchanged from the brief, enforced by the above)

Modular · source-independent (`SourceRecord`) · idempotent (SHA256 + `status`)
· extensible (downloader registry) · category-sorted (identity / wardrobe /
setting / concept) · reproducible · **provenance-first** (a record with no
licence never lands) · optimised as the *input* to the suite's existing
CV/search/curation stages rather than duplicating them.

---

## 12. Phased delivery

**Phase 1 — walking skeleton**
- `pyproject.toml` (extras: `cli`, `server`, `dev`), `Makefile`, `Dockerfile`.
- `models.SourceRecord`, `store` (SQLite `subjects`+`photographs`, WAL, migrations).
- `net` (rate limit + retry + resume), `ingest` (download→verify→cap→SHA256→pool→record, `<category>/<subject>/`).
- `subjects.py` + per-category `seeds/*.yaml` (identity / wardrobe / setting / concept) curated loaders.
- `downloaders/commons.py` (Wikimedia Commons).
- `export` (symlink/`--copy` published tree, with `--licence` / `--category` filters).
- Typer CLI: `run` (fetch+export), `fetch`, `export`, `list`, `stats`, `verify`, `subjects`.
- `gallery` compose profile wired into `argus-studio/compose.yaml`.

**Phase 2 — breadth**
- `loc`, `smithsonian`, `rijksmuseum`, curated `lac` (Karsh allow-list) downloaders.
- Wikidata SPARQL identity harvester (`subjects --category identity --from-wikidata`); incremental update mode.
- Opportunistic `phash` metadata (informational only).

**Phase 3 — polish (optional)**
- `europeana`, `flickr` with strict per-record rights filtering.
- Read-only provenance FastAPI + `/gallery` route in the demo frontend.

---

## 13. Resolved decisions

1. **Name** — `argus-quarry` (acquisition connotation, no clash with the
   frontend's "viewing"). ✅
2. **Subject list** — **hybrid + per-category**: curated `seeds/<category>.yaml`
   (identity / wardrobe / setting / concept) for deterministic dev/QA (Phase 1),
   plus an optional Wikidata SPARQL harvester (identity) to scale toward 5–7k
   (Phase 2). See §4. ✅
3. **Resolution** — **configurable per-file cap** (~12 MP / few MB default),
   full-res `remote_url` retained for on-demand re-fetch; overridable per run.
   Keeps the 20–40 GB budget without discarding fidelity. See §7. ✅
4. **`QUARRY_HOME`** — sibling **`./quarry`** dir, fully outside the image tree.
   See §6. ✅
5. **Landing** — **raw pool + export**: fetch into `QUARRY_HOME/images`, then
   `export` publishes a `<category>/<subject>/` tree into `DATASET_DIR` (symlink
   default, `--copy` fallback), with optional `--licence` / `--category` filters.
   See §6/§8. ✅
6. **Categories** — subjects are grouped into identity / wardrobe / setting /
   concept and land in `<category>/<subject>/` subfolders, so one pool serves
   multiple LoRA-training workflows. See §4/§6. ✅

### Follow-ups that surfaced while resolving

- **Symlink cross-mount:** for the published symlink tree to resolve inside
  curator/lens containers, they must also mount `QUARRY_HOME` read-only, else
  quarry publishes with `--copy`. Needs a one-line `.env.example` + compose note
  when wiring the `gallery` profile (already flagged in §8).
- **Wikidata → PD likelihood:** SPARQL harvester should pre-filter on death-year
  / country to reduce quarantines, but licence is still enforced per-record at
  ingest (never trust the query alone).
