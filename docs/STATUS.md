# Project Status and Roadmap

Last updated: 2026-06-24

This document is the handoff point for continuing SameSame development in a new
chat or work session. Read it together with the root `README.md`,
`docs/USAGE.md`, and `docs/CONFIG.md`.

## Current State

Implemented:

- recursive scanning of one or more input folder trees;
- exact duplicate detection for every explicitly included file extension using
  partial and full hashes;
- video similarity using five sampled ffmpeg frames and perceptual hashes;
- start/end-aligned video resampling for small duration differences;
- image similarity using perceptual structure plus average color;
- default image support for JPG/JPEG, PNG, WebP, BMP, GIF, TIFF;
- image matching across resize, JPEG recompression, and common format changes;
- separate cached video and image fingerprints in SQLite;
- transitive content clusters combining exact relationships with video or image
  similarity relationships;
- corrected folder Jaccard scoring that includes unmatched files in the union;
- HTML and JSON reports with exact, video, image, folder, and name sections;
- optional Anthropic, LM Studio, or local heuristic title normalization;
- threshold-safe candidate blocking for large video and image candidate sets;
- deduplication of resolved file paths discovered through overlapping roots;
- automatic migration of older cache databases with the new image column.

The program is report-only. It does not move, merge, rename, or delete files.

## Supported Behavior

- One input root: finds duplicates within that tree.
- Multiple input roots: finds duplicates within and across all trees and also
  calculates folder-pair similarity.
- Traversal is recursive through all nested subfolders.
- Default scanning covers common video and image formats.
- Audio can be explicitly included for exact byte-identical matching only.

## Verification Completed

The available suite currently contains 19 unit/integration tests.

- All 19 pass in the auxiliary local Python 3.10.6 environment that has Pillow
  installed. This interpreter is useful for verification but is below the
  project's supported Python 3.11+ requirement.
- Core tests pass on Python 3.11.9.
- Image tests are skipped in the current Python 3.11.9 installation because its
  environment does not yet have project dependencies installed.
- A real ffmpeg integration test passes with ffmpeg/ffprobe
  `2026-06-15-git-44d082edc8`. It generates a short source video, a resized
  recompressed AVI, an MKV with one second appended, and an unrelated control.
- Python compilation, JSON config parsing, cache migration, and
  `git diff --check` pass.
- A recursive CLI test confirms that a resized/recompressed JPEG in a nested
  folder matches its PNG source and appears in HTML/JSON and folder clusters.
- Candidate blocking was checked against generated passing pairs without
  observed false negatives.

## Environment Still Needed

Install the project in a Python 3.11+ virtual environment:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Confirm that `python --version` reports 3.11 or newer before creating the
environment.

`ffmpeg` and `ffprobe` are installed under `C:\ffmpeg\bin`. New terminals
should discover them through the machine PATH. Verify:

```powershell
ffmpeg -version
ffprobe -version
```

The current long-running Codex process may retain the old PATH, so verification
in this session uses the absolute executable paths.

## Important Known Limitations

- Audio has no perceptual fingerprinting. Different MP3/FLAC/WAV encodes of the
  same recording are not recognized as similar.
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

1. Install all dependencies in the Python 3.11 environment and run the full
   suite there.
2. Add perceptual audio fingerprinting and default audio extensions.
3. Build a representative media fixture corpus and tune image/video thresholds
   using measured false-positive and false-negative rates.
4. Improve image robustness for rotation/cropping only if real collections
   demonstrate the need.
5. Consider report UX improvements such as thumbnails, media metadata, cluster
   summaries, and explicit review decisions.

## Working Tree Handoff

At the time of this update, scanner path deduplication, duration-aligned video
matching, and their documentation/test updates are present as uncommitted
working-tree changes. The earlier fixes and image feature were already
committed. A new session should begin with:

```powershell
git status --short
git diff --check
python -m unittest discover -s tests -v
```

Do not discard the existing changes. Review and commit them as one coherent
scanner/video-integration change when ready.

## Suggested Prompt for a New Chat

```text
Read README.md, docs/USAGE.md, docs/CONFIG.md, and docs/STATUS.md.
Preserve the current uncommitted working-tree changes. Verify the test suite,
then continue from the "Recommended Next Work" section in docs/STATUS.md.
```
