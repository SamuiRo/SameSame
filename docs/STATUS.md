# Project Status and Roadmap

Last updated: 2026-07-31

This document is the handoff point for continuing SameSame development in a new
chat or work session. Read it together with the root `README.md`,
`docs/USAGE.md`, and `docs/CONFIG.md`.

The current package version is `1.7.5`. Stages A through E of the `1.7.0`
forward-looking audit are implemented — see `docs/AUDIT-1.7.0.md` for the
full findings, fixes, and regression coverage. Notably: similar-match results
are now grouped into transitive clusters instead of pairs (F-23), scans and
folder consolidation batches actually stop when cancelled instead of
finishing a queued backlog (F-02, F-12), and `anthropic` is an optional `ai`
extra rather than a hard dependency (F-19). Stage C's detection-quality items
that need a real labelled corpus (F-24 similarity rescaling, F-27 sampling
grid, F-30 descriptor work) remain open — no such corpus exists yet.

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
  similarity relationships, exposed as a single review case (with a
  per-pair evidence matrix) instead of one case per pairwise match, and used
  for both folder Jaccard scoring and keeper-based batch cleanup;
- corrected folder Jaccard scoring that includes unmatched files in the union;
- HTML and JSON reports with exact, video, image, audio, mixed-evidence,
  folder, and name sections, grouped by cluster;
- optional Anthropic, LM Studio, or local heuristic title normalization
  (Anthropic requires the `ai` extra: `pip install -e ".[ai]"`);
- parent-folder context for generic names such as `01 - Episode 1`, preventing
  same-number episodes from unrelated seasons becoming identical name keys;
- threshold-safe candidate blocking for large image candidate sets;
- reproducible synthetic/manifest threshold benchmark with FP/FN metrics;
- measured audio default threshold of 94% based on the baseline corpus;
- deduplication of resolved file paths discovered through overlapping roots;
- automatic migration of older cache databases with image/audio columns.
- reusable `ScanService` orchestration independent of argparse and PySide6;
- structured stage, item-progress, warning, completion, cancellation, and
  failure events, throttled to at most ~10 progress events/second so a large
  library does not flood the Qt event loop with one signal per file;
- cooperative cancellation of every fingerprinting stage that drops the
  queued backlog instead of waiting for it to finish (`cancel_futures=True`),
  and retains already committed cache work;
- `ScanResult` with scanned records and basic review metadata;
- lazy read-only probing of container, codec, resolution, frame rate, audio and
  subtitle tracks, chapters, and attachments.
- optional PySide6 desktop shell exposed through `samesame-gui`;
- multiple-root selection and scan settings with a background worker;
- responsive progress, warning logs, and cooperative cancel control;
- filters for all seven report categories (including mixed-evidence clusters);
- side-by-side image/video/audio preview with synchronized video seeking;
- detailed per-file metadata, open-file/open-folder controls, and report export.
- keep and ignore review states persisted by stable cluster-level result-group IDs;
- SHA-256 preflight and immediate pre-action identity verification;
- reversible quarantine with collection-relative paths and collision handling;
- operating-system recycle-bin integration through optional `Send2Trash`;
- persistent SQLite operation journal (WAL mode, shared busy timeout) with
  requested/completed/failed states;
- journal-driven restore for completed quarantine operations;
- cluster-level batch quarantine (any content-backed category, not just
  exact groups) that preserves the selected keeper;
- background file actions and explicit confirmation flows;
- mutation controls restricted to content-backed evidence (exact, video,
  image, audio, or mixed).
- independent source-preserving transcoding backend and `samesame-transcode` CLI;
- four anime presets for libx265, AV1 NVENC, and HEVC NVENC;
- FFmpeg encoder registration and hardware-initialization capability checks;
- sequential queue with progress, cancellation, dry-run, JSON output, and logs;
- MKV stream/metadata preservation and post-encode duration, stream, chapter,
  and first-frame decode validation.
- desktop transcode queue launched from reviewed video results;
- asynchronous preset capability status, progress, cancel, retry, output, and
  diagnostic-log controls;
- before/after media metadata, size, and savings display;
- separately confirmed quarantine-first promotion with conflict checks,
  content verification, journal integration, and rollback attempt.
- dedicated folder-based video-compression tab with recursive metadata loading,
  per-file checkboxes, and extension/size/duration batch selection;
- request-scoped custom libx265, AV1 NVENC, and HEVC NVENC presets that reuse
  the existing capability checks, source-preserving queue, and output validation.
- cycle-safe, unlimited-depth folder traversal with linked-directory support,
  broader video extensions, and visible rows even when metadata probing fails;
- optional post-encode cleanup that defaults to reversible quarantine and acts
  only after output validation and source SHA-256 identity verification;
- duplicate-review batch cleanup checklist with quick select-all-except-current,
  quarantine/recycle actions, all-files warnings, per-file identity checks, and journaling.
- third desktop Folder consolidation tab with editable final title names,
  explicit source-folder to target-subfolder mappings, and exact per-file previews;
- conflict-safe consolidation that never overwrites existing paths, rechecks
  preview identity, SHA-256-verifies every move, rolls failed batches back,
  can be cancelled between files (closing the window requests it too), and
  supports SHA-256-verified undo of the latest completed or cancelled batch;
- automatic cleanup of empty source folders only after a fully successful
  consolidation batch.

The scanner CLI remains report-only. The desktop interface modifies a source only
after an explicit quarantine confirmation, or an unsafe recycle opt-in, and a successful identity
preflight. There is no permanent-delete action. Encoding always creates and
validates a new MKV and keeps the original; promotion is a later explicit,
quarantine-first action.

## Supported Behavior

- One input root: finds duplicates within that tree.
- Multiple input roots: finds duplicates within and across all trees and also
  calculates folder-pair similarity.
