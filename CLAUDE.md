# CLAUDE.md — argus-quarry

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely. The deeper rationale (and section numbers referenced in the code) lives in [DESIGN.md](DESIGN.md).

## What this is

The **acquisition** stage — first in the Argus suite: it digs up raw material, acquiring public-domain / CC0 images from upstream archives across the LoRA-training taxonomy (identity / wardrobe / setting / concept) and landing them, with full provenance and licence, into a category-sorted raw pool the rest of the suite consumes.

```
argus-quarry -> argus-curator -> argus-lens -> argus-forge -> your trainer -> argus-proof
  acquire        curate/export    caption       configs        LoRA           validate
```

Deliberately lean: **acquisition + provenance only**. Quality scoring, near-dup, faces and captioning are owned downstream (curator/lens) — do not rebuild them here.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/main/SUITE.md

## Layout

`src/argus_quarry/`: `models.py` (Pydantic contract + licence helpers), `config.py` (`QuarryConfig`; every path derives from `$QUARRY_HOME`), `store.py` (SQLite provenance DB, migrations, read-only open), `net.py` (polite `httpx`), `downloaders/` (one module per source; register in `__init__.py`), `subjects.py` + `seeds/*.yaml`, `exif.py` (EXIF device → `capture_medium`; the one scanner rule, shared with `scripts/film_digital_set.py`), `ingest.py` (licence gate → download → verify → cap → dedup → land), `export.py`, `cli.py`, `archives/` (DuckDB-indexed PD12M / Megalith metadata, `semantic.py` CLIP search, `mirror.py` Megalith tar offsets, `upload_year.py` + `data/flickr_upload_breakpoints.json` Megalith upload year from the Flickr id, `flickr_dates.py` keyless/API upload-date lookups that `scripts/calibrate_flickr_upload.py` measures the table with, `museum.py` the Met harvest and image->object table behind PD12M's `museum_*` dates, measured by `scripts/measure_museum_match.py`), `server/` (FastAPI on **:8102**). `scripts/fetch_public_domain_metadata.py` archives the datasets' metadata from Hugging Face.

Facts the code doesn't show: `items.pos` is the PD12M embedding row, derived by concatenating the embedding files in sorted-filename order; keep that derivation in step with the file order or semantic hits map to the wrong items. Three caps: megapixels (downscale on landing), `max_file_bytes` / `$QUARRY_MAX_FILE_MB` (a per-download ceiling, default 100 MiB, set well above ordinary originals so it rejects only runaway files; an over-cap row is `oversize` and skipped until the cap is raised past the bytes it saw), and `$QUARRY_MAX_GB` (the pool).

## Commands

```bash
make dev     # uv venv + editable install with [dev,cli]
make test    # uv run --no-sync pytest --tb=short -q
make lint    # ruff check (CI pins ruff 0.15.20; format --check too)
make fmt     # ruff format + --fix
make check   # lint + test + build
```

Run one test: `uv run --no-sync pytest tests/test_ingest.py::test_name -q`.

## Must never happen

Each rule names the check that fails when it's broken. A new rule gets a check, not just prose here; a rule about code shape (a banned import or API) can go in argus-ci's suite-rules catalogue (smk762/argus-ci#16) instead.

| Must never happen | Enforced by |
|---|---|
| A downloader or archive decides licence policy (filters, or imports the gate) | `test_commons::test_commons_reports_a_non_free_licence_instead_of_filtering_it`, `test_commons::test_only_ingest_applies_licence_policy`, `test_ingest::test_non_free_licence_is_quarantined` |
| A licence label is accepted on a substring, or a negated / qualified label is accepted | `test_models::test_normalise_licence` |
| Anything but `sha256` dedups; `phash` drives dedup | `test_ingest::test_exact_duplicate_bytes_are_deduped`, `test_ingest::test_a_matching_phash_never_dedups` |
| A read path mutates the pool, or reads past an un-checkpointed WAL | `test_server::test_read_only_home_with_a_hot_wal_is_unavailable_not_stale`, `test_server::test_legacy_pool_served_read_only_is_unavailable_not_500`, `test_store::test_open_readable_rejects_an_unmigrated_pool` |
| The server gains a write route other than the opt-in fetch | `test_server::test_no_mutation_endpoints` (`OPT_IN_WRITE_ROUTES`) |
| A fetch runs without `QUARRY_ALLOW_FETCH=1` | `test_archives::test_fetch_refused_returns_slice_and_command` |
| Two fetches run at once (the second must get 409) | `test_archives::test_a_second_fetch_while_one_runs_is_409` |
| CORS grants POST to `*`, or credentials with `*` | `test_archives::test_cors_allows_post_only_when_fetch_is_enabled` |
| Archive code writes outside `$QUARRY_HOME`, or a reader sees a half-built index | `test_archives::test_archives_write_nothing_outside_quarry_home`, `test_archives::test_rebuild_replaces_index_atomically` |
| Megalith's licence label reads as CC0 | `test_archives::test_megalith_licence_label_reads_as_pd_never_cc0` |
| A Flickr upload year is named or labelled as a capture date (`upload_year`, `date_sources: flickr-upload`) | `test_upload_year::test_the_committed_table_is_monotone_measured_and_says_upload`, `test_upload_year::test_megalith_items_carry_an_upload_year_and_filter_on_it` |
| A museum date is attached by anything but an exact image match (`uuid5(URL, museum image URL)` = the PD12M filename), or loses its matched object id | `test_museum::test_pd12m_names_each_copy_uuid5_of_the_museum_image_url`, `test_museum::test_matched_pd12m_items_carry_the_museum_date_and_object` |
| The Flickr API key (`QUARRY_FLICKR_API_KEY`) reaches a repr, a log line or an error message | `test_flickr_dates::test_the_key_comes_from_the_environment_and_never_shows_in_a_repr`, `test_flickr_dates::test_the_api_key_never_reaches_an_error_message` |
| An alternate is tried on anything but `RemoteGone` (unless preferred), or `remote_url` is rewritten | `test_mirror::test_mirror_is_not_tried_when_flickr_fails_for_other_reasons`, `test_mirror::test_deleted_flickr_photo_lands_from_the_mirror`, `test_mirror::test_prefer_mirror_skips_flickr` |
| A `#bytes=` range accepts a `200` (a whole shard) | `test_mirror::test_ranged_download_refuses_a_server_that_ignores_range` |
| A resumed download splices two different files | `test_net::test_a_changed_resource_restarts_instead_of_splicing`, `test_net::test_a_206_at_the_wrong_offset_is_never_appended` |
| A request skips the per-source rate limit or the polite `User-Agent` | `test_net::test_every_request_is_paced_and_carries_the_user_agent` |
| `fetch_public_domain_metadata.py` downloads a photo or tar shard | `test_fetch_metadata::test_no_part_ever_fetches_a_photo` |
| `$DATASET_DIR` overlaps `$QUARRY_HOME` | `test_export::test_export_refuses_a_dest_that_overlaps_the_pool` |
| Export reads outside `images/` or writes outside the dest | `test_export::test_export_refuses_a_row_whose_path_climbs_out` |
| A photo publishes without its provenance sidecar, or a person photo without `likeness_consent: false` | `test_export::test_export_writes_a_provenance_sidecar_per_file`, `test_export::test_export_marks_a_person_photo_as_not_consented` |
| The pre-0.2 aliases (`Person`, `PortraitRecord`, `load_people`, `people`) or legacy-pool migration break | `test_models::test_pre_0_2_aliases_stay_importable`, `test_cli::test_people_alias_still_works`, `test_store::test_migrates_legacy_people_schema` |
| A download streams past `max_file_bytes`, or an oversize file lands or is re-fetched under the same cap | `test_ingest::test_a_download_over_max_file_bytes_is_aborted_and_marked_oversize`, `test_ingest::test_an_oversize_row_is_skipped_until_the_cap_is_raised` |
| A file naming no EXIF device records `capture_medium: digital`, or the scanner rule lives anywhere but `argus_quarry.exif` | `test_ingest::test_a_file_naming_no_device_records_an_unknown_medium_never_digital`, `test_film_digital::test_the_script_and_ingest_share_one_scanner_rule` |
| A version is hand-edited | `_version.py` is generated by `hatch-vcs` and gitignored |

## Conventions & gotchas

- **Provenance-first is the whole point.** Licence *policy* lives in exactly one place: the ingest licence-gate (`is_accepted_licence`). Downloaders faithfully report the raw licence string on *every* candidate and never filter — ingest quarantines the unaccepted ones centrally. Don't move that decision into a downloader.
- **`sha256` is the only dedup key** (`UNIQUE` in the DB → idempotent reruns). `phash` is opportunistic, informational-only metadata; it must **never** drive dedup here (near-dup is argus-curator's job).
- **Read paths must not mutate the pool** (DESIGN.md §9). `list`/`stats`/`export`/`verify` (without `--repair`) and the server go through `ProvenanceStore.open_readable` / the CLI's `_open_reader`, which tolerate a `:ro`-mounted `$QUARRY_HOME` (issue #5). `open_readable` picks `mode=ro` vs `immutable=1` and **refuses to read past an un-checkpointed WAL** (`ProvenanceUnavailable`) rather than serving stale data — don't loosen this or fall back to a read-write open on a read path.
- **The server is read-only except one opt-in route.** `POST /archives/{name}/fetch` lands an archive slice and is refused (403, with the slice JSON + CLI command in the body) unless `QUARRY_ALLOW_FETCH=1`, the demo-safe posture forge/proof/atlas share. It writes only through `IngestEngine`, one fetch at a time (409 otherwise). Every other route, the provenance routes especially, stays read-only; `tests/test_server.py::OPT_IN_WRITE_ROUTES` is the allow-list, so a new write route has to be added there on purpose. CORS is off by default; with `--cors` it allows `GET`/`HEAD`, plus `POST` only when fetch is enabled *and* an explicit origin list is given (`--cors-origin` / `QUARRY_CORS_ORIGINS`; a listed `*` is folded back into the read-only wildcard) (never to `*`, or any site could drive pool writes from a visitor's browser), and `allow_credentials` is deliberately false unless an explicit origin list is given (never reflect `*` with credentials). A short-lived connection per request keeps a concurrent `fetch`'s WAL visible.
- **Archives never write outside `$QUARRY_HOME`.** The archive dirs are treated as read-only (mount them `:ro`); the index is rebuilt atomically (temp file, then `os.replace`) so a server reading it never sees a half-written file. Megalith has no per-photo licence, so its records state the dataset filter as the licence label; keep that label free of the substring `cc0`, or `normalise_licence` would record Megalith items as CC0 rather than PD.
- **`$QUARRY_HOME` owns the whole raw pool** (`images/`, `metadata/`, `cache/`, `logs/`) as side-car dirs the curator never scans. `export` publishes a *separate* clean tree into `$DATASET_DIR` — keep the two separate. Files land under `images/<category>/<subject>/`.
- **Categories mirror the suite taxonomy** (curator `TargetCategory`): identity / wardrobe / setting / concept. Categories are normalised but not hard-validated so downstream can extend them.
- **Mirrors are alternates, decided in ingest.** A `SourceRecord.alternates` entry is tried only when the primary reports the bytes gone (`RemoteGone`: 403/404/410), or first when `prefer_alternates` is set. Other failures don't fall back. The row's `remote_url` is never rewritten (it stays the full-res primary and the resume key); a mirror that served the bytes is recorded in `fetched_from`, so provenance names the host. A `#bytes=START-END` fragment on a URL is a byte range `NetClient.download` honours; it must get a `206` for exactly that span and refuses a `200`, which would stream a whole ~1.9 GB shard.
- **Full-res `remote_url` is always retained** even when the megapixel cap downscales a landed file, so it can be re-fetched at original size. The total-pool budget (`$QUARRY_MAX_GB`) stops a run *cleanly* and resumably (`BudgetReached`).
- **Backward-compat surface is intentional.** Pre-0.2 was person/portrait-only: keep the `Person`/`PortraitRecord` aliases, the hidden `people` command, and `load_people` importable. `store._migrate` upgrades legacy pools (people→subjects, flat→category layout) in place — preserve that.
- **Versioning is git-tag-derived** (`hatch-vcs`); `src/argus_quarry/_version.py` is generated (gitignored). Never hand-edit a version; tag `vX.Y.Z` to release (PyPI via OIDC + GHCR image). Ruff line-length 120, target py311; `structlog` for logging; Pydantic v2 everywhere.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never open a PR against `main` or push work to it directly: `main` must never be ahead of `dev`. Before a release, cut `[Unreleased]` in `CHANGELOG.md` into a dated `## [X.Y.Z]` section plus its compare link: the tag's GitHub Release takes its notes from it, and `scripts/check_changelog.py` fails the release PR and the tag workflow without it. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected and needs a PR, merge the release PR, then immediately merge `main` back into `dev` so the merge commit is not left on `main` alone.
