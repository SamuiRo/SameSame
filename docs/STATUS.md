# Project Status and Roadmap

Last updated: 2026-08-03

This document is the handoff point for continuing SameSame development in a new
chat or work session. Read it together with the root `README.md`,
`docs/USAGE.md`, and `docs/CONFIG.md`.

The current package version is `1.9.4`. Stages A through E, and Stage F
except its corpus-dependent items, are implemented — see
`docs/AUDIT-1.7.0.md` for full findings, fixes, and regression coverage.

**Start here if you are picking this up fresh: `docs/AUDIT-1.8.0.md`.** That
audit measured the 1.8.0 matcher against the real 276-video test library
(`S:\Daedalus\Kiroku\TestLib`) and found that a full scan would have taken
**~39 hours**, because F-25's lowered alignment floor and F-27's five
offsets together made the expensive alignment recheck fire on essentially
every duration-compatible pair, uncached. 1.9.0 fixes that (A-01) by
sampling each video once on an absolute-time grid, caching it, and aligning
by integer shifts over cached hashes:

1.9.1 then fixed a defect in that fix (A-13): the grid cadence was chosen to
bound the sample count, which made it 32 seconds for a typical episode — too
coarse to resolve the few seconds of offset that separate two releases of
the same episode. It is now a fixed 2 seconds, built in one decode pass.

| | 1.8.0 | 1.9.0 | 1.9.1 |
| --- | ---: | ---: | ---: |
| Alignment cost per pair | 10.3-12.3 s | 1.3 ms | 4.5 ms |
| Full library scan, warm cache | ~39 hours | 32 s | **105 s** |
| Video matches at the default | 82 (many false) | 105 | **129** |
| Lowest labelled true positive | — | 15.25 | **34.00** |

`video_threshold` moved `15.0` → `20.0` as part of the same work, this time
derived from all 22 849 compatible pairs of the real library rather than
from 76 curated pairs (A-02). The 15-19 band was dominated by cross-title
false positives, including different episodes of the same show. After A-13
the two classes separate with nothing at all between `18.72` and `28.44`,
so `20.0` sits inside an empty gap rather than on a knife edge.

From the earlier `1.7.0` audit: similar-match results are grouped into
transitive clusters instead of pairs (F-23), scans and folder consolidation
batches actually stop when cancelled (F-02, F-12), `anthropic` is an optional
`ai` extra (F-19), folder comparison runs per-directory (F-16), the desktop
GUI remembers scan settings via `QSettings` (F-18), and the HTML report is
English-only with a search/filter box (F-20).

A real, user-confirmed labelled corpus exists at
`docs/threshold-real-manifest.json` (76 video/image pairs across roughly 20
titles from a local test library the user built specifically for this work,
local/gitignored; titles are anonymized as "Title A", "Title B", etc. in
`docs/AUDIT-1.7.0.md` and `docs/THRESHOLDS.md`; treat this as
the stable baseline — the user has indicated they've likely exhausted what
they can readily label). F-27 (alignment-recheck offset sweep) and **F-24
(similarity rescale) are both now done** — see `docs/AUDIT-1.7.0.md`'s
2026-07-31 and four 2026-08-01 updates for the full before/after numbers.

**F-24 changed what threshold numbers mean.** Video/image/audio similarity
is now a *calibrated* confidence (`0` = indistinguishable from unrelated
content, `100` = identical) instead of a raw Hamming-based percentage with a
~50% noise floor — see `docs/THRESHOLDS.md` for the full explanation and
migration notes. **All three defaults are now measured rather than carried
over**, and in two of the three cases the measurement changed the scoring
formula, not just the number:

| | pre-F-24 | F-24 | measured | derived in |
| --- | ---: | ---: | ---: | --- |
| `video_threshold` | `85` | `15` | **`20`** | 1.9.0-1.9.1 (A-02) |
| `image_threshold` | `90` | `80` | **`60`** | 1.9.2 (A-06) |
| `audio_threshold` | `94` | `88` | **`60`** | 1.9.3 (A-14) |

