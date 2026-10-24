# Changelog

All notable changes to argus-quarry are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions come from git tags (`hatch-vcs`). A `v*` tag publishes the PyPI package and `ghcr.io/smk762/argus-quarry`.

## [0.3.0] - 2026-10-24

### Added

- **Public-domain archives** ([#18](https://github.com/smk762/argus-quarry/pull/18)): search and slice the local metadata of **PD12M** (12.4M CC0 / Public Domain Mark images from 640 institutions) and **Megalith-10m** (9.58M Flickr photos marked public domain, CC0, Flickr Commons or US Gov) without downloading their images. A chosen slice lands in the pool through the normal ingest pipeline: licence gate, SHA256 dedup, provenance.
  - `scripts/fetch_public_domain_metadata.py` archives links, captions, sizes and PD12M's CLIP embeddings from Hugging Face, with a `SOURCE.json` of pinned revisions. About 45 GB, no photos.
  - `QUARRY_PD12M_DIR` / `QUARRY_MEGALITH_DIR` point at the archive. `argus-quarry archives index` builds a DuckDB index under `$QUARRY_HOME/cache/archives/`, memory-bounded (`--memory-limit`, default 4 GB) and rebuilt atomically.
  - Filters:
    - caption terms, with `"phrases"` and `-term` exclusions, plus a separate `exclude`;
    - institution or Flickr account;
    - licence and MIME type;
    - minimum width, height or short side;
    - orientation, and known-size-only;
    - caption-derived **tone** (bw / color), **medium** (photo, painting, drawing, print, map, text, object) and **year range**.

    Results come in a fixed shuffle or largest-first.
  - CLI: `archives status | index | search | fetch slice.json [--dry-run]`.
  - Server: `GET /archives`, `/archives/{name}/facets`, `/search`, `/items/{id}`, `/thumb` (PD12M thumbnails are rendered in memory; Megalith thumbnails redirect to Flickr's 500 px rendition) and `/slice`, which returns the saveable spec plus the CLI command.
  - **`POST /archives/{name}/fetch`** lands a slice and streams NDJSON progress. It is the server's one write route and is **off unless `QUARRY_ALLOW_FETCH=1`**. When off, it answers 403 with the slice and the CLI command. CORS allows `POST` only while fetch is enabled, and only to an explicit origin list, never `*`.
  - New `archives` extra (duckdb), included in `all`, `dev` and the image.

### Changed

- The server is no longer strictly read-only. The archive slice fetch above is a deliberate, opt-in exception, recorded in DESIGN.md §9 and pinned by `tests/test_server.py::OPT_IN_WRITE_ROUTES`. The provenance routes are unchanged.
- `GET /health` reports `fetch_allowed`.

### Fixed

- Ingest lands TIFF, GIF and WebP originals re-encoded as PNG. Before, they kept their bytes under a `.jpg` name.
- A failed row that a later record retries now lands under the row's own category and subject, which is where the DB and `export` look for it.
- Two different files whose SHA256 shares its first 8 characters no longer overwrite each other in the pool. The second gets the full hash in its name.

### Security

- The image distrusts the compromised eMudhra/emSign roots (`emSign ECC Root CA - G3` and the `Comodo AAA` cross-sign) and installs a hash-pinned `pip 26.2.2`. The package requires `certifi>=2026.10.19`, the bundle httpx verifies against ([#17](https://github.com/smk762/argus-quarry/pull/17)).

### Upgrading

- Nothing changes until you set the archive env vars. To use the archives, archive the metadata, point `QUARRY_PD12M_DIR` / `QUARRY_MEGALITH_DIR` at it (mounted read-only in containers), and run `argus-quarry archives index` once. The build takes about 30 s for PD12M and 4 min for Megalith, and needs about 5 GB of disk under `QUARRY_HOME`.

## [0.2.3] - 2026-07-23

### Fixed

- `GET /health` is pure liveness again. Readiness moved to a new `GET /ready`, which returns 503 until the pool can be served. Conflating them made an empty pool permanently unhealthy ([#12](https://github.com/smk762/argus-quarry/pull/12)).
- The image runs as a non-root user, so the server no longer writes root-owned WAL sidecars into a bind mount ([#13](https://github.com/smk762/argus-quarry/pull/13)).

## [0.2.2] - 2026-07-22

### Fixed

- The read path is genuinely read-only: the server and the read commands open a `:ro` `QUARRY_HOME` without creating, migrating or relocating anything ([#7](https://github.com/smk762/argus-quarry/pull/7)).
- Release workflow: a TestPyPI failure no longer gates the real release ([#6](https://github.com/smk762/argus-quarry/pull/6)).

## [0.2.1] - 2026-07-22

### Added

- Read-only provenance HTTP API (`argus-quarry serve`, `:8102`), which powers argus-studio's `/gallery`.

### Fixed

- The Docker image ships the `[server]` extra and serves by default ([#4](https://github.com/smk762/argus-quarry/pull/4)).

## [0.2.0] - 2026-07-03

### Changed

- Subjects generalised from portraits to the LoRA-training categories identity / wardrobe / setting / concept ([#1](https://github.com/smk762/argus-quarry/pull/1)). Pre-0.2 pools migrate in place.

## [0.1.0] - 2026-07-02

First release: provenance-first acquisition of public-domain / CC0 portraits from Wikimedia Commons into a SQLite-tracked raw pool, with export to a curator-ready tree.

[0.3.0]: https://github.com/smk762/argus-quarry/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/smk762/argus-quarry/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/smk762/argus-quarry/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/smk762/argus-quarry/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/smk762/argus-quarry/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/smk762/argus-quarry/releases/tag/v0.1.0
