# Threshold Benchmarking

SameSame includes a reproducible threshold benchmark:

```powershell
samesame-benchmark `
  --ffmpeg C:\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\ffmpeg\bin\ffprobe.exe `
  --output docs/threshold-baseline.json
```

The synthetic corpus contains labeled positive and negative pairs for images,
videos, and audio. It covers resize/recompression, brightness and volume
changes, container/codec changes, small duration changes, and unrelated
controls.

The evaluator reports:

- minimum positive and maximum negative similarity;
- false positives and false negatives at the configured threshold;
- a recommended threshold where a false positive costs five times as much as a
  false negative.

## Current Baseline Decision

The baseline generated on 2026-06-25 produced:

| Media | Current threshold | False positives | False negatives | Decision |
| --- | ---: | ---: | ---: | --- |
| Images | 90 | 0 | 0 | Keep 90. |
| Videos | 90 | 0 | 2 | Keep 90 until representative real videos justify a lower value. |
| Audio | 90 | 1 | 0 | Raise the default to 94. |

The video misses come from a synthetic `rgbtestsrc` pattern whose low-texture
color bars are unusually sensitive to resize/re-encode changes. The generated
corpus suggests 74.38 for zero errors, but that is too aggressive to apply to
real collections without a larger negative set.

The audio false positive was a pair of distinct but harmonically simple
synthetic signals scoring 93.75. A 94% default separates all current audio
positives and negatives.

## Evaluating Real Media

Pass a JSON manifest instead of generating synthetic fixtures:

```powershell
samesame-benchmark `
  --manifest docs/threshold-manifest.example.json `
  --ffmpeg C:\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\ffmpeg\bin\ffprobe.exe `
  --output threshold-real.json
```

Manifest paths are relative to the manifest file. Add both known matches and
hard negatives from the same collection. Do not treat the synthetic baseline
as proof that a threshold is universally safe.
