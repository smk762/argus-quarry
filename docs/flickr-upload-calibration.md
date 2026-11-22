# Megalith upload year from Flickr photo ids (#53)

`upload_year` dates every Megalith item by **when it was uploaded to Flickr**, estimated from its photo id. It is never when the photo was taken. Flickr Commons institutions upload 19th-century photographs today, and scanned film is uploaded long after it was shot. A 2019 `upload_year` says nothing about whether the photo is digital.

## How it works

Flickr gives photo ids in increasing order as photos are uploaded. A committed table of (photo id, upload time) breakpoints, `src/argus_quarry/archives/data/flickr_upload_breakpoints.json`, maps any id onto an approximate upload time by linear interpolation. At index time it becomes per-year id thresholds in one SQL `CASE`, so dating all 9.58M items costs no request and no join. Ids outside the measured range (below the smallest or above the largest sampled id) get `NULL`, never an extrapolated year.

## Measurement (2026-11-22)

`scripts/calibrate_flickr_upload.py` built the table. The answers it got are in [`flickr-upload-samples.jsonl`](flickr-upload-samples.jsonl), so the table can be re-fitted without asking Flickr again.

- **Sample.** 747 Megalith photo ids: one from each of 700 equal-count id strata, both ends of the id range, and 60 log-spaced bands below the 5th-percentile id. Equal-count strata alone leave 2004–2009 nearly unsampled, because early ids are sparse and grew roughly exponentially.
- **Ground truth.** Each photo's `datePosted`, read keyless from its public Flickr photo page. 704 photos answered and 43 (5.8%) were gone. There were no parse errors.
- **Fit and test.** The answers were sorted by id and split alternately: 353 to fit the table and 351 held out. Each breakpoint is the median id and median time of 15 consecutive fit samples, and the times are then made non-decreasing. The bin size was chosen by 2-fold cross-validation inside the fit half (exact-year rate for 5 / 9 / 15 / 25 / 35: 0.78 / 0.78 / 0.80 / 0.71 / 0.60). The held-out half was never used to choose anything.

### Held-out error (351 photos)

| | exact year | within 1 year | median error | 90th percentile |
|---|---|---|---|---|
| all | **87.8%** | **94.3%** | 14 days | 138 days |
| uploaded 2004–2010 (31) | 61.3% | 93.5% | 106 days | 439 days |
| uploaded 2011–2014 (31) | 93.5% | 100% | 61 days | 133 days |
| uploaded 2015–2018 (199) | 87.9% | 92.5% | 14 days | 546 days |
| uploaded 2019–2021 (90) | 94.4% | 96.7% | 7 days | 32 days |

All the large misses go one way. 29 of the 351 photos (8%) were posted more than a year *after* the time their id implies, and none more than a year before. That matches photos uploaded privately and made public later, or posted dates edited later; their ids still sit with photos uploaded at the same time. For those photos the id is probably the better guide to the upload, so these numbers overstate the error, if anything. The 15-sample median is what stops such photos bending the table.

### Coverage

All 9,580,688 Megalith items get an `upload_year`, because the table spans the full id range. By year: 2015–2018 hold 61%, 2019–2021 27%, and 2004–2010 2.5%. Ids above 51,705,578,255 all read as 2021: the newest sampled photo's posted date was older than its neighbours', so the table's last stretch is flat.

## Rebuilding the table

```bash
uv run --no-sync python scripts/calibrate_flickr_upload.py \
    --links "$QUARRY_MEGALITH_DIR/links/data/*.parquet" \
    --cache docs/flickr-upload-samples.jsonl --write
argus-quarry archives index megalith
```

Answers already in `--cache` are reused, and new ids are fetched at `--rate` requests per second (default 1). Without a key the dates come from the public photo pages. With `QUARRY_FLICKR_API_KEY` set they come from `flickr.photos.getInfo` (`dates.posted`). Flickr issues API keys only to Pro accounts. The key is held as a secret and never appears in a log line, a repr or an error. The API's `dates.posted` should be the same posted time the page shows. That has not been checked yet, because this table was built keyless.
