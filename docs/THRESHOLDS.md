# Threshold Benchmarking

## The similarity scale (F-24)

`video_threshold`, `image_threshold`, and `audio_threshold` are all
**calibrated confidence percentages**: `0` means "indistinguishable from
unrelated content" and `100` means identical. This is not the same thing as
"percent similar" in a naive sense.

The underlying fingerprints are compared with a Hamming distance, and two
completely unrelated perceptual hashes of any bit width differ in roughly
half their bits by chance - so a *raw* Hamming-based similarity has a noise
floor around 50%, not 0%. Measured directly (300 random 64-bit hash pairs):
mean 52.3%, range 49.6-55.5%. Before F-24, threshold values were expressed on
that raw scale, so "85%" looked like "very similar" but was actually most of
the way from "identical" (100%) to "coin flip" (~50%) - and a real anime
corpus confirmed genuine duplicates routinely score in the low-to-mid 50s on
that raw scale, both before and after alignment.

`calibrate_similarity()` (`dedupe/video_fingerprint.py`) rescales the raw
score linearly so `50` raw maps to `0` and `100` raw stays `100`:

```
calibrated = max(0, (raw - 50) / 50 * 100)
```

This is applied to the video, image (structure component only - the color
component is a direct RGB distance with no noise floor and is left alone),
and audio similarity scores wherever they are reported or compared against a
user-supplied threshold. It is *not* applied to internal raw-scale
comparisons that need to tell a genuinely degenerate score (e.g. exactly
`0.0`, common in mocked tests) apart from a merely muddy real one (raw
51-56 is common for confirmed real duplicates) - both collapse to the same
clamped `0` after calibration, so `find_video_matches`'s
`ALIGNMENT_RECHECK_FLOOR` circuit-breaker stays on the raw scale internally.
See `calibrate_similarity`'s docstring for the full reasoning.

No fingerprint version bump was needed for this change: the stored
fingerprint bytes (the phash/Chromaprint values themselves) are unaffected;
only the comparison math changed, and comparisons are computed fresh, never
cached.

## Current defaults and how they were chosen

| Media | Default (calibrated) | How it was derived |
| --- | ---: | --- |
| `video_threshold` | `15.0` | **Evidence-derived** from a 76-pair real, user-confirmed corpus (`docs/threshold-real-manifest.json`, local/gitignored - see `docs/AUDIT-1.7.0.md`'s 2026-08-01 updates). The corpus's own best-fit threshold is `13.86` (52/61 = 85% recall, 0 false positives across all 15 confirmed negatives); `15.0` adds a small safety margin above the worst confirmed negative (`12.51` calibrated) at the cost of 2 fewer true positives (50/61 = 82% recall). Raise it toward `13.9` for maximum recall on content similar to the corpus, or lower it further only with new evidence that the negative ceiling is actually higher than `12.51`. |
| `image_threshold` | `80.0` | `calibrate_similarity(90)` - preserves the exact pre-F-24 practical strictness. The real corpus has only one confirmed image pair so far (a poster duplicate, scoring far above this bar); there isn't yet comparable evidence to justify moving this bar the way `video_threshold` was moved. |
| `audio_threshold` | `88.0` | `calibrate_similarity(94)` - same reasoning as `image_threshold`: preserved, not re-derived, pending real audio corpus evidence. |

`folder_threshold` and `name_threshold` are **not** affected by F-24: they
are Jaccard/fuzzy-string scores, not Hamming-based, and have no noise floor.

## Running the benchmark

```powershell
samesame-benchmark `
  --ffmpeg C:\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\ffmpeg\bin\ffprobe.exe `
  --output docs/threshold-baseline.json
```

The synthetic corpus contains labeled positive and negative pairs for images,
videos, and audio. It covers resize/recompression, brightness and volume
changes, container/codec changes, small duration changes, and unrelated
controls. All similarity values in its output are on the calibrated scale.

The evaluator reports:

- minimum positive and maximum negative similarity (calibrated), plus the raw
  values alongside them for diagnosis;
- false positives and false negatives at the configured threshold;
- a recommended threshold where a false positive costs five times as much as a
  false negative.

## Evaluating Real Media

Pass a JSON manifest instead of generating synthetic fixtures:

```powershell
samesame-benchmark `
  --manifest docs\threshold-manifest.example.json `
  --ffmpeg C:\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\ffmpeg\bin\ffprobe.exe `
  --video-threshold 15.0 --image-threshold 80.0 --audio-threshold 88.0 `
  --output threshold-real.json
```

Manifest paths are relative to the manifest file. Add both known matches and
hard negatives from the same collection. Do not treat the synthetic baseline
as proof that a threshold is universally safe - the synthetic corpus is
clean, low-noise fixture content; real anime/video content has measurably
different (muddier) separation, which is exactly what motivated F-24 and the
real corpus in the first place.

## What the real corpus showed (2026-08-01)

Summarized from `docs/AUDIT-1.7.0.md`'s three 2026-08-01 updates; see there
for the full detail and pair-by-pair breakdown in
`docs/threshold-real-results.json`.

- 76 pairs (61 positive, 15 negative) across roughly 20 real titles, each
  pair individually confirmed by the library's owner - not inferred from
  filenames.
- At the evidence-derived threshold, recall held stable at 85% (52/61) as
  the corpus tripled in size from 31 to 76 pairs, with **zero false
  positives** throughout - evidence this is a real property of the matcher,
  not sampling luck.
- The remaining false negatives concentrate in a small, named set of
  titles/episodes rather than spreading evenly across the corpus - a
  **descriptor** limitation (tracked as F-30, not yet implemented), not
  primarily an alignment one. One case in particular
  (a specific episode of one otherwise well-behaved title, called out as
  "Title P" in `docs/AUDIT-1.7.0.md`) has an almost-zero duration delta (1.1s),
  so F-27's alignment recheck has nothing to search, yet the raw score is
  still ~55% - ruling out alignment as the cause for that pair specifically.
- Two confirmed real matches were excluded by the duration ratio/delta gate
  (F-29) rather than scored at all: one plausibly because the longer file is
  a multi-episode concatenation (the gate working as intended), the other a
  near-miss just over the ratio limit (a case for reporting gated pairs
  separately instead of silently dropping them, per F-29's original
  write-up).

## Prior synthetic-corpus baseline decision (pre-F-24, raw scale)

Recorded for history; the raw-scale numbers below no longer correspond to
the current default thresholds after F-24's rescale.

The baseline generated on 2026-06-25 produced:

| Media | Old raw threshold | False positives | False negatives | Decision |
| --- | ---: | ---: | ---: | --- |
| Images | 90 | 0 | 0 | Keep 90 (raw). |
| Videos | 85 | 0 | 2 | Use 85 (raw) with the versioned sequence fingerprint. |
| Audio | 94 | 0 | 0 | Keep 94 (raw). |

The video misses came from a synthetic `rgbtestsrc` pattern whose low-texture
color bars are unusually sensitive to resize/re-encode changes - the same
low-texture sensitivity later confirmed on real content and tracked as F-30.