- Traversal is recursive through all nested subfolders.
- Default scanning covers common video, image, and audio formats.
- Python callers can run scans without terminal output through
  `dedupe.service.ScanService`, receive typed events, cancel through a
  thread-safe token, and request detailed metadata only for selected files.
- Desktop users can run the optional `samesame-gui` interface without changing
  the CLI-only dependency footprint.
- Quarantine is the reversible default. OS recycle is blocked unless the user
  explicitly enables the red unsafe option because Windows may delete permanently.
- Python callers can use `dedupe.transcode.TranscodeQueue`; CLI users can run
  `samesame-transcode` independently of scanning and the desktop interface.
- Desktop users can queue reviewed videos, inspect capability and validation
  results, and explicitly promote an output only after source quarantine.

## Verification Completed

The snapshot below is from the `1.7.0` release verification. For the fixes
made since then (Stages A-E of the forward-looking audit), see the
per-stage "Full suite: N passed" lines and regression-test names in
`docs/AUDIT-1.7.0.md` — as of the last Stage E pass the suite contains 120
tests plus 7 subtests, all passing, with `ruff check`/`ruff format --check`
clean and a documented 37-error `mypy` baseline.

- All 95 pass under both `unittest` and pytest in the project-local Python
  3.11.9 virtual environment with all runtime/dev dependencies installed.
- Service coverage verifies structured events, warnings, failure reporting,
  cooperative cancellation/cache preservation, review metadata, and stable
  GUI progress without constructing terminal progress locks in Qt workers.
- GUI coverage verifies all result-category adapters, offscreen window startup,
  and an end-to-end background scan through the Qt event loop.
- Action coverage verifies scan-to-preflight failures, pre-action changes,
  SHA-256 quarantine validation, collision allocation, journal persistence,
  safe recycle blocking/explicit delegation, Windows sharing-violation retry,
  exact-batch keeper equality, rollback after failed move verification, batch
  quarantine, and restore through a Qt worker.
- Consolidation coverage verifies folder/name suggestions, numeric titles,
  path validation, conflict previews, changed-file rejection, verified batch
  execution, rollback, journal persistence, and undo.
- Ruff passes with no findings.
- A real ffmpeg integration test passes with ffmpeg/ffprobe
  `2026-06-15-git-44d082edc8`. It generates a short source video, a resized
  recompressed AVI, an MKV with one second appended, and an unrelated control.
- A real audio integration test generates WAV sources and confirms matching
  across MP3, FLAC, and volume changes while rejecting an unrelated recording.
- A real x265 integration test preserves two audio tracks, a subtitle track,
  chapter metadata, an MKV attachment, and the non-ASCII-named source.
- An offscreen GUI integration test runs a real x265 queue through Qt signals
  and verifies that the source and validated output both remain available.
- Promotion tests cover successful quarantine-first replacement, preflight
  destination conflicts, and automatic source restore after a simulated move failure.
- Python compilation, JSON config parsing, cache migration, CLI startup, and
  `git diff --check` pass.
- Package builds produce both the `samesame-1.7.0` source archive and
  universal wheel (current package version: `1.7.5`).
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
- Permanent deletion is intentionally not implemented.
- Unsafe recycle operations depend on operating-system behavior and are not
  automatically restorable. They are blocked by default; use quarantine when rollback matters.
- Restore is available only while the quarantined file still matches its
  journaled identity and the original path remains free.
- Hardware presets require compatible NVIDIA hardware and drivers; capability
  checks report unavailable devices before a queue starts.
- Transcoding currently produces MKV only. Promotion of a non-MKV source uses
  the same stem with an `.mkv` extension and refuses an occupied destination.
- A promoted source remains in quarantine, but journal restore is intentionally
  unavailable while the promoted file occupies its original path.

## Planned Product Work

The original GUI/transcoding plan and its acceptance criteria are complete.
Deferred matching and advanced encoding ideas remain in `docs/ROADMAP.md`.

Ongoing matching work remains evidence-driven:

- add representative labeled pairs, especially hard negatives and low-texture
  video variants — a real labelled anime corpus is still needed before F-24
  (similarity scale recalibration), F-27 (absolute-time sampling grid), or
  F-30 (descriptor investigation) in `docs/AUDIT-1.7.0.md` can be attempted;
- improve alternate-cut hints without weakening content-backed matching;
- keep episode-in-compilation segment matching and crop/rotation-resistant
  image matching as separately scoped future features.

## Working Tree Handoff

Phases 0 through 5 are implemented, and the current package version is `1.7.5`. The scanner CLI
delegates to the reusable service, the optional PySide6 interface consumes the
same scan service, journaled actions, and independent transcode backend. A new
session should begin with:

```powershell
git status --short
git diff --check
python -m unittest discover -s tests -v
```

Check the working tree before continuing and preserve any user changes that are
present. Start deferred features only after the service, GUI/action, and full
FFmpeg/transcode integration suites remain green.

The closed `1.6.6` safety review is recorded in `docs/AUDIT.md`. The current
forward-looking `1.7.0` audit — findings, fixes, regression coverage, and
residual/open items (Stage C's corpus-dependent detection-quality work) — is
recorded in `docs/AUDIT-1.7.0.md`.

## Suggested Prompt for a New Chat

```text
Read README.md, docs/USAGE.md, docs/CONFIG.md, docs/STATUS.md,
docs/AUDIT-1.7.0.md, docs/ROADMAP.md, and docs/ANIME_ENCODING_PRESETS.md.
Inspect and preserve any existing working-tree changes. Verify the test suite,
then choose one separately scoped deferred item from docs/AUDIT-1.7.0.md
(Stage F, or Stage C once a real corpus exists) or docs/ROADMAP.md while
keeping the existing source-protection rules intact.
```
