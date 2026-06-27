# Project Status and Roadmap

Last updated: 2026-06-27

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
- reusable `ScanService` orchestration independent of argparse and PySide6;
- structured stage, item-progress, warning, completion, cancellation, and
  failure events;
- cooperative cancellation that retains already committed cache work;
- `ScanResult` with scanned records and basic review metadata;
- lazy read-only probing of container, codec, resolution, frame rate, audio and
  subtitle tracks, chapters, and attachments.

The program is report-only. It does not move, merge, rename, or delete files.
There is no desktop interface or transcoding command in version 1.5.2. Those
features are planned in `docs/ROADMAP.md`.

## Supported Behavior

- One input root: finds duplicates within that tree.
- Multiple input roots: finds duplicates within and across all trees and also
  calculates folder-pair similarity.
- Traversal is recursive through all nested subfolders.
- Default scanning covers common video, image, and audio formats.
- Python callers can run scans without terminal output through
  `dedupe.service.ScanService`, receive typed events, cancel through a
  thread-safe token, and request detailed metadata only for selected files.

## Verification Completed

The available suite currently contains 40 unit/integration tests.

- All 40 pass under both `unittest` and pytest in the project-local Python
  3.11.9 virtual environment with all runtime/dev dependencies installed.
- Service coverage verifies structured events, warnings, failure reporting,
  cooperative cancellation/cache preservation, and review metadata.
- Ruff passes with no findings.
- A real ffmpeg integration test passes with ffmpeg/ffprobe
  `2026-06-15-git-44d082edc8`. It generates a short source video, a resized
  recompressed AVI, an MKV with one second appended, and an unrelated control.
- A real audio integration test generates WAV sources and confirms matching
  across MP3, FLAC, and volume changes while rejecting an unrelated recording.
- Python compilation, JSON config parsing, cache migration, CLI startup, and
  `git diff --check` pass.
- Package builds produce both the `samesame-1.5.2` source archive and
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
- There is no desktop UI; the current interface is the `samesame` CLI and its
  HTML/JSON reports.
- Anime encoding presets are specified for future work, but no transcoding
  module or `samesame-transcode` command exists yet.

## Planned Product Work

The detailed implementation plan, safety rules, complexity estimates, and
acceptance criteria are in `docs/ROADMAP.md`. The planned order is:

1. Add a read-only PySide6 desktop interface for scans and side-by-side review.
2. Add safe quarantine/recycle actions with preflight checks and an operation
   journal. Permanent deletion is not part of the initial implementation.
3. Add a separate transcoding module and `samesame-transcode` command using the
   four presets in `docs/ANIME_ENCODING_PRESETS.md`.
4. Integrate a validated transcoding queue into the desktop interface.

Ongoing matching work remains evidence-driven:

- add representative labeled pairs, especially hard negatives and low-texture
  video variants;
- add video/audio candidate blocking only if real performance measurements
  justify it;
- improve alternate-cut hints without weakening content-backed matching;
- keep episode-in-compilation segment matching and crop/rotation-resistant
  image matching as separately scoped future features.

## Working Tree Handoff

Phase 0 is implemented for the `1.5.2` release: the CLI now delegates to the
reusable service, while the service remains independent of both terminal UI
and PySide6. A new session should begin with:

```powershell
git status --short
git diff --check
python -m unittest discover -s tests -v
```

Do not discard uncommitted `1.5.2` work. Continue with Phase 1 only after the
service tests and full FFmpeg integration suite remain green.

## Suggested Prompt for a New Chat

```text
Read README.md, docs/USAGE.md, docs/CONFIG.md, docs/STATUS.md,
docs/ROADMAP.md, and docs/ANIME_ENCODING_PRESETS.md.
Preserve the current uncommitted working-tree changes. Verify the test suite,
then continue with the read-only PySide6 desktop interface in Phase 1 of
docs/ROADMAP.md.
```
