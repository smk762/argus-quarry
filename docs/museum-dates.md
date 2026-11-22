# Museum catalogue dates for PD12M (#52): the Met

PD12M records only the institution and its own S3 copy of each image, not the museum object it came from. The `museum_*` columns fill that gap for the Metropolitan Museum of Art: the object an image came from, its page, and the museum's own catalogue date for it.

## Matching is exact

PD12M names each S3 copy `uuid5(NAMESPACE_URL, <the museum's image URL>)`. For example, the Met's `https://images.metmuseum.org/CRDImages/ad/original/DP239096.jpg` (object 4706) is PD12M's `images/92889c07-87c0-5e18-b16c-af75cb6b04a8.jpeg`. A Met object whose `primaryImage` or one of its `additionalImages` hashes to a PD12M filename is the object that image came from. Matching therefore needs no image download, no title search and no similarity threshold: it is a join on the filename.

PD12M's `hash` column is the MD5 of its S3 copy. That copy is sometimes byte-identical to the Met's current original and sometimes not (the Met has re-shot or re-encoded some images since), so the hash can confirm a match but can't rule one out. Precision was measured by comparing the images instead (below).

## Measurement (2026-11-22, `scripts/measure_museum_match.py`)

The sample was 3,000 Met object ids drawn at random (seed 52) from the full collection of 503,479, fetched from the Met's open API. The summary is in [`museum-match.json`](museum-match.json).

| | |
|---|---|
| public-domain objects with images in the sample | 1,538 (51%) |
| of those, with an image PD12M holds | **903 (58.7%, 95% CI 56.2–61.2%)** |
| matched objects with a catalogue date | 99.9% |
| PD12M Met images a full harvest would match (scaled up from the sample) | ~153,000 of PD12M's 149,458, i.e. essentially all of them within sampling error |
| precision: matches whose images are the same picture | **60 / 60 (95% CI 94.0–100%)** |

- **Precision check.** For 60 random matches, the Met's web-size rendition and PD12M's copy were both downloaded and compared. All 60 had a perceptual-hash distance of 10 bits or less (of 64) and the same aspect ratio within 2%.
- **What doesn't match.** About 41% of the Met's public-domain objects with images have none in PD12M. That is the Met-side miss: PD12M's builders kept a subset. From the PD12M side, essentially every Met item is a Met URL the harvest can reach.
- **Non-public-domain objects** (49% of the sample) have no images in the API at all, so they never match.

## The harvest

```bash
argus-quarry archives museum pd12m --limit 3000   # a sample run; repeat to continue
argus-quarry archives museum pd12m                # the rest: resumable, stop and re-run any time
argus-quarry archives search pd12m --museum-year-min 1850 --museum-year-max 1899 --q portrait
```

The harvest fetches the Met's id list once (`/objects`; the paginated search caps at 10,000 hits), then one `/objects/{id}` per object. Answers are appended to `$QUARRY_HOME/cache/archives/pd12m.met.jsonl` and compiled into `pd12m.museum.parquet` (image uuid → object), which every `archives index` joins.

**Rate and blocks.** The Met's documentation allows 80 requests a second, but its firewall (Imperva) answers `403` once an IP has made a few hundred requests in a short window. That happened at 10/s and at 2/s alike. It lets the IP back in after a pause. At the default of 1 request/second, a 47-minute run had no blocks. A `403`/`410`, or a non-JSON page where JSON belongs, is never cached as "gone" (only a `404` is). The harvest pauses `--block-wait` seconds (default 300) and resumes, and gives up after 50 blocks. An early test run recorded firewall refusals as deleted objects, which is why this matters.

**Cost.** The whole collection is about 503k requests, nearly 6 days at 1 request/second. Every non-public-domain object in the sample had no images. Prefiltering ids with the Met's Open Access CSV (`Is Public Domain`) would therefore roughly halve that. That isn't done yet.

## The columns and filters

| column | |
|---|---|
| `museum` | `met` |
| `museum_object_id`, `museum_object_url` | the matched object and its page (provenance) |
| `museum_date` | the catalogue's text, e.g. `ca. 1815`, `5th century B.C.` |
| `museum_year_begin`, `museum_year_end` | the catalogue's year range (`objectBeginDate` / `objectEndDate`; BCE is negative). An undated object (`0`/`0` with no text) is `NULL`, not the year 0 |

`museum_year_min` / `museum_year_max` match only objects whose **whole** catalogue range lies inside the filter: an object dated 1800–1900 is not "from 1890". Unmatched items are left out by these filters, never guessed. Facets name the source (`date_sources.museum_year: "museum-catalogue"`) beside `year` (caption), `clip_era` (CLIP zero-shot) and `upload_year` (Flickr upload).

The date is the museum's date for the **object**. For a photograph that is when it was made, for a painting when it was painted, and for a photograph *of* an object (a vase, a coin) the object's date, not the photo's.
