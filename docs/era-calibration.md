# CLIP era calibration (PD12M)

How far to trust `clip_era` and `clip_kind` (#19). The raw numbers are in [era-calibration.json](era-calibration.json), and `scripts/calibrate_era.py` reproduces them:

```bash
uv run --no-sync python scripts/calibrate_era.py \
    --index $QUARRY_HOME/cache/archives/pd12m.duckdb \
    --vectors $QUARRY_HOME/cache/archives/pd12m.semantic.<token>/vectors.f16.npy \
    --out era-calibration.json            # --write-bias re-fits and stores the biases
```

Measured 2026-11-22 on PD12M rev `867988b` with prompt set sha256 `838913006bde…` (`era.PROMPTS_SHA256`). The run read the dated rows memory-mapped, 8,192 at a time, in 10 s with a 5.4 GB peak RSS.

## Method

- **Space.** PD12M published OpenAI CLIP ViT-L/14 image embeddings. The 17 label vectors (kind × period, each the normalised mean of 2–5 prompts) come from the same model's text tower (`openai/clip-vit-large-patch14`, rev `32bd642`, the tower `semantic=` queries use). They were encoded once, offline on CPU, and committed.
- **Score.** softmax(100 · cos + bias) over the 17 labels. The probability is then summed per period (`clip_era`, `clip_era_conf`) and per kind (`clip_kind`, `clip_kind_conf`).
- **Ground truth.** The first year a caption mentions (61,336 items), mapped to the six periods. The caption medium maps to the kind: photo is photo; painting, drawing and print are artwork; map and text are document. Items split by `hash(id)` into a fit half and a **held-out half (30,561)**. Every number below is from the held-out half.
- **Biases.** Per-label logit biases were fitted on the fit half, artworks and documents only, with every period weighted equally (the dated set is 75% pre-1946; the archive is not). **Photo labels keep a zero bias.** A photo caption's year is usually its *subject's* date, as the audit below shows, so fitting photo biases on it would drag photos early. A fit that included photos reached 0.648 balanced accuracy, but it cut iNaturalist's photo-and-digital rate from 99.6% to 93.3%, so it was rejected.

## Per era (held out, all kinds)

Balanced accuracy is **0.616** (zero-shot without biases: 0.559). Plain accuracy is 0.613, 85.7% land within one bucket, and the kind is right for 90.2%.

| True era (caption year) | n | Recall | Precision |
|---|---|---|---|
| pre1800 | 4,248 | 0.621 | 0.602 |
| 1800s | 15,857 | 0.615 | 0.782 |
| 1900-1945 | 7,050 | 0.589 | 0.610 |
| 1946-1979 | 1,224 | 0.429 | 0.433 |
| 1980-1999 | 391 | 0.652 | 0.136 |
| 2000-now | 1,791 | 0.788 | 0.369 |

Raising `clip_era_min_conf` trades coverage for accuracy:

| min conf | 0 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 |
|---|---|---|---|---|---|---|
| kept | 100% | 89.6% | 76.4% | 61.7% | 48.2% | 34.9% |
| accuracy | 0.613 | 0.644 | 0.681 | 0.720 | 0.754 | 0.792 |

The low precision of `1980-1999` and `2000-now` reflects the labels more than the model. In the caption-dated set, most items whose caption names an old year are modern photographs of old things (next section).

## The film / digital boundary (photos)

Held-out items whose caption medium is photo, by true era (caption year) and predicted era:

| True ↓ / predicted → | ≤1945 | 1946-1979 | 1980-1999 | 2000-now |
|---|---|---|---|---|
| ≤1945 (2,229) | 2,139 | 45 | 11 | 34 |
| 1946-1979 (123) | 48 | 67 | 7 | 1 |
| 1980-1999 (17) | 2 | 3 | 12 | 0 |
| 2000-now (22) | 7 | 1 | 2 | 11 |

Caption-dated photos near the boundary are too few to judge it (17 and 22 items), and the 34 "≤1945 → 2000-now" cells are mostly not errors. A hand audit read 100 random captions where `photo, 2000-now, conf ≥ 0.5` disagreed with a pre-2000 caption year, drawn from 6,000 random dated items. About 98 described a modern photograph of an older dated subject: a 1929 Ford at a car show, a plaque reading "Built AD 1874", an 1878 coin beside a ruler, a museum specimen label "Lea, 1857", a house number "1515". One or two were ambiguous, and none was clearly an old photograph.

Collection-level references are the better test of the boundary. They hold 20,000 random items each (1,205 for colour, which is all there were):

| Reference | Read as photo | Photo and 2000-now | at conf ≥ 0.5 | at conf ≥ 0.7 |
|---|---|---|---|---|
| iNaturalist (founded 2008: digital) | 99.3% | **99.2%** | 99.1% | 97.2% |
| Historic-photo archives, b/w photos | 98.7% | **0.3%** | 0.1% | 0.0% |
| Historic-photo archives, colour photos | 92.8% | **0.3%** | 0.2% | 0.1% |

The historic-photo archives are Swedish Railway Museum, Deutsche Fotothek, Netherlands Institute for Military History, Estonian Sports and Olympic Museum, The Norwegian Museum of Cultural History, Bohuslän Museum, The Utrecht Archives and Regional Archive Nijmegen. Their photos are mostly digitised film and glass plates, and their colour photos (caption tone `color`) are the closest proxy here for scanned colour film. Within those archives, the colour photos spread as 33% `1946-1979`, 9% `1980-1999` and 1% `2000-now`. Some also land in `1800s` (28%), which is likely tinted or hand-coloured prints.

A 1M-row sample run (the first rows of the archive, scored from the NAS share in 40 s) gave 400,588 `photo` + `2000-now` items, and 346,761 at `clip_era_min_conf=0.7`. Of those, 50 captions say "vintage", "old photo" or "sepia". The 5,779 with caption tone `bw` turned out, on reading a sample, to be colour photos of black-and-white subjects (butterflies, a magpie, lichen).

## What this supports

- **Photo versus digital: use it.** `clip_kind=photo&clip_era=2000-now&clip_era_min_conf=0.7` keeps 97% of known-digital photos and admits about 0.1% of historic-archive photos. Scanned colour film was the residual risk; a hand-labelled boundary set checked against each file's EXIF device found 55 of 55 such photos digital at conf ≥ 0.7 ([film-digital/README.md](film-digital/README.md), #55). A person still accepts each image, as argus-curator #113 requires.
- **`clip_era=1980-1999` is not a film filter.** On the same set, every photo below conf 0.7 whose EXIF names a device is a digital capture, and at conf ≥ 0.7 it is about half film. Read it as "looks older".
- **Finer periods: a cue only.** At about 62% balanced accuracy (85% within one bucket), use `clip_era` to sort or to narrow a search, not as a date.
- **Not a capture date.** For a photograph of an artwork, the era is the artwork's, which is what the prompts describe. Unscored items never match a `clip_*` filter.

## Reproducing the full pass

`argus-quarry archives era pd12m` scores all 12.4M rows in chunks of 65,536 (about 200 MB each). It writes each chunk under `$QUARRY_HOME/cache/archives/.pd12m.era.parts/` as it goes, so an interrupted run resumes. When done, it publishes `pd12m.era.parquet` atomically and rebuilds the metadata index. From the NAS share, expect about 9 minutes plus a 1-minute reindex, with peak memory around 3.3 GB (the reindex). With a semantic build present, it reads the local float16 copy instead.