At `15.0` the 76-pair corpus reached 52/61 confirmed matches (85%) with 0
false positives *on that corpus* — but A-02 showed that result does not
transfer to a library scan, which compares three orders of magnitude more
unrelated pairs and does produce false positives at `15.0`.

F-30 was closed in 1.9.1, and its premise was wrong: the misses it named
were alignment failures, not a descriptor ceiling. Its reasoning ("this
episode has essentially no duration delta, yet still scores low, so
alignment is not the cause") assumed alignment means *duration*
compensation — but a same-duration pair can still be offset in time, and at
1.9.0's 32-second grid cadence a 6-second offset was invisible. See A-13.

## Current State

Implemented:

- recursive scanning of one or more input folder trees;
- exact duplicate detection for every explicitly included file extension using
  partial and full hashes;
- video similarity using 15 sampled ffmpeg frames and perceptual hashes;
- a second, absolute-time alignment grid per video (one frame every 2s, built
  in a single decode pass), cached in SQLite and versioned separately, so
  time-shifted duplicates align by integer offset over cached hashes instead
  of re-decoding a file per candidate offset per pair;
- alignment applied to every duration-compatible pair, with no score floor
  and no duration-delta gate, at about a millisecond per pair;
- versioned video fingerprints with monotonic sequence alignment;
- ratio/delta duration gating that permits ordinary alternate cuts while
  excluding single episodes from multi-episode compilations;
- image similarity using calibrated perceptual structure, with average
  colour acting only as a veto rather than as part of the score;
- default image support for JPG/JPEG, PNG, WebP, BMP, GIF, TIFF;
- image matching across resize, JPEG recompression, and common format changes;
- audio similarity using ffmpeg's Chromaprint muxer, aligned over ±30
  seconds of offset by probing every shift and scoring only the winner;
- default audio support for MP3, FLAC, WAV, M4A, AAC, Ogg, Opus, WMA, AIFF;
- audio matching across codec, bitrate, container, and moderate volume changes;
- separate cached video, image, and audio fingerprints in SQLite;
- transitive content clusters combining exact relationships with video, image, or audio
  similarity relationships, exposed as a single review case (with a
  per-pair evidence matrix) instead of one case per pairwise match, and used
  for both folder Jaccard scoring and keeper-based batch cleanup;
- corrected folder Jaccard scoring that includes unmatched files in the union;
- per-directory folder comparison (comparing each file's containing folder,
  not only the collection root), with the collection-root comparison kept as
  an additional roll-up when several roots are scanned, and a shared-cluster
  prefilter so unrelated directories are never generated as a candidate pair;
- HTML and JSON reports with exact, video, image, audio, mixed-evidence,
  folder, and name sections, grouped by cluster; the HTML report is
  English-only with a live search/filter box and per-section result counts;
- optional Anthropic, LM Studio, or local heuristic title normalization
  (Anthropic requires the `ai` extra: `pip install -e ".[ai]"`);
- parent-folder context for generic names such as `01 - Episode 1`, preventing
  same-number episodes from unrelated seasons becoming identical name keys;
- threshold-safe candidate blocking for large image candidate sets;
- degenerate-descriptor vetoes on both image and audio: two flat colours
  and two pure tones each produce near-identical descriptors, so pairs
  whose descriptors carry no information are refused rather than scored;
- reproducible synthetic/manifest threshold benchmark with FP/FN metrics;
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
- `QSettings`-backed persistence of collection roots, thresholds, worker
  count, ffmpeg/ffprobe paths, quarantine path, skip flags, window geometry,
  and splitter sizes across launches; the unsafe-recycle opt-in is
  deliberately excluded and always starts unchecked;
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

- One input root: finds duplicates within that tree, including folder-pair
  similarity between subfolders under it.
- Multiple input roots: finds duplicates within and across all trees, and
  additionally calculates a coarser folder-pair similarity between the roots
  themselves.
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
  universal wheel (current package version: `1.9.0`).
- A recursive CLI test confirms that a resized/recompressed JPEG in a nested
  folder matches its PNG source and appears in HTML/JSON and folder clusters.
- Candidate blocking was checked against generated passing pairs without
  observed false negatives.
- The 27-pair synthetic threshold corpus in `docs/threshold-baseline.json`
  and the 25-pair real video manifest predate the 1.9.x work: their recorded
  numbers are on the pre-F-24 raw scale and against matchers that have since
  changed in both formula and cost. Treat them as history, not as a current
  baseline — the live evidence is the real-library measurement below and the
  per-medium derivations in `docs/AUDIT-1.8.0.md` (A-02, A-06, A-14).
- A full 276-video / 97.5 GB real library scan (`S:\Daedalus\Kiroku\TestLib`)
  yields 129 video matches, 14 exact groups, and 61 folder pairs at the
  `20.0` default. On 4 workers the one-time alignment-grid build takes
  **26 minutes** (it reads every file once, so it is disk-bandwidth-bound)
  and a warm-cache rescan takes **105 seconds**. The pre-1.9.0 matcher would
  have needed ~39 hours for the same scan (`docs/AUDIT-1.8.0.md`, A-01).
  This supersedes the earlier "59-file collection in about 20 seconds" note,
  which predated the alignment work entirely.

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

- **F-30 is closed, and its premise was wrong.** Both of its named misses
  turned out to be alignment failures, not descriptor ceilings: at 1.9.0's
  32-second grid cadence a mere 6-second offset between two releases was
  invisible. At 1.9.1's 2-second cadence the same pairs score `66.30` and
  `75.42` instead of `19.08` and `15.25`, with no movement at all in any
  confirmed false positive (A-13). If new misses appear, re-check alignment
  before assuming the descriptor is at fault;
- all three similarity thresholds are now measured rather than carried over
  (A-02 video, A-06 images, A-14 audio), and in two of the three cases the
  measurement changed the formula, not just the number;
- audio matching is for finding duplicates among standalone audio files
  (mp3/wav/flac and friends) and deliberately does not work alongside video;
  a video-only library correctly exercises none of it. This is settled
  scope, confirmed with the user — not a gap to close;
- improve alternate-cut hints without weakening content-backed matching;
- keep episode-in-compilation segment matching and crop/rotation-resistant
  image matching as separately scoped future features.

## Working Tree Handoff

Phases 0 through 5 are implemented, and the current package version is `1.9.4`. The scanner CLI
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

The closed `1.6.6` safety review is recorded in `docs/AUDIT.md`. The `1.7.0`
audit — findings, fixes, regression coverage, and residual/open items — is in
`docs/AUDIT-1.7.0.md`. **The current audit is `docs/AUDIT-1.8.0.md`**: it
measured 1.8.0 against the real library. Everything in it is now fixed
— A-01 through A-05 and A-07/A-08/A-09/A-11 in 1.9.0, A-13 in 1.9.1, A-06
in 1.9.2, A-14 in 1.9.3, A-12 in 1.9.4. **Every finding in that audit is
closed**, and its closing section records a full-library validation at the
shipped defaults.

A lesson worth carrying forward from that audit: every Critical finding in
it existed because detection changes had been validated on curated pairs and
a fast synthetic suite, never on a full library run. Both matter, and only
the second one catches cost and base-rate problems.

## Suggested Prompt for a New Chat

```text
Read README.md, docs/USAGE.md, docs/CONFIG.md, docs/STATUS.md,
docs/AUDIT-1.8.0.md, docs/AUDIT-1.7.0.md, docs/ROADMAP.md, and
docs/ANIME_ENCODING_PRESETS.md. Inspect and preserve any existing
working-tree changes. Verify the test suite, then choose one separately
scoped deferred item from docs/AUDIT-1.8.0.md (A-12),
docs/AUDIT-1.7.0.md (F-30), or docs/ROADMAP.md, while keeping the existing
source-protection rules intact. Any change to detection quality or matching
cost must be validated with a full scan of the real library, not only with
the test suite.
```
