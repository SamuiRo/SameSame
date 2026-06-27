# Project Status and Roadmap

Last updated: 2026-06-25

This document is the handoff point for continuing SameSame development in a new
chat or work session. Read it together with the root `README.md`,
`docs/USAGE.md`, and `docs/CONFIG.md`.

## Current State

Implemented:

- recursive scanning of one or more input folder trees;
- exact duplicate detection for every explicitly included file extension using
  partial and full hashes;
- video similarity using 15 sampled ffmpeg frames and perceptual hashes;
- start/end-aligned video resampling for small duration differences;
- versioned video fingerprints with monotonic sequence alignment;
- ratio/delta duration gating that permits ordinary alternate cuts while
  excluding single episodes from multi-episode compilations;
- image similarity using perceptual structure plus average color;
- default image support for JPG/JPEG, PNG, WebP, BMP, GIF, TIFF;
- image matching across resize, JPEG recompression, and common format changes;
- audio similarity using ffmpeg's Chromaprint muxer;
- default audio support for MP3, FLAC, WAV, M4A, AAC, Ogg, Opus, WMA, AIFF;
- audio matching across codec, bitrate, container, and moderate volume changes;
- separate cached video, image, and audio fingerprints in SQLite;
- transitive content clusters combining exact relationships with video, image, or audio
  similarity relationships;
- corrected folder Jaccard scoring that includes unmatched files in the union;
- HTML and JSON reports with exact, video, image, audio, folder, and name sections;
- optional Anthropic, LM Studio, or local heuristic title normalization;
- parent-folder context for generic names such as `01 - Episode 1`, preventing
  same-number episodes from unrelated seasons becoming identical name keys;
- threshold-safe candidate blocking for large image candidate sets;
- reproducible synthetic/manifest threshold benchmark with FP/FN metrics;
- measured audio default threshold of 94% based on the baseline corpus;
- deduplication of resolved file paths discovered through overlapping roots;
- automatic migration of older cache databases with image/audio columns.

The program is report-only. It does not move, merge, rename, or delete files.

## Supported Behavior

- One input root: finds duplicates within that tree.
- Multiple input roots: finds duplicates within and across all trees and also
  calculates folder-pair similarity.
- Traversal is recursive through all nested subfolders.
- Default scanning covers common video, image, and audio formats.

## Verification Completed

The available suite currently contains 33 unit/integration tests.

- All 33 pass under both `unittest` and pytest in the project-local Python
  3.11.9 virtual environment with all runtime/dev dependencies installed.
- Ruff passes with no findings.
- A real ffmpeg integration test passes with ffmpeg/ffprobe
  `2026-06-15-git-44d082edc8`. It generates a short source video, a resized
  recompressed AVI, an MKV with one second appended, and an unrelated control.
- A real audio integration test generates WAV sources and confirms matching
  across MP3, FLAC, and volume changes while rejecting an unrelated recording.
- Python compilation, JSON config parsing, cache migration, CLI startup, and
  `git diff --check` pass.
- Isolated package builds produce both the `samesame-1.5.0` source archive and
  universal wheel.
- A recursive CLI test confirms that a resized/recompressed JPEG in a nested
  folder matches its PNG source and appears in HTML/JSON and folder clusters.
- Candidate blocking was checked against generated passing pairs without
  observed false negatives.
- The 27-pair synthetic threshold corpus is recorded in
  `docs/threshold-baseline.json`: image 90% and audio 94% produce zero measured
  errors; video 85% has two synthetic low-texture false negatives and no false
  positives.
- The local 25-pair real video manifest measures 5/15 content-backed positives
  and 0/10 false positives at 85%; weaker same-title variants remain review
  hints, and the compilation pair is duration-gated.
- A full 59-file real collection scan completes in about 20 seconds on the
  warmed cache, yielding 9 video matches and 7 contextual name hints without
  merging the three similarly named `Aku no Onna Kanbu` seasons or the
  compilation pair.

## Development Environment

The project-local `.venv` uses Python 3.11.9 and has the editable project plus
all `dev` dependencies installed. Activate it with:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

`ffmpeg` and `ffprobe` are installed under `C:\ffmpeg\bin`. New terminals
should discover them through the machine PATH. Verify:

```powershell
ffmpeg -version
ffprobe -version
```

The current long-running Codex process may retain the old PATH, so verification
in this session uses the absolute executable paths.

## Important Known Limitations

- Audio matching is not designed for substantial remixes, speed/pitch changes,
  or long inserted/removed sections.
- Video containment matching (for example, finding one episode inside a
  multi-episode compilation) is not implemented. Such pairs may still appear
  as name hints and are not treated as content duplicates.
- Image matching is not designed yet for heavy cropping, arbitrary rotation,
  large overlays/watermarks, or major edits.
- Animated GIF and multi-page TIFF matching currently uses the first decoded
  frame/page.
- With overlapping roots, each resolved path is assigned to the first supplied
  root through which it is discovered.
- Name-only matches remain hints and are not deletion evidence.
- There is no automatic deletion or duplicate-resolution workflow.

## Recommended Next Work

Suggested order:

1. Add labeled pairs from representative real collections using
   `docs/threshold-manifest.example.json`, especially hard video negatives and
   low-texture video variants.
2. Add threshold-safe candidate blocking if large audio duration buckets become
   a measured performance problem.
3. Add threshold-safe candidate blocking for sequence-aligned video
   fingerprints if large duration buckets become a measured performance issue.
4. Continue improving low-confidence alternate-cut review hints without
   weakening content-backed matching.
5. Consider optional video segment/containment matching for single episodes
   inside compilations as a future, separately enabled feature.
6. Improve image robustness for rotation/cropping only if real collections
   demonstrate the need.
7. Consider report UX improvements such as thumbnails, media metadata, cluster
   summaries, and explicit review decisions.

## Working Tree Handoff

Threshold benchmarking was committed in `da9d33b`. At the time of this update,
versioned sequence-aligned video matching, contextual name hints, version 1.5.0
metadata, documentation, and tests are uncommitted. A new session should begin
with:

```powershell
git status --short
git diff --check
python -m unittest discover -s tests -v
```

Do not discard the existing changes. Review and commit them as one coherent
video-alignment and contextual-name change when ready.

## Suggested Prompt for a New Chat

```text
Read README.md, docs/USAGE.md, docs/CONFIG.md, and docs/STATUS.md.
Preserve the current uncommitted working-tree changes. Verify the test suite,
then continue from the "Recommended Next Work" section in docs/STATUS.md.
```
