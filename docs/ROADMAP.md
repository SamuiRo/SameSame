# Development Roadmap

Last updated: 2026-06-27

This roadmap records the staged implementation of the scanner, journaled
desktop cleanup, and transcoding features. Phases 0 through 4 are complete in
SameSame `1.6.0`; current commands and workflows are documented in `README.md`
and `USAGE.md`.

## Goals

1. Add a simple desktop interface for configuring and running scans.
2. Make duplicate review practical with side-by-side media and metadata.
3. Add safe, explicit actions for keeping, quarantining, or recycling files.
4. Add anime-oriented video compression as an independent module.
5. Integrate compression into the desktop review workflow without coupling it
   to duplicate detection.

The UI should use ordinary native controls. Animations, themes, and decorative
effects are outside the initial scope.

## Complexity Scale

| Level | Meaning |
| --- | --- |
| 1/5 | Small isolated change with little integration risk. |
| 2/5 | Straightforward feature using existing project structures. |
| 3/5 | Several components, asynchronous work, or meaningful test coverage. |
| 4/5 | Cross-cutting feature with filesystem, media, or safety concerns. |
| 5/5 | High-risk architecture or data-integrity work requiring extensive validation. |

Estimates below are development days for one developer and include automated
tests, documentation, and a basic real-media verification pass. They are not
calendar commitments.

## Phase 0: Shared Application Services

Target: foundation for both CLI and GUI.

Status: completed in `1.5.2`.

| Work | Complexity | Estimate |
| --- | ---: | ---: |
| Extract scan orchestration from `dedupe.cli` into a reusable service | 3/5 | 2-3 days |
| Add structured stage/progress events | 3/5 | 1-2 days |
| Add cooperative cancellation while preserving completed cache work | 3/5 | 1-2 days |
| Add richer media metadata models for review | 3/5 | 1-2 days |

Required outcomes:

- the existing CLI remains compatible;
- the scanner can run without printing directly to a terminal;
- callers can receive stage, item-count, warning, completion, and failure
  events;
- cancellation never corrupts the cache or leaves a media file modified;
- detection logic remains independent of PySide6.

Implemented in `1.5.2`:

- `dedupe.service.ScanService` owns orchestration previously embedded in the CLI;
- `ScanEvent` reports stage start/completion, item progress, warnings, final
  completion, cancellation, and failures;
- `CancellationToken` cooperatively stops work between items and stages while
  retaining committed cache results;
- `ScanResult` exposes report data, file records, and basic media metadata;
- detailed codec, stream, resolution, track, chapter, and attachment metadata
  is available through a lazy, read-only probe API;
- the CLI delegates to the service and retains its existing command behavior.

## Phase 1: Read-Only Desktop Review

Target: first desktop release, with no file mutation.

Status: completed in `1.5.3`.

Recommended toolkit: PySide6 Widgets with Qt Multimedia. PySide6 should be an
optional dependency so CLI-only installations stay lightweight.

| Work | Complexity | Estimate |
| --- | ---: | ---: |
| Application shell, folder picker, and scan settings | 2/5 | 2-3 days |
| Background scan, progress, logs, and cancel control | 3/5 | 2-3 days |
| Result/cluster list with filters by evidence type | 3/5 | 2-3 days |
| Side-by-side image/video preview and synchronized seeking | 4/5 | 3-5 days |
| Size, duration, codec, resolution, audio, and subtitle metadata | 3/5 | 1-2 days |
| Open file/folder and export/open reports | 2/5 | 1 day |

Initial layout:

- left: scans and result filters;
- center: duplicate groups or candidate pairs;
- right: two selected files, preview, metadata, evidence, and decision area;
- bottom: progress, warnings, and current background operation.

Acceptance criteria:

- the UI remains responsive during long scans;
- one or several roots can be selected;
- every existing report category is visible;
- the user can compare paths, sizes, metadata, and media before making a
  decision;
- closing or cancelling the UI does not modify source media.

Implemented in `1.5.3`:

