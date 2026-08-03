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
comparisons: `find_video_matches` decalibrates the incoming threshold once
and does all of its comparisons on the raw scale, calibrating only the final
score. See `calibrate_similarity`'s docstring for the full reasoning.

One caveat on the image path (A-06): calibration is applied to the structure
term *before* it is blended with the colour term, so `image_threshold=80` is
**not** equivalent to the old `90`. In old-scale terms the condition changed
from `0.8·S + 0.2·C >= 90` to `0.8·S + 0.1·C >= 80` - the colour term now
carries half the weight relative to the bar. The number is unchanged pending
real image evidence, but it should be re-derived rather than assumed to
preserve the old behaviour.

No fingerprint version bump was needed for this change: the stored
fingerprint bytes (the phash/Chromaprint values themselves) are unaffected;
only the comparison math changed, and comparisons are computed fresh, never
cached.

## How video alignment works (A-01)

Two fingerprints are stored per video, for different jobs:

| | Sampling | Size | Used for |
| --- | --- | ---: | --- |
| `fingerprint` | 15 frames at fixed *fractions* of the duration | 15 hashes | the cheap first-pass score |
| `grid_fingerprint` | one frame every **2 seconds** of *absolute* time, one decode pass | ~850 hashes for a 28-min episode | alignment |

Because grid samples sit at absolute offsets, the same moment of the same
content lands on the same index in both files, so aligning two videos is an
integer shift over cached hashes - no decoding at pair time.

The cadence has to be fine relative to a *shot*, not merely to the file
(A-13). An integer-slot shift can only cancel offsets that are multiples of
the cadence, and a few seconds of extra logo or intro is the commonest
difference between two releases of one episode - a real pair in the test
library sits exactly 6s apart. An earlier 32-second cadence could not see
that at all and scored the pair as noise.

A 2-second cadence is affordable because the grid is built in **one decode
pass** (`fps=1/2,scale=32:32`), not one seek per sample: 851 samples in 7.9s
against 8.5s for the 53 seeks a 32-second cadence needed. This does not
contradict the 1.7.0 audit's withdrawn "single pass" idea - that was
measured for 15 samples, where per-sample seeks genuinely win. The trade
inverts once you want hundreds.

Alignment therefore runs for **every** duration-compatible pair, with no
score floor and no duration-delta gate. It is the matcher's strongest
discriminator: on the real library, true duplicates gain up to +76 points
from it while confirmed unrelated pairs gain zero.

Measured on the 276-video / 97.5 GB library:

| | 1.8.0 | 1.9.0 (32s grid) | 1.9.1 (2s grid) |
| --- | ---: | ---: | ---: |
| Alignment cost per pair | 10.3-12.3 s | 1.3 ms | 4.5 ms |
| Grid build, all 276 files, 4 workers | — | 15 min | 26 min |
| Full scan, warm cache | ~39 hours (nothing cached) | 32 s | **105 s** |
| Video matches at the default threshold | 82 (many false) | 105 | **129** |

1.9.1 trades a slower one-time grid build - it reads every file once, so it
is disk-bandwidth-bound rather than CPU-bound - for detection that actually
works on time-shifted releases. The build is cached and versioned, so it is
paid once per file, ever.

## Current defaults and how they were chosen

| Media | Default (calibrated) | How it was derived |
| --- | ---: | --- |
| `video_threshold` | `20.0` | **Evidence-derived from a full library scan** (A-02): all 22 849 duration-compatible pairs of a real 276-video library, with alignment on. The earlier `15.0` came from a 76-pair curated corpus and does not survive library scale - a real scan compares three orders of magnitude more unrelated pairs, and the 15-19 band is dominated by cross-title false positives. After A-13 fixed alignment the two classes separate cleanly with **nothing at all between 18.72 and 28.44**: every pair above that gap is a genuine same-title duplicate, every pair below it is cross-title. `20.0` sits inside the empty gap, so it has real margin on both sides rather than being a knife edge. |
| `image_threshold` | `80.0` | `calibrate_similarity(90)`, kept unchanged - but see the caveat above: this is *not* equivalent to the pre-F-24 `90`, and no comparable real image evidence exists yet to re-derive it. The real corpus has only one confirmed image pair (a poster duplicate, scoring far above this bar). |
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
