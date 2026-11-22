# Film or digital: a hand-labelled set at the clip_era boundary (#55)

[docs/era-calibration.md](../era-calibration.md) measures the film/digital boundary with collection references: iNaturalist as digital, historic-photo archives as film. Scanned colour film from the 1980s and 90s is the case argus-curator #113 worries about most, and it has no sizeable ground truth there: the caption-dated set holds only 17 photos from 1980–1999 and 22 from 2000 on. This set adds that ground truth, labelled by a person.

## The sample (`sample.json`)

`scripts/film_digital_set.py sample` drew 298 colour photos from the first 3,000,000 PD12M rows, scored with the pinned prompt set (`prompts_sha256` in the file). It took photos CLIP reads as `clip_kind=photo` with `clip_era` 1980-1999 or 2000-now, stratified by era and confidence band:

| clip_era | conf < 0.5 | 0.5–0.7 | ≥ 0.7 |
|---|---|---|---|
| 2000-now | 58 (of 40,285) | 60 (of 104,450) | 60 (of 436,777) |
| 1980-1999 | 40 (of 14,355) | 40 (of 19,404) | 40 (of 20,361) |

- **Stratified, then reweighted.** 75% of the archive's 2000-now photos (in this pool) sit at conf ≥ 0.7. A plain random draw would say almost nothing about the low-confidence bands where film scans would hide. Every rate the report gives is reweighted by each band's real size, so it describes the archive, not the sample.
- **No iNaturalist.** Its photos are digital by construction and already serve as the known-digital reference, so including them would flatter precision. 257 of the 298 come from Wikimedia Commons.
- **Colour only.** The caption's tone must not be `bw`, and each drawn image must have a mean HSV saturation of at least 0.10. 48 candidates failed that check (greyscale or sepia scans) and the next candidate was drawn instead. One stratum ended 2 short.

## Labelling

The labelling page (built by `film_digital_set.py page`) shows one photo at a time on an untinted grey surround, at up to 1280 px with a full-size view. It is **blind**: the page carries only ids and image paths, in a fixed shuffled order, and never shows CLIP's era or confidence. The labels are:

- `digital`: taken on a digital camera.
- `film`: a scan of a negative, slide or print.
- `unsure`: can't tell. It is reported separately and never counted either way.

The question is about the capture, not the subject: a modern photo of an old car is digital.

## Reproducing the report

```bash
# the page's stored labels (read_db with out_dir) -> labels.json, dropping anything outside the set
uv run --no-sync python scripts/film_digital_set.py labels \
    --sample docs/film-digital/sample.json --export <read_db out_dir> --out docs/film-digital/labels.json
uv run --no-sync python scripts/film_digital_set.py report \
    --sample docs/film-digital/sample.json --labels docs/film-digital/labels.json
```

`report` gives, for `clip_kind=photo&clip_era=2000-now` at `clip_era_min_conf` 0, 0.5 and 0.7, the reweighted share of decided labels that are digital (its precision) and the share that are film. It also gives the share of `1980-1999` photos that are actually digital.

## Results

Labelling is in progress. The results, and whether boundary-specific prompts ("a scanned 35mm film photo", "film grain", "a digital camera photo") are needed, follow once every photo has a label.