- optional `PySide6` GUI extra and `samesame-gui` entry point;
- multiple-root picker and scanner settings without adding Qt to CLI installs;
- background `ScanService` worker with progress, warnings, cancellation, and logs;
- filters and review entries for exact, video, image, audio, folder, and name results;
- side-by-side image, video, and audio panes with independent file selection;
- synchronized video seeking, shared playback, and independent mute controls;
- lazy codec, container, resolution, frame-rate, audio, subtitle, chapter, and
  attachment metadata;
- open-file/open-folder controls and HTML/JSON report export/open actions;
- close-time cancellation with no source-media mutation.

Estimated phase total: 11-17 days.

## Phase 2: Safe Duplicate Actions

Target: explicit review decisions and reversible cleanup.

Status: completed in `1.5.4`.

| Work | Complexity | Estimate |
| --- | ---: | ---: |
| Review states: keep, ignore, quarantine, recycle | 3/5 | 1-2 days |
| Preflight identity checks before every operation | 4/5 | 2-3 days |
| Quarantine/move service with collision handling | 4/5 | 2-3 days |
| Recycle-bin integration | 4/5 | 1-2 days |
| Persistent operation journal and recoverable failures | 4/5 | 2-3 days |
| Multi-file and cluster confirmation flow | 4/5 | 2-3 days |

Safety policy:

- permanent deletion is not part of the first implementation;
- exact duplicates may support batch selection after confirmation;
- video, image, and audio matches require explicit review;
- name-only hints never enable a delete/recycle batch action;
- before acting, SameSame verifies that the source still has the expected
  path, size, modification time, and content identity;
- quarantine is the preferred reversible default;
- the journal records requested, completed, skipped, and failed operations;
- undo is promised only for operations that are actually reversible.

Implemented in `1.5.4`:

- keep and ignore review decisions persisted in the operation journal;
- separate SHA-256 preflight identity captured after verifying the scan path,
  size, and modification time;
- a second identity check immediately before quarantine/recycle and validation
  of quarantined/restored output;
- collection-relative quarantine paths with root isolation and deterministic
  collision suffixes;
- operating-system recycle-bin integration through optional `Send2Trash`;
- SQLite journal states for requested, completed, skipped, and failed operations;
- journal viewer with restore enabled only for completed reversible quarantine;
- background file-action workers that prevent UI closure during an active move;
- explicit individual confirmation for content-backed matches;
- exact-group batch quarantine that keeps the selected copy;
- mutation controls disabled for folder and name-only hints;
- no permanent-delete operation.

Estimated phase total: 10-16 days.

## Phase 3: Independent Transcoding Module

Target: a tested backend that works without the GUI.

Status: completed in `1.5.5`.

Proposed package structure:

```text
dedupe/transcode/
  models.py
  presets.py
  probe.py
  command_builder.py
  runner.py
  validation.py
  queue.py
```

Proposed command: `samesame-transcode`. Duplicate scanning must not import the
desktop UI, and the transcoder must be usable from tests and scripts without a
window.

| Work | Complexity | Estimate |
| --- | ---: | ---: |
| Preset registry and FFmpeg capability detection | 3/5 | 1-2 days |
| ffprobe input analysis and command planning | 3/5 | 1-2 days |
| FFmpeg process runner, progress, cancellation, and logs | 4/5 | 2-3 days |
| Temporary output and stream/metadata preservation | 4/5 | 2-3 days |
| Output validation and source-protection rules | 4/5 | 2-3 days |
| Sequential job queue and CLI | 3/5 | 2-3 days |

The four initial presets and their exact encoder arguments come from
`docs/ANIME_ENCODING_PRESETS.md`:

- `anime_x265_max`: libx265 CRF 20, `slower`;
- `anime_x265_balanced`: libx265 CRF 22, `slow`;
- `anime_av1_nvenc`: av1_nvenc CQ 22, `p7`;
- `anime_hevc_nvenc`: hevc_nvenc CQ 22, `p7`.

Transcoding contract:

1. Probe the input before building a command.
2. Detect whether the selected encoder and options are available.
3. Write to a new temporary output; never encode over the source.
4. Prefer MKV for anime so copied audio, subtitles, chapters, metadata, and
   attachments can be preserved.
5. Encode only the video stream by default. Copy audio and subtitles unless
   the user explicitly chooses otherwise.
6. Parse progress, allow cancellation, and retain a diagnostic log.
7. Validate that the output opens, decodes, has an acceptable duration, and
   retains the expected streams.
8. Report the before/after size and savings. Quality-based CRF/CQ presets do
   not guarantee a target file size.
9. Keep the original after successful encoding. Replacing or recycling it is
   a separate, confirmed file action.

Test matrix:

- all four command builders;
- unsupported/missing encoder;
- video with multiple audio and subtitle tracks;
- chapters, metadata, and MKV attachments;
- cancellation and FFmpeg failure;
- invalid or truncated output;
- output larger than input;
- path conflicts and non-ASCII filenames;
- synthetic integration encode for every encoder available on the test host.

Implemented in `1.5.5`:

- independent `dedupe.transcode` package and `samesame-transcode` entry point;
- four exact CPU/NVENC presets with encoder and hardware initialization checks;
- ffprobe input model and MKV command planning that maps video, audio,
  subtitles, attachments, chapters, and metadata;
- cancellable FFmpeg runner with machine-readable progress and retained logs;
- unique temporary output, conflict protection, and source-preserving finalization;
- duration, stream-count, chapter, first-frame decode, and output-size validation;
- sequential queue, dry-run and JSON CLI modes, plus synthetic x265 integration
  coverage with multiple audio tracks, subtitle, chapter, and attachment.

Estimated phase total: 11-17 days.

## Phase 4: Transcoding in the Desktop UI

Target: compression queue available from reviewed files.

Status: completed in `1.6.0`.

| Work | Complexity | Estimate |
| --- | ---: | ---: |
| Preset selection and capability/status display | 2/5 | 1-2 days |
| Queue view with progress, cancel, retry, and logs | 3/5 | 2-3 days |
| Before/after metadata and size comparison | 3/5 | 1-2 days |
| Confirmed replace/quarantine workflow | 4/5 | 2-3 days |

The UI must explain unavailable hardware presets rather than failing after a
job starts. Completing a transcode must not automatically classify or delete a
duplicate.

Implemented in `1.6.0`:

- reviewed video results can populate a dedicated desktop transcode queue;
- asynchronous encoder and hardware initialization status for every preset;
- sequential background jobs with per-file progress, cancel, retry, output,
  and diagnostic-log controls;
- before/after duration, stream, chapter, size, and savings comparison;
- encode completion keeps the source and does not alter review classification;
- separately confirmed quarantine-first promotion with source and output
  SHA-256 identity checks, destination conflict rejection, journal integration, and
  attempted source rollback if promotion fails;
- scan and file-mutation controls are disabled while a queue or promotion is active.

Estimated phase total: 6-10 days.

## Release Sequence

Suggested milestones:

1. `1.5.2`: application-service refactor. Completed.
2. `1.5.3`: read-only desktop review. Completed.
3. `1.5.4`: safe quarantine/recycle workflow and operation journal. Completed.
4. `1.5.5`: independent transcoding module, CLI, presets, and validation. Completed.
5. `1.6.0`: desktop transcoding queue and replacement workflow. Completed.

The original GUI/transcoding roadmap is complete. Deferred work below remains
separately scoped and should continue to be evidence-driven.

## Deferred Work

These remain separate from the GUI/transcoding roadmap:

- optional episode-in-compilation segment matching;
- rotation/crop-resistant image matching;
- threshold-safe video/audio candidate blocking if real collections prove it
  necessary;
- broader threshold manifests and low-texture video tuning;
- advanced transcode quality metrics such as optional VMAF sampling;
- parallel GPU queues and remote workers.
