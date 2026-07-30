# Project Audit — 1.7.0

Audit date: 2026-07-29
Audited version: `1.7.0` (`master` @ `64d883f` plus the uncommitted scan-status
work in `dedupe/gui/main_window.py`, `docs/GUI_REFERENCE.md`, `tests/test_gui.py`)

This audit complements `docs/AUDIT.md` (which closed the `1.6.6` safety review).
It is a **forward-looking** audit: it looks for correctness, scalability,
robustness, and maintainability problems in the code as it stands today, and
proposes a staged improvement plan. Nothing here has been fixed yet.

## Revision 2 — 2026-07-29

Revision 1 raised five open questions. They are now answered, and the answers
change both the priorities and one of the recommendations.

| Question | Decision |
| --- | --- |
| Extend folder comparison to real directories? | **Yes** — F-16 moves into the plan. |
| Video candidate blocking vs. detection quality? | **Detection quality is a confirmed real problem.** Obvious anime duplicates are missed even at a 50% threshold. This is now the largest workstream. |
| Optional NumPy dependency acceptable? | **Yes**, if it improves quality or capability. |
| Extract all 15 frames in one ffmpeg pass? | **Withdrawn — see the correction below.** |
| Target scale? | **100–1000 files, up to ~600 GB** (i.e. ~600 MB average per file). |

A new problem was also reported from real use and is confirmed: similar (non-exact)
matches are presented as **pairs**, so three copies of the same episode produce
three separate review cases instead of one group. This is **F-23** and it is the
highest-value fix in the document.

### Correction to revision 2, question 4

Revision 1 suggested replacing the 15 per-frame ffmpeg invocations with a single
invocation using a `select` filter. **That recommendation was wrong for this
content profile and is withdrawn.** Measured on a 1080p 10-bit x265 sample:

| Approach | 2-minute file |
| --- | ---: |
| 15 fast-seek invocations (current) | 6.50 s |
| One full-decode pass with a `select` filter | 6.35 s |
| 15 invocations with `-noaccurate_seek` + ffmpeg-side 32×32 downscale | 6.41 s |
| 15 bare `ffmpeg -version` spawns (process overhead only) | **0.32 s** |

Process spawn is only ~21 ms each, and PNG encoding plus the Pillow resize are
negligible. The cost is **decode from the preceding keyframe** — about 0.55 s per
sampled frame, matching a ~250-frame GOP. The two approaches therefore break even
at ~2 minutes, but a single-pass decode scales with **duration** while 15 seeks
scale with **GOP size**. On a real 24-minute episode a full decode is roughly
76 s versus about 8 s for 15 seeks — the current design is right and would have
become ~10× slower.

See **F-28** and **F-31** for the optimisation levers that do exist.

### Severity recalibration for the stated scale

Two revision-1 findings were sized for a 200,000-file library. At 1,000 files
they are cheap hygiene rather than blockers, and two others get worse:

| Finding | Was | Now | Reason at 1,000 files / 600 GB |
| --- | --- | --- | --- |
| F-04 progress event flood | High | **Low** | 1,000 events is nothing for the Qt event loop. Still worth throttling, but not urgent. |
| F-08 linear record lookup | High | **Low** | 1,000 records × a 100-file batch is trivial. One-hour fix, keep it. |
| F-15 repeated SHA-256 passes | Medium | **High** | At ~600 MB average, one promote reads ~2.4 GB. Across a library that is terabytes of redundant I/O. |
| F-02 cancellation backlog | Critical | **Critical** | Fingerprinting 1,000 large videos takes hours; an uncancellable backlog is the worst case in the product. |

## Method

Read in full: `service`, `scanner`, `cache`, `exact_hash`, `video_fingerprint`,
`image_fingerprint`, `audio_fingerprint`, `name_normalizer`, `folder_compare`,
`metadata`, `models`, `events`, `progress`, `report`, `cli`, `consolidation`,
the whole `actions/` package, the whole `transcode/` package, and the `gui/`
package. Skimmed: `benchmark`, `thresholds`, `tools/`, `tests/`, `docs/`.

Executed on the project `.venv` (Python 3.11.9):

| Check | Result |
| --- | --- |
| `python -m pytest -q` | 96 passed, 4 subtests — **but see F-11 (flaky)** |
| `python -m unittest discover -s tests` | passes |
| `ruff check .` | All checks passed |
| `ruff format --check .` | **23 files would be reformatted** |
| CI configuration | **none found** (`.github/` does not exist) |
| Type checking / coverage gate | **none configured** |

Micro-benchmarks used as evidence are reproduced inline with the findings.

## Summary of outcome

The project is genuinely in good shape on the axis it was built around:
**source protection**. Identity preflight, journaling, quarantine-first
promotion, rollback, and the "never overwrite" rules are consistent and well
tested. That work should not be disturbed.

The weaknesses are in three other areas:

1. **Scalability.** The matching phase is single-threaded pure Python with a
   pair-generation bug that makes video matching super-quadratic. The scanner is
   usable on the ~60-file collection it was tuned against and will not survive a
   few thousand videos.
2. **Responsiveness and lifecycle.** Cancellation does not actually stop queued
   work; progress events are emitted per item into the Qt event loop; the GUI
   holds OS file handles and leaks dialogs; several enable/disable paths
   disagree with each other.
3. **Engineering hygiene.** No CI, no type checking, no coverage gate, format
   drift, one flaky test, a required dependency for an optional feature, and
   duplicated subprocess/SQLite setup code.

Severity is assigned by user impact:
**Critical** = data loss or the feature does not work at real scale;
**High** = user-visible malfunction or serious slowdown;
**Medium** = correctness/robustness gap with a narrow trigger;
**Low** = hygiene and polish.

---

## Critical

### F-01 · Video candidate pairing is super-quadratic and its limiter is dead code

`dedupe/video_fingerprint.py:211-221`

```python
def _candidate_pairs(candidates, max_candidates_per_bucket, threshold):
    del max_candidates_per_bucket, threshold   # <-- both arguments discarded
    pairs = set()
    for left in range(len(candidates)):
        pairs.update((left, right) for right in range(left + 1, len(candidates)))
    return pairs
```

Two compounding problems:

1. The function ignores both of its limiting arguments and always materialises
   **every** pair. `--max-video-candidates-per-bucket` is therefore inert; the
   CLI help string already admits this ("Reserved compatibility setting"), and
   so does `docs/CONFIG.md:245`.
2. `find_video_matches` (`video_fingerprint.py:334-342`) calls it **once per
   one-second duration bucket**, and each bucket re-collects every record inside
   a ±50%/±15-minute window. The same pair is regenerated in hundreds of
   buckets. The `seen` set suppresses duplicate *comparisons*, but only after
   the pairs have already been built.

Measured (uniform 24-minute videos, ±2 minutes, default ratio/delta gates):

| Videos | Duration buckets | Pairs generated | Pairs actually needed | Waste | Time in `_candidate_pairs` alone |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 200 | 134 | 2,666,600 | 19,900 | 134× | 0.29 s |
| 500 | 211 | 26,322,250 | 124,750 | 211× | 4.42 s |
| 1000 | 238 | 118,881,000 | 499,500 | 238× | ~20 s (extrapolated) |

That is pure set-building cost, before a single frame is compared. It also
briefly holds a set of hundreds of thousands of tuples per bucket.

**Fix.** Enumerate each unordered pair exactly once: iterate buckets in sorted
order and pair each record only with records in buckets `>= key` (skipping
already-emitted combinations), or drop the bucket loop entirely and sort records
by duration with a two-pointer sweep over the compatible window. Then either
implement real candidate limiting or delete the parameter from the CLI, config,
and docs — a knob that does nothing is worse than no knob.

Complexity 3/5 · 1-2 days including a regression test that asserts the pair
count for a synthetic duration distribution.

### F-02 · Cancelling a scan waits for the entire queued backlog

`dedupe/exact_hash.py:113,140` · `video_fingerprint.py:303,315` ·
`image_fingerprint.py:108` · `audio_fingerprint.py:101,113` ·
`name_normalizer.py:360`

Every fingerprinting stage uses this shape:

```python
with ThreadPoolExecutor(max_workers=workers) as executor:
    futures = {executor.submit(...) for record in targets}
    for future in tqdm(as_completed(futures), ...):   # raises ScanCancelled
        ...
```

`ScanCancelled` propagates out of the `with` block, so `ThreadPoolExecutor.
__exit__` runs `shutdown(wait=True)` — which **does not cancel pending work
items**. Every already-submitted job still runs to completion before the
exception surfaces.

Verified:

```
cancel at item 3 of 40 queued jobs took 4.01 s
(= all 40 jobs x 0.2 s / 2 workers = the full backlog)
```

Real consequence: pressing **Cancel** during video fingerprinting of a
5,000-file library, with each fingerprint spawning 15 ffmpeg processes, leaves
the user waiting for the remaining thousands of fingerprints. The GUI shows
"Cancelling…" and stays that way, `closeEvent` refuses to close the window
(`main_window.py:1050-1055`), and `docs/STATUS.md` claims "responsive progress,
warning logs, and cooperative cancel control".

**Fix.** Replace the context manager with an explicit
`executor.shutdown(wait=False, cancel_futures=True)` on the cancellation path,
and have the worker functions themselves check the token before starting work
(so already-running ffmpeg calls are the only thing left to wait for). Consider
terminating the in-flight `subprocess.run` calls too — currently a 180-second
audio fingerprint timeout is the worst case per running worker.

Complexity 3/5 · 1-2 days. This is the single most user-visible defect.

### F-03 · Anthropic name normalization can crash the whole scan with `KeyError`

`dedupe/name_normalizer.py:171-204` and `386-389`

`_normalize_batch_with_claude` returns only the entries the model actually
produced:

```python
for item in results:
    raw = by_id.get(int(item["id"]))
    if raw is None:
        continue
    output.append(NormalizedName(...))
return output
```

The LM Studio path deliberately backfills the gaps
(`_coerce_normalized_results`, `name_normalizer.py:228-229`):

```python
missing = set(batch) - {item.raw_name for item in output}
output.extend(fallback_normalize(raw) for raw in missing)
```

The Anthropic path has no such backfill. `normalize_names` then ends with an
unguarded lookup:

```python
return {record.path_key: normalized[inputs_by_path[record.path_key]] for record in records}
```

If the model omits an id — entirely possible with `BATCH_SIZE = 150` titles and
`max_tokens=4096`, where a truncated tool call silently drops trailing
entries — this raises `KeyError`, which `ScanService.run` converts into a
`FAILED` event. **The entire scan is lost**, after all hashing and fingerprinting
work has already been done, because a title could not be tidied up.

**Fix.** Apply the same backfill in `_normalize_batch_with_claude` (or better:
route it through `_coerce_normalized_results`), and make the final comprehension
fall back to `fallback_normalize` rather than indexing. Add a regression test
with a stubbed client that returns a partial result set.

Complexity 1/5 · half a day. Cheapest high-value fix in this audit.

---

## High

### F-04 · Progress events are emitted per item into the Qt event loop

`dedupe/progress.py:75-89` · `dedupe/gui/main_window.py:390-404`

`tqdm()` emits one `ScanEvent(PROGRESS)` per yielded item, and the GUI connects
`ScanWorker.event` across threads, so each one becomes a queued cross-thread
signal. `scan_folders` wraps its file iterator in `tqdm`
(`scanner.py:95`), so a 200,000-file library queues 200,000 signals during
discovery alone — each one running `_handle_event`, which now also calls
`_refresh_scan_status()` (string building, `list(ScanStage)`, `.index()`,
`QLabel.setText`, `QProgressBar.setValue`).

The event loop cannot drain that faster than the worker produces it, so the
window becomes unresponsive precisely during the phase the progress bar exists
to report on.

**Fix.** Throttle at the source: emit a PROGRESS event at most every N items or
every ~100 ms (whichever comes first), always emitting the final one. The new
1-second `_scan_status_timer` in the working tree already provides the "still
alive" signal, so the per-item events are not needed for liveness.

Complexity 2/5 · half a day.

### F-05 · Every ffmpeg/ffprobe call flashes a console window on Windows

12 call sites: `video_fingerprint.py:57,85` · `audio_fingerprint.py:21,48` ·
`metadata.py:209` · `transcode/probe.py:56,96` · `transcode/capabilities.py:58`
· `transcode/validation.py:47` · `transcode/runner.py:69` ·
`benchmark.py:41`

None of them pass `creationflags=subprocess.CREATE_NO_WINDOW` (verified: no
occurrence anywhere in the project source). Under a windowed launcher
(`pythonw.exe`, a PyInstaller `--noconsole` build, or a Start Menu shortcut) each
child process opens and closes a console window. Video fingerprinting spawns
**15 ffmpeg processes per video** (`fingerprint_video`, `video_fingerprint.py:139-144`),
so scanning 500 videos produces roughly 7,500 window flashes.

`transcode/runner.py:69` additionally does not redirect `stdin`; the ffmpeg
command does pass `-nostdin`, but the fingerprint and probe commands do not.

**Fix.** Add a single `dedupe/process.py` helper that owns `creationflags`
(Windows only), `stdin=subprocess.DEVNULL`, default timeouts, and consistent
exception handling, then route all 12 call sites through it. This also cleans up
F-09.

Complexity 2/5 · 1 day.

### F-06 · Preview keeps OS handles on media and produces a flaky test

`dedupe/gui/preview.py:262` · observed in `tests/test_gui.py`

`MediaPane._load_path` hands the file to `QMediaPlayer`, which holds an open
handle for as long as the source is set. The handle is only dropped when a
mutating action explicitly calls `release_paths`
(`main_window.py:788-793`) or the pane is cleared. Selecting a result and then
switching tabs, renaming the file in Explorer, or letting an external tool touch
it hits a Windows sharing violation. `docs/AUDIT.md` already fixed the *action*
path; the *idle preview* path is still holding handles.

This is not theoretical — it makes the suite flaky. In a full-suite run:

```
FAILED tests/test_gui.py::GuiSmokeTests::test_video_review_enables_transcode_queue_action
PermissionError: [WinError 32] ... 'episode-b.mkv'  (tempfile cleanup)
1 failed, 95 passed
```

Running `tests/test_gui.py` alone passes; a second full run passed. So the
result depends on when Qt Multimedia happens to release the handle.

**Fix.** Release the player source whenever the pane loses the current selection
(tab change, result change, window deactivation) rather than only on mutation,
and add an explicit teardown in the GUI tests that clears both panes and pumps
the event loop before the temporary directory is removed.

Complexity 3/5 · 1-2 days including making the test deterministic.

### F-07 · Busy-state gating is recomputed in five places and they disagree

`dedupe/gui/main_window.py:485-500, 821-850, 981-995, 997-1011, 1013-1033`

There are five independent methods that each re-derive which controls should be
enabled — `_set_running`, `_action_finished`, `_transcode_busy_changed`,
`_compression_busy_changed`, `_consolidation_busy_changed` — and each checks a
different subset of the five possible busy sources.

Concrete divergences:

- `_transcode_busy_changed` (line 989) re-enables `comparison.set_actions_enabled`
  without checking `consolidation_tab.is_busy`. Start a consolidation, then let a
  transcode queue finish → destructive file-action buttons come back while a
  journaled consolidation batch is still moving files.
- `_action_finished` (lines 828-837) re-enables the comparison actions,
  `folder_list`, and `settings_group` without checking `consolidation_tab.is_busy`
  or `compression_tab.is_loading`.
- `_action_finished` (lines 836-837) unconditionally re-enables
  `category_filter` and `result_list`.

Every one of these is guarded a second time inside the request handlers
(`_request_action` etc. re-check all four conditions), so no unsafe operation can
actually start. The bug is a **UI honesty** bug: buttons look available and then
silently do nothing when clicked.

**Fix.** One `_update_enabled_state()` method that reads all five busy flags and
sets every control, called from every state transition. Delete the other four
implementations. This also removes ~60 lines from a 1,079-line file.

Complexity 2/5 · 1 day.

### F-08 · Batch actions are `O(files × batch)` because record lookup is a linear scan

`dedupe/gui/main_window.py:578-587`

```python
def _record_for_path(self, path_text, *, allow_directory=False):
    if self._result is not None:
        for record in self._result.records:      # linear scan of the whole scan
            if record.path_key == path_text:
                return record
```

Called once per path in `_request_batch_quarantine` and `_request_batch_action`.
A 100-file batch over a 200,000-file scan is 20 million string comparisons on
the GUI thread, freezing the window before the confirmation dialog appears.

**Fix.** Build `self._records_by_path: dict[str, FileRecord]` in
`_scan_completed` and look up in it. `preview.MediaPane.configure` already does
exactly this (`preview.py:173`) — reuse the same map.

Complexity 1/5 · 1 hour.

---

## Medium

### F-09 · Inconsistent subprocess error handling; `OSError` escapes in two places

`video_fingerprint.py:60` catches `(SubprocessError, ValueError, JSONDecodeError)`
but **not** `OSError`. `audio_fingerprint.py:28,52` catch only `SubprocessError`.
`metadata.py:226` and the whole `transcode/` package correctly catch
`(OSError, SubprocessError, …)`.

So a missing/renamed/permission-denied ffmpeg binary raises `FileNotFoundError`
or `PermissionError` out of `get_duration`/`fingerprint_audio` inside a worker
thread instead of degrading to a warning. `check_video_tools` runs first and
usually prevents this, but there is a real window: the binary can disappear or a
network path can drop between the check and the call, and `resolve_binary`
returns a *path* that is then invoked much later.

**Fix.** Folded into the F-05 shared `dedupe/process.py` helper.
Complexity 1/5 · included in F-05.

### F-10 · Metadata probe can raise out of a `QRunnable` with no handler

`dedupe/gui/worker.py:69-72` · `dedupe/metadata.py:168`

```python
@Slot()
def run(self) -> None:
    metadata = probe_media_metadata(self.record, self.ffprobe)   # no try/except
    self.signals.completed.emit(self.record.path_key, metadata)
```

`_probe_image` catches `(OSError, ValueError)`, but Pillow's
`DecompressionBombError` derives directly from `Exception`:

```
(PIL.Image.DecompressionBombError, Exception, BaseException, object)
```

`image_fingerprint.py:40` catches it correctly; `metadata.py:168` does not. A
large PNG selected in the review pane therefore throws an unhandled exception
out of a thread-pool runnable. Under PySide6 an unhandled Python exception in a
slot aborts the process.

Separately, `MetadataTask` keeps a raw reference in `MediaPane._metadata_tasks`
and its `MetadataSignals` outlives the pane if the window closes mid-probe —
`ComparisonWidget.stop()` waits only 2 seconds (`preview.py:445`) and does not
cancel the ffprobe subprocess.

**Fix.** Wrap `MetadataTask.run` in `try/except Exception` and emit an error
`MediaMetadata`; add `DecompressionBombError` to `_probe_image`; add a global
`sys.excepthook` in `gui/app.py` that logs and shows a dialog instead of
aborting.

Complexity 2/5 · half a day.

### F-11 · SQLite is opened without WAL, `busy_timeout`, or `check_same_thread` policy

`dedupe/cache.py:16` · `dedupe/actions/journal.py:748` ·
`dedupe/consolidation.py:486`

All three use bare `sqlite3.connect(path)`. Consequences:

- `operations.sqlite3` is shared by `OperationJournal` **and**
  `ConsolidationService`. `JournalDialog.refresh()` runs on the GUI thread while
  `ConsolidationWorker` writes from a background thread (they are wired together
  via `journal_changed`, `main_window.py:1035-1037`). Without
  `PRAGMA busy_timeout` a concurrent write surfaces as `database is locked`
  instead of waiting.
- No WAL means readers and writers block each other outright.
- `docs/AUDIT.md` lists "one active SameSame process" as an accepted limit, but
  even a single process has these two writers.

**Fix.** One `_connect(path)` helper applying
`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`,
`PRAGMA busy_timeout=5000`, used by all three modules.

Complexity 2/5 · half a day.

### F-12 · Consolidation cannot be cancelled and re-reads every byte twice

`dedupe/consolidation.py:265-328, 412-431`

`ConsolidationService.execute` has no `CancellationToken`. `closeEvent`
(`main_window.py:1073-1077`) can only wait. `_move_one` computes SHA-256 of the
source *and* of the destination, so a 500 GB consolidation reads 1 TB, with a
progress callback that fires only between files — a single 20 GB file shows a
frozen progress bar for minutes with no cancel button.

Additionally `_record_move_requested` (line 560) issues a
`SELECT COUNT(*)` plus a `commit()` per move, and `_record_move_status` commits
again: three fsyncs per file.

**Fix.** Thread a `CancellationToken` through `execute`/`_move_one` and check it
between files (a partially completed batch is already rollback-capable, and the
existing undo covers it). Report byte-level progress from inside `_hash_file`.
Track the sequence number in Python instead of querying it, and batch the
journal commits per move rather than per statement.

Complexity 3/5 · 1-2 days.

### F-13 · `restore=` injection silently disables the recycle safety gate

`dedupe/actions/service.py:40`

```python
self.allow_unsafe_recycle = allow_unsafe_recycle or recycle is not None
```

Passing a custom `recycle` callable turns on unsafe recycle regardless of the
explicit flag. `source_cleanup.recycle_transcode_source` reproduces the same
rule (`source_cleanup.py:655`: `allow_unsafe_recycle=recycle is not None`).

This exists so the tests can inject a fake recycler, but it means the safety gate
is bypassable by a constructor argument rather than by the deliberate red
opt-in. The GUI never passes `recycle=`, so users are not currently affected —
but the invariant "recycle requires an explicit unsafe opt-in" is not actually
enforced by the service.

**Fix.** Keep the two concepts separate: `recycle` selects *which* implementation
to call, `allow_unsafe_recycle` decides *whether* calling is permitted. Update
the tests to pass both explicitly.

Complexity 1/5 · 2 hours.

### F-14 · Union-find uses unbounded recursion

`dedupe/folder_compare.py:29-33`

```python
def find(path: str) -> str:
    parent.setdefault(path, path)
    if parent[path] != path:
        parent[path] = find(parent[path])     # recursive path compression
    return parent[path]
```

`union` has no rank/size heuristic (`parent[right_root] = left_root`), so a chain
of pairwise matches `a–b`, `b–c`, `c–d`, … builds a linear parent chain. Roughly
1,000 chained video/image matches — plausible in a large photo library where
near-identical frames form a chain — exceeds the default recursion limit and
raises `RecursionError` at the clustering stage, after all the expensive work.

**Fix.** Iterative `find` with a two-pass path compression, plus union by size.
Complexity 1/5 · 2 hours.

### F-15 · Transcode hashes each file up to four times

`transcode/queue.py:321,341` (source + output) and
`transcode/promotion.py:539,546,564` (source + output + promoted output)

A verified promote-after-encode of a 20 GB source with a 12 GB output reads
roughly **84 GB** in SHA-256 passes on top of the encode itself. Each pass is
single-threaded `hashlib` over 4 MiB chunks.

The hashes are genuinely required for the safety guarantees, so this is not a
bug — but the *same* content is hashed more than once within one workflow.

**Fix.** Cache the digest alongside `(path, size, mtime, inode)` for the duration
of the queue/promotion workflow and reuse it when the stat tuple is unchanged.
Re-hash only when the identity check indicates the file moved or changed.

Complexity 2/5 · 1 day.

### F-16 · Folder comparison only ever compares scan roots

`dedupe/folder_compare.py:117-119`

```python
for record in records:
    by_folder.setdefault(record.root_key, []).append(record)
```

`root_key` is the *collection root the user added*, not the directory containing
the file. So "Folder pairs" with one selected root produces zero results, and
with two roots produces exactly one pair. The most common real use case —
"is `D:\Anime\Show\Season 1` a duplicate of `D:\Anime\Show\Season 1 (copy)`?" —
is not covered at all.

The implementation matches what `README.md:94` and `docs/STATUS.md:104-106`
describe, so this is a **scope gap, not a broken promise** — but it is the single
biggest missing capability relative to what users expect from a duplicate finder.

**Fix.** Key `by_folder` on `record.path.parent`, cap the pair count with a
shared-cluster prefilter (comparing every directory pair is quadratic in
directory count), and keep the root-level pairs as an additional roll-up.

Complexity 3/5 · 2-3 days.

---

## Low

### F-17 · Transcode dialogs are never destroyed and accumulate signal connections

`dedupe/gui/main_window.py:961-978`

`TranscodeDialog` is created with `parent=self` and never gets
`WA_DeleteOnClose`. `_transcode_closed` drops the Python reference
(`main_window.py:1045`), but the C++ object survives as a child of the main
window, keeping its table model, workers, and log buffers alive. Line 976:

```python
self.allow_unsafe_recycle.toggled.connect(dialog.set_allow_unsafe_recycle)
```

is never disconnected, so each opened dialog adds a permanent receiver. Over a
long session — open a compression queue, close it, repeat — memory grows and the
checkbox notifies every dead dialog.

**Fix.** `dialog.setAttribute(Qt.WA_DeleteOnClose)` and disconnect in
`_transcode_closed`. Complexity 1/5 · 1 hour.

### F-18 · The GUI remembers nothing between launches

`dedupe/gui/app.py` sets `QCoreApplication.setOrganizationName("SameSame")` —
which exists precisely to enable `QSettings` — but no `QSettings` is ever
constructed. Collection roots, thresholds, worker count, ffmpeg/ffprobe paths,
quarantine root, skip flags, window geometry, and splitter sizes are all lost on
exit. For a tool whose main workflow is "scan the same library again next week",
this is the most-felt piece of missing polish.

**Fix.** Persist scan settings and window state in `QSettings`; deliberately do
**not** persist the `allow_unsafe_recycle` opt-in, which should reset to safe on
every launch. Complexity 2/5 · 1 day.

### F-19 · `anthropic` is a hard runtime dependency for an optional feature

`pyproject.toml:29`

`anthropic>=0.39.0` sits in `[project.dependencies]`, pulling in `httpx`,
`pydantic`, `anyio`, and their transitive tree into every install — including
CLI-only installs that use `--name-provider none`. The import itself is already
lazy (`name_normalizer.py:172`, inside the function).

`MODEL_NAME = "claude-haiku-4-5"` (`name_normalizer.py:37`) is also worth
revisiting; Claude 5 family models are now available.

**Fix.** Move `anthropic` to an `ai` extra, keep the lazy import, and raise a
clear message when the provider is selected without the extra installed.
Complexity 1/5 · 2 hours.

### F-20 · HTML report: mixed languages, no search, single monolithic string

`dedupe/report.py:117-242`

`<html lang="uk">` with Ukrainian `<h2>` headings
(`Точні дублікати файлів`, `Схожі відео`, …) inside an otherwise entirely
English product. Escaping is correct throughout (`html.escape` on every
user-controlled string), so there is no injection issue.

The document is built as one f-string and written at once — a report for a large
library becomes a multi-megabyte HTML file with thousands of `<details>`
elements, no filtering, no sorting, and no search.

**Fix.** Pick one language (English, to match the rest), and add a small
self-contained filter/search box plus per-section counts.
Complexity 2/5 · 1 day.

### F-21 · Formatting drift, no CI, no type checking, no coverage gate

- `ruff format --check .` reports **23 files** needing reformatting (16 source,
  7 test). `docs/AUDIT.md:113-114` already noted this and deferred it.
- No `.github/workflows`: nothing runs `pytest`/`ruff` on push.
- No `mypy`/`pyright` configuration, despite the codebase being fully annotated
  and `from __future__ import annotations` used everywhere — the type hints are
  currently decorative.
- No coverage measurement, so the 96 tests' actual reach is unknown.

**Fix.** One mechanical `ruff format` commit touching nothing else; a GitHub
Actions workflow running `ruff check`, `ruff format --check`, and the non-ffmpeg
test subset; a `mypy` config starting in non-strict mode on `dedupe/` excluding
`gui/`.

Complexity 2/5 · 1 day for all four.

### F-22 · Minor observations

| Item | Location | Note |
| --- | --- | --- |
| `check_video_tools` called twice per scan | `service.py:256,320` | Two `shutil.which` round trips; resolve once and pass down. |
| `_available_path` TOCTOU | `actions/service.py:330-338` | `exists()` then use; single-process only, so currently safe. |
| Cache rows are never pruned | `cache.py` | Documented in `AUDIT.md`; add a `--vacuum-cache` maintenance command. |
| `scan_folders` has no cancellation | `scanner.py:85-119` | Discovery of a huge tree cannot be interrupted before the first stage boundary. |
| `rglob` swallows nothing | `scanner.py:94` | Permission errors during traversal are not converted to warnings. |
| `find_name_hints` fuzzy loop is O(k²) | `name_normalizer.py:432-457` | Full cross product over distinct title keys before the cheap year/episode filter; group by `(year, episode)` first. |
| Failed consolidation undo leaves batch status stale | `consolidation.py:352-372` | An exception mid-undo skips `_finish_batch`; the batch still reads `completed`. Recoverable by re-running undo. |
| `_remove_empty_tree` on the failure path | `consolidation.py:292-294` | Removes pre-existing empty subdirectories under `final_root` even though `final_root_existed` was recorded. |
| `ScanResult.metadata` built eagerly | `service.py:408` | One `MediaMetadata` per record for every scan, even when the GUI never displays them. |

---

## Detection quality and result grouping (added in revision 2)

This section is the answer to "obvious anime duplicates are missed, and the ones
that are found are split into pairs". Every claim below is either a direct code
reference or a measurement.

### F-23 · Critical — Similar matches are pairwise; the transitive clusters are computed and then discarded

`dedupe/gui/result_items.py:39-66` · `dedupe/models.py:113-122` ·
`dedupe/service.py:384-392`

`build_review_items` creates one `ReviewItem` per **match object**:

```python
for index, match in enumerate(report.video_matches, start=1):
    items.append(ReviewItem(category="video", paths=(match.left, match.right), ...))
```

Exact duplicates get `paths=tuple(group.paths)` — a real group of any size — and
name hints get a `paths` list. Video, image, and audio get exactly two paths.

So for `folder1/A.mp4`, `folder2/B.mp4`, `folder3/C.mp4` (the same episode with
different intros) the reviewer is shown:

```
Video match 1 · A ? B
Video match 2 · B ? C
Video match 3 · A ? C
```

The growth is quadratic: 3 copies → 3 cases, 5 copies → 10, 8 copies → 28.

**The most frustrating part is that the correct grouping already exists.**
`build_cluster_assignments` (`folder_compare.py:18-109`) runs a union-find over
exact groups plus video, image, and audio matches and produces exactly the
transitive clusters the user wants. But its output is passed only to
`compare_folders` and then dropped — `DedupeReport` has no cluster field, so the
clusters never reach the GUI, the HTML report, or the JSON report.

Downstream consequences, all confirmed in code:

| Effect | Location |
| --- | --- |
| The batch-cleanup checklist can never contain more than 2 files for a similar match | `preview.py:423` — `_populate_batch_files(item.paths)` |
| "Check all except current" is meaningless with 2 entries | `preview.py:526-536` |
| "Quarantine other copies…" is hard-restricted to exact groups, so keeper-based batch cleanup is **unavailable for similar videos entirely** | `main_window.py:630`, `preview.py:485-487` |
| A decision on pair (A,B) does not mark (B,C) or (A,C) — `_group_id` hashes the pair's own paths | `main_window.py:573-576` |
| After quarantining B from case 1, cases 2 and 3 still list B; preflight then fails with "Cannot access source file" | `actions/preflight.py:675-682` |
| "Transcode videos…" only ever receives the 2 paths of the current pair | `preview.py:561-565` |
| HTML/JSON reports are pairwise for the same reason | `report.py:40-71` |

**Fix.** Add a `content_clusters: list[ContentCluster]` field to `DedupeReport`,
populate it from the union-find that already runs in the clustering stage, and
make `build_review_items` emit one `ReviewItem` per cluster with the full path
list plus the per-pair evidence for display. Lift the `category == "exact"`
restriction on keeper-based batch cleanup to any content-backed cluster — the
per-file SHA-256 keeper comparison in `perform_if_matches`
(`actions/service.py:98-153`) is what actually makes that safe, and it does not
depend on the category.

**Design decision required.** Full transitive closure can over-merge: if A~B and
B~C but A≁C, all three land in one cluster. For "which of these do I keep?" that
is usually what the user wants, but the UI must show *why* each file is in the
cluster so a wrong merge is visible. Options: (a) full closure with a visible
per-pair evidence matrix; (b) closure only above a higher "merge" threshold,
keeping weaker links as separate pairs; (c) closure plus a warning badge when the
cluster is not fully connected. Recommendation: **(a)**, with the minimum pairwise
similarity in the cluster shown as its confidence — `build_cluster_assignments`
already computes exactly that value (`folder_compare.py:82-90`).

Complexity 3/5 · 2-3 days.

### F-24 · Critical — The similarity scale has a noise floor of ~52%, so threshold values do not mean what they appear to mean

`dedupe/video_fingerprint.py:148-195`

`hamming_similarity` converts a 64-bit Hamming distance to a percentage. Two
*unrelated* 64-bit perceptual hashes differ in ~32 of 64 bits, so they score
~50%. `ordered_sequence_similarity` then takes the best subsequence alignment,
which biases it slightly upward. Measured over 300 pairs of completely unrelated
random fingerprints:

```
UNRELATED videos: mean = 52.3%   min = 49.6%   max = 55.5%
```

And with only part of the sample grid genuinely corresponding:

| Frames truly identical | Reported similarity |
| ---: | ---: |
| 15 / 15 | 100.0% |
| 12 / 15 | 96.0% |
| 10 / 15 | 93.3% |
| 8 / 15 | 88.0% |
| 6 / 15 | 79.3% |
| 4 / 15 | 70.0% |
| 2 / 15 | 60.6% |
| 0 / 15 | 52.4% |

Two consequences:

1. **The usable range is ~52–100, not 0–100.** The GUI spin box offers 0–100
   (`main_window.py:243-249`) and the CLI validates 0–100
   (`cli.py:295`). Setting 50% does not mean "half similar" — it means "accept
   literally every duration-compatible pair", which floods the review list.
2. **A genuine match with partial alignment scores far below the default.** A
   pair where 6 of 15 samples land on corresponding content — exactly what a
   different intro produces — scores 79.3% and is rejected by the default 85%
   threshold.

**Fix.** Rescale to a calibrated confidence, e.g.
`max(0, (raw - 50) / 50 * 100)`, so 0% means "indistinguishable from unrelated"
and 100% means identical. Re-derive the default thresholds against
`docs/threshold-baseline.json` on the new scale, bump the fingerprint version so
cached values are not mixed across scales, and document the change in
`docs/THRESHOLDS.md`. This affects video, image (`image_fingerprint.py:45-51`,
which mixes an 80%-weighted structure term with the same noise floor) and audio
(`audio_fingerprint.py:57-75`, same 50% floor over 32-bit values).

Complexity 3/5 · 2 days, mostly re-benchmarking.

### F-25 · Critical — The alignment recheck is arithmetically unreachable at any threshold ≤ 65

`dedupe/video_fingerprint.py:30, 360-365`

```python
ALIGNMENT_RECHECK_FLOOR = 65.0
...
similarity = ordered_sequence_similarity(left.fingerprint, right.fingerprint)
if ALIGNMENT_RECHECK_FLOOR <= similarity < threshold and 0.05 < delta <= ALIGNMENT_RECHECK_MAX_DELTA:
    similarity = video_similarity(left, right, ffmpeg, aligned_fingerprints)
```

The recheck is the only mechanism that re-samples the longer video on the
shorter one's timeline — the mechanism specifically designed to catch
different-intro and alternate-cut duplicates. Its guard requires
`65 <= similarity < threshold`, which is an **empty interval whenever
`threshold <= 65`**:

```
threshold=50: 65 <= s <  50  ->  IMPOSSIBLE - recheck never runs
threshold=60: 65 <= s <  60  ->  IMPOSSIBLE - recheck never runs
threshold=65: 65 <= s <  65  ->  IMPOSSIBLE - recheck never runs
threshold=75: active for s in [65, 75)
threshold=85: active for s in [65, 85)
```

So the natural user reaction to missed matches — lowering the threshold — turns
the alignment rescue **off**. Combined with F-24 (unrelated pairs score ~52), a
50% threshold simultaneously floods the list with noise *and* disables the one
feature that would have found the real match.

Two further limits on the same code path:

- `_duration_aligned_similarity` returns the plain unaligned score when
  `duration_delta <= 0.05` (`video_fingerprint.py:248-249`). Two releases with
  the *same* total length but content shifted internally (a different OP of the
  same length, or an OP added and an ED trimmed) are never re-aligned.
- Only two offsets are tried, `0.0` and `duration_delta`
  (`video_fingerprint.py:252`). That covers extra content entirely at the start
  or entirely at the end, not extra content in the middle.

**Fix.** Decouple the recheck from the accept threshold: run it whenever the raw
score is above the noise floor and the durations are compatible, subject to a
budget (it costs a re-fingerprint of the longer file). Make the floor relative to
the calibrated scale from F-24. Allow re-alignment when the duration delta is
zero. Consider a coarse offset sweep instead of two fixed offsets.

Complexity 3/5 · 2 days.

### F-26 · High — One failed frame out of 15 silently removes a video from matching entirely

`dedupe/video_fingerprint.py:129-145`

```python
for fraction in SAMPLE_POINTS:
    frame = _extract_frame(path, timestamp, ffmpeg)
    if frame is None:
        return None          # all-or-nothing across 15 independent subprocesses
    hashes.append(phash(frame))
```

`_extract_frame` returns `None` on any `SubprocessError` (including a 60-second
timeout), any `OSError`, or simply empty stdout. Fifteen independent chances to
fail; one failure discards the other fourteen good frames.

`find_video_matches` then stores nothing (`video_fingerprint.py:320-323`,
`if fingerprint:`), and the bucket build requires a fingerprint
(`video_fingerprint.py:327-329`):

```python
if record.duration is not None and record.fingerprint:
    buckets[int(round(record.duration))].append(record)
```

The file is therefore **absent from video matching altogether** — it cannot be
missed by a threshold, because it is never a candidate. The same applies when
`get_duration` fails: `duration is None` excludes the file just as completely.

The user is told only indirectly: `_extract_frame` logs a per-frame warning
(`video_fingerprint.py:91`) that lands in a scan log which may hold thousands of
lines. There is **no summary** of how many files were excluded, and the GUI has
no indication that a given file was skipped.

This is the most likely explanation for "obvious duplicates are missing even at a
50% threshold", because at 50% every scored pair passes (F-24) — so a missing
result almost certainly means the file was never scored.

**Fix.** Three parts:
1. Tolerate partial failures — keep the successful frames, record which sample
   indices are missing, and compare only positions present in both fingerprints
   (with a minimum-coverage requirement). This needs a fingerprint version bump.
2. Retry a failed extraction once with a small timestamp nudge before giving up.
3. Emit a stage summary — "N of M videos could not be fingerprinted and were
   excluded from video matching" — as a `WARNING` event, and surface the list in
   the GUI so the user can see the gap.

Complexity 3/5 · 2 days. **Highest-value detection fix after F-23.**

### F-27 · High — Sampling is proportional to total duration, so any length difference de-synchronises every sample

`dedupe/video_fingerprint.py:26-27, 137-144`

```python
SAMPLE_POINTS = tuple((index + 1) / (SAMPLE_COUNT + 1) for index in range(SAMPLE_COUNT))
timestamp = max(0.0, start_offset + span * fraction)
```

Samples are taken at fixed *fractions of the whole file*. For version A of 1440 s
and version B of 1530 s (a 90-second intro difference), sample *i* of A is at
content time `1440·(i+1)/16` while sample *i* of B is at `1530·(i+1)/16 − 90`.
The offset between them is not constant — it varies from −90 s at the first
sample to 0 s at the last. It is a **stretch**, not a shift.

`ordered_sequence_similarity` handles a clean index shift very well (measured:
a 1-frame grid shift still scores 98.7%, a 5-frame shift 93.3%), but a stretch
leaves most samples landing on unrelated content, which is why such pairs collapse
toward the noise floor and never reach the F-25 recheck gate.

**Fix.** This is what the aligned re-fingerprint in `_duration_aligned_similarity`
is for; F-25 makes it reachable. Longer term, sample on an **absolute** grid
(for example every 30 s from the start, capped at N samples) so two files of
different length share a common timeline and a constant offset, which the DP can
absorb directly.

Complexity 3/5 · 2 days, coupled with F-25 and a fingerprint version bump.

### F-28 · Medium — Video and audio worker counts are silently capped at 4

`dedupe/service.py:276` and `service.py:340`

```python
workers=min(config.workers, 4),
```

The GUI spin box allows 1–32 (`main_window.py:160`) and the CLI allows 1–64
(`cli.py:303`), but both are clamped to 4 for the two stages that dominate
wall-clock time. There is no warning that the setting was ignored.

Measured extraction cost on 1080p 10-bit x265: ~0.55 s per sampled frame,
~6.5 s per file for 15 samples, dominated by decode-from-keyframe. For 1,000
videos that is roughly 1.8 hours single-threaded, ~27 minutes at the hard-coded
4 workers, and proportionally less if the cap is raised on a machine that can
sustain the I/O.

**Fix.** Make the cap a documented option with a sensible default derived from
the CPU count, and log when the requested value is reduced.

Complexity 1/5 · 2 hours.

### F-29 · Medium — Duration gating rejects pairs before any content evidence, with hard-coded limits

`dedupe/video_fingerprint.py:28-29, 198-208, 337-341`

```python
DEFAULT_MAX_DURATION_RATIO = 1.5
DEFAULT_MAX_DURATION_DELTA = 15 * 60.0
```

Two files are excluded before comparison if the longer is more than 1.5× the
shorter **or** more than 15 minutes longer. Neither value is reachable from the
CLI, the config file, or the GUI — `find_video_matches` accepts them as
parameters but `ScanService` never passes them (`service.py:270-278`).

For anime episodes this is usually fine. It fails for short content: a 90-second
OP versus the same OP at 140 seconds is a ratio of 1.56 and is silently
rejected. It is also the intended behaviour for excluding an episode from a
compilation, so the value cannot simply be raised without re-testing.

**Fix.** Expose both as documented options, and report duration-gated pairs in a
separate low-confidence bucket rather than dropping them silently.

Complexity 2/5 · half a day.

### F-30 · Medium — 15 samples over a 24-minute episode is one frame per 96 seconds

`dedupe/video_fingerprint.py:26`

`SAMPLE_COUNT = 15` is fixed for all durations. On a 24-minute episode that is
one sample every 96 seconds, and the alignment DP has only 15 points to work
with — so a single mis-sampled frame costs 6.7% of the score
(see the F-24 table: 12/15 correct already drops to 96.0%).

`docs/STATUS.md:157-159` already records "two synthetic low-texture video false
negatives" at the 85% threshold. Anime is the low-texture case: large flat colour
areas produce weak DCT coefficients, so `phash` is least discriminative on
exactly the target content. `phash` also resizes to a square 32×32 without
preserving aspect ratio (`video_fingerprint.py:107`), so a 4:3 and a 16:9 release
of the same content — or a letterboxed versus a cropped encode — produce
unrelated hashes.

**Fix (investigation, not a mechanical change).** Candidates, in order of
expected value per unit of risk: scale `SAMPLE_COUNT` with duration; add a
colour-histogram component alongside the structure hash, as the image path
already does; detect and strip letterboxing before hashing (ffmpeg `cropdetect`);
evaluate a stronger descriptor on a labelled anime corpus. This must be measured
against `docs/threshold-baseline.json` rather than guessed — and the corpus needs
real anime pairs, which it currently lacks.

Complexity 4/5 · 3-5 days including corpus work.

### F-31 · Low — Frame extraction cost is decode-bound; the only real lever is fewer decoded frames

Measured breakdown per sampled frame on 1080p 10-bit x265 (~0.55 s):
process spawn ~21 ms, PNG encode plus Pillow LANCZOS resize negligible
(ffmpeg-side 32×32 downscaling gained only 1.1×), `-noaccurate_seek` gained
nothing. The remainder is decoding the ~250-frame GOP that precedes the target
timestamp.

The one untested lever is snapping sample timestamps to keyframes, which would
decode one frame instead of ~250. The risk is that keyframe positions differ
between two encodes of the same content, which would break alignment — so it
must be evaluated against the corpus alongside F-30, not adopted blindly.

Complexity 2/5 · 1 day to evaluate.

---

## What is working well

Worth stating explicitly, because the improvement plan must not regress any of it:

- **Source protection is coherent and tested.** Preflight identity → immediate
  re-verification → move → post-move content verification → journaled outcome →
  attempted rollback. The 12 action tests and 5 promotion tests cover the
  failure paths, not just the happy path.
- **The `transcode` package is genuinely independent.** No Qt import, no scanner
  import, usable from a script, with its own CLI and real ffmpeg integration
  tests.
- **`ScanService` / `ScanEvent` / `CancellationToken`** is a clean seam. The CLI
  and the GUI really do share one orchestration path.
- **Threshold work is evidence-driven**, with a reproducible benchmark corpus
  and recorded FP/FN numbers rather than guessed constants.
- **Documentation is unusually honest** — `docs/CONFIG.md` openly labels the
  inert candidate setting as reserved, and `docs/AUDIT.md` lists residual risks
  instead of claiming completeness.

---

## Proposed improvement plan (revision 2)

Sequenced so that each stage is independently shippable and testable. Estimates
follow the `docs/ROADMAP.md` convention (developer-days, tests and docs
included). Nothing below changes the source-protection rules.

Revision 2 reorders the plan around the confirmed priority: **result grouping and
detection quality first**, because those are what make the tool wrong rather
than merely slow, and because the target scale (1,000 files) makes several
revision-1 performance items non-urgent.

### Stage A — Quick correctness fixes (~2 days) · done 2026-07-31

Small, independent, low-risk. Ship first so later stages build on a stable base.

| # | Work | Complexity | Status |
| --- | --- | ---: | --- |
| F-03 | Backfill Anthropic normalization gaps; stop `KeyError` from killing a scan | 1/5 | Done |
| F-13 | Separate `recycle` injection from the unsafe opt-in | 1/5 | Done |
| F-14 | Iterative union-find with union by size | 1/5 | Done |
| F-10 | Guard `MetadataTask.run`; catch `DecompressionBombError`; add `sys.excepthook` | 2/5 | Done |
| F-17 | Destroy transcode dialogs and disconnect their signals | 1/5 | Done |
| F-08 | `dict` lookup for review records | 1/5 | Done |
| F-28 | Stop silently clamping the worker count to 4 | 1/5 | Done |

All seven items landed together. `python -m pytest -q` passes (97 passed, 4
subtests — one new regression test added for F-03), `ruff check .` is clean.
F-06's pre-existing preview-handle flakiness in
`test_video_review_enables_transcode_queue_action` was observed intermittently
during verification, unrelated to these changes and unchanged by them; it
remains tracked under Stage E.

### Stage B — Result grouping (~4 days) · highest value · done 2026-07-31

Turns N(N−1)/2 pairwise cases into one case per real duplicate set. This is the
reported pain point and it unlocks batch cleanup for similar matches.

| # | Work | Complexity | Status |
| --- | --- | ---: | --- |
| F-23a | Add `ContentCluster` to `DedupeReport`; populate from the existing union-find | 2/5 | Done |
| F-23b | One `ReviewItem` per cluster with the full path list and a per-pair evidence matrix | 3/5 | Done |
| F-23c | Lift the exact-only restriction on keeper-based batch cleanup to any content-backed cluster | 2/5 | Done |
| F-23d | Cluster-level `_group_id` so one decision covers the whole set | 1/5 | Done (free consequence of F-23b) |
| F-23e | Cluster sections in the HTML and JSON reports | 2/5 | Done |

Acceptance: three copies of one episode in three folders produce **one** review
case listing three files, with a keeper selector and a working batch action; a
decision recorded once is visible for the whole cluster.

**Implementation notes.**
`folder_compare.py` gained `build_content_clusters()` (pure function over
match/group lists, no records/names needed) and `resolve_content_clusters()`
(prefers `report.content_clusters`, recomputes on the fly if a caller built a
`DedupeReport` without going through `ScanService` — tests, scripts, older
reports). `build_cluster_assignments()` now calls `build_content_clusters()`
internally instead of duplicating the union-find, and returns
`(assignments, content_clusters)`. `build_review_items()` in
`gui/result_items.py` emits one `ReviewItem` per cluster (category = cluster
level, including a new `"content"` category for clusters connected by more
than one evidence type) with an evidence string built from the cluster's
per-pair matches. `_group_id` in `main_window.py` needed no code change — it
already hashes category + sorted paths, so one `ReviewItem` per cluster makes
it cluster-scoped automatically. The exact-only restriction on
"Quarantine other copies…" / batch checked-quarantine was lifted to all
content-backed categories in `main_window.py` and `preview.py`. `report.py`'s
HTML export now renders one `<details>` block per cluster (plus a new "Змішані
докази" section for mixed-evidence clusters); `report_to_dict()` populates
`content_clusters` via the same fallback when the caller didn't.
Acceptance covered by
`tests/test_gui.py::GuiSmokeTests::test_similar_video_chain_is_one_review_case_with_working_batch_cleanup`
and `tests/test_core.py::CoreTests::test_html_and_json_reports_group_chained_matches_into_one_cluster`.
Full suite: 99 passed, 4 subtests, `ruff check .` clean.

### Stage C — Detection quality (~7 days) · needs measurement, not guessing · partially done 2026-07-31

The order matters: recalibrate the scale before re-tuning anything, because every
threshold in the project is expressed on the broken scale.

**Scope decision (2026-07-31).** No real anime/video duplicate corpus was
available in this environment — only the 27 synthetic pairs in
`docs/threshold-baseline.json`. Per the audit's own warning below ("every
other item is unverifiable without it"), F-24, F-27, and F-30 were
**deliberately deferred** rather than implemented against guessed numbers.
Only the items that are pure logic/reachability fixes - correct regardless of
where the similarity scale eventually lands - were done now.

| # | Work | Complexity | Status |
| --- | --- | ---: | --- |
| F-26 | Tolerate partial fingerprints; retry once; report excluded files as a stage summary | 3/5 | Done |
| F-25 | Make the alignment recheck reachable | 3/5 | Done (reachability fix only; see note) |
| F-29 | Expose the duration ratio/delta gates | 2/5 | Done (gate values only; gated-pair reporting deferred) |
| F-24 | Rescale similarity to a calibrated 0–100 confidence; bump the fingerprint version; re-derive defaults | 3/5 | **Deferred — needs real corpus** |
| F-27 | Absolute-time sampling grid so different-length versions share a timeline | 3/5 | **Deferred — needs real corpus** |
| F-30 | Descriptor/sampling investigation | 4/5 | **Deferred — needs real corpus** |
| — | **Build a real labelled anime corpus** | 3/5 | **Still open — blocks the three items above** |

**Implementation notes.**
F-25: `video_fingerprint.py`'s recheck guard no longer gates on `similarity <
threshold` — it now fires whenever the raw score clears
`ALIGNMENT_RECHECK_FLOOR` (65) and the duration delta is in range, independent
of the accept threshold. This was the specific reachability bug (empty
interval whenever `threshold <= 65`). The related "allow re-alignment at zero
duration delta" sub-item was **not** done: with only two fixed offsets
(`0.0`, `duration_delta`), re-aligning at delta≈0 tries the same offset twice
and changes nothing observable - it's only meaningful together with F-27's
offset-sweep rework, which is deferred. F-26: `fingerprint_video` now keeps
whatever frames it can extract (retrying each failed sample once at a nudged
timestamp) instead of discarding all 15 on a single failure, requires at
least `MINIMUM_FRAME_COVERAGE` (50%) of samples to still consider the file
fingerprinted, and logs a stage-level summary of how many files were
excluded. `VIDEO_FINGERPRINT_VERSION` bumped 2 → 3 so old cached fingerprints
(always exactly 15 samples) aren't compared against new partial ones under
stale assumptions. `ordered_sequence_similarity`'s existing alignment DP
already tolerates unequal-length fingerprints (proven by the pre-existing
"tolerates inserted samples" test), so dropping failed samples needed no
change there. F-29: `DEFAULT_MAX_DURATION_RATIO`/`DEFAULT_MAX_DURATION_DELTA`
are now real CLI flags (`--max-video-duration-ratio`,
`--max-video-duration-delta`), config keys (flat and nested under `video:`),
and threaded through `ScanOptions`/`ScanConfiguration` into
`find_video_matches`. Reporting duration-gated pairs in a separate bucket
(the second half of F-29's original write-up) was **not** done - it's a
report/UI feature, not a reachability fix, and was out of the approved scope
for this pass.

Regression coverage: `test_alignment_recheck_still_fires_at_a_low_threshold`
(fails without the F-25 fix - reproduces the empty-interval bug directly),
`test_video_fingerprint_tolerates_one_missing_frame`,
`test_video_fingerprint_retries_a_failed_frame_once`,
`test_video_fingerprint_gives_up_below_minimum_coverage`,
`test_video_matching_logs_a_summary_when_files_are_excluded`,
`test_cli_exposes_video_duration_gate_overrides`,
`test_config_file_video_section_maps_duration_gate_keys` (all in
`tests/test_core.py`). Full suite: 106 passed, 7 subtests, `ruff check .`
clean.

Acceptance (full stage, still pending F-24/F-27/F-30/corpus): the reported
missed pairs are found at the default threshold, with no new false positives
on the corpus, and every threshold change is backed by FP/FN numbers.

**Do the corpus first if only one thing from this stage is done.** Every other
item is unverifiable without it.

### Stage D — Responsiveness and I/O (~4 days) · done 2026-07-31

| # | Work | Complexity | Status |
| --- | --- | ---: | --- |
| F-02 | Real cancellation: `cancel_futures=True` + pre-work token checks | 3/5 | Done (cancel_futures only; see note) |
| F-05 + F-09 | Shared `dedupe/process.py`: no console windows, uniform errors, `stdin=DEVNULL` | 2/5 | Done |
| F-15 | Reuse verified digests within a transcode/promotion workflow | 2/5 | Done |
| F-07 | Single `_update_enabled_state()` in `MainWindow` | 2/5 | Done |
| F-11 | WAL + `busy_timeout` for all three SQLite users | 2/5 | Done |

**Implementation notes.**

F-02: added `cancellable_executor()` to `dedupe/progress.py` — a
`ThreadPoolExecutor` wrapper whose `finally` calls
`shutdown(wait=False, cancel_futures=True)` instead of the context manager's
default `shutdown(wait=True)`, so a `ScanCancelled` raised while iterating
`as_completed(futures)` drops the still-queued backlog instead of waiting for
it. All 8 call sites (`exact_hash.py` ×2, `video_fingerprint.py` ×2,
`image_fingerprint.py`, `audio_fingerprint.py` ×2, `name_normalizer.py`) now
route through it. Measured: 40 jobs / 2 workers at 0.2s each went from ~4.0s
to cancel down to ~0.5s
(`tests/test_service.py::test_cancelling_mid_backlog_does_not_wait_for_queued_work`,
which fails at ~4s against the old raw-`ThreadPoolExecutor` pattern). The
audit's second half — "have the worker functions themselves check the token
before starting work" — was **not** done: `check_cancelled()` reads a
`ContextVar` that isn't propagated into `ThreadPoolExecutor` worker threads by
default (unlike asyncio's `run_in_executor`), so this would need a
`CancellationToken` threaded explicitly through every fingerprinting module's
public signature (`find_exact_duplicates`, `find_video_matches`,
`find_image_matches`, `find_audio_matches`, `normalize_names`) — a larger,
separate API change deferred to avoid destabilizing five modules' public
interfaces in one pass. `cancel_futures=True` already delivers the primary
observed symptom (an unresponsive Cancel button); only already-running jobs
(bounded by `workers`) now continue in the background after cancel returns.

F-05+F-09: new `dedupe/process.py` (`run()`/`popen()`) sets
`creationflags=CREATE_NO_WINDOW` on Windows and `stdin=subprocess.DEVNULL` by
default; all 11 `subprocess.run`/`Popen` call sites (`video_fingerprint.py`,
`audio_fingerprint.py`, `metadata.py`, `transcode/probe.py`,
`transcode/capabilities.py`, `transcode/validation.py`, `transcode/runner.py`,
`benchmark.py`) route through it. Fixed the two spots that caught only
`SubprocessError`, not `OSError` (`video_fingerprint.get_duration`,
`audio_fingerprint.check_chromaprint`/`fingerprint_audio`).

F-15: `promote_transcode` no longer re-hashes the source or the encoded
output before quarantining — it trusts `TranscodeResult.input_sha256`/
`output_sha256` (already computed and double-verified by the queue moments
earlier) whenever the file's `(size, mtime)` still matches what the queue
recorded, added a new `output_modified_at` field to `TranscodeResult` to make
that check possible for the output. The **post-move** re-hash of `promoted`
stays unconditional, since it's verifying the move itself, not re-confirming
old state — that's the safety-critical check and it was left untouched. Cuts
the hash passes for a full encode→promote workflow from ~5 to ~3. Did not add
inode to the identity tuple as the audit's fix text suggested:
`Path.write_bytes()`-style in-place overwrites keep the same inode, so it
would not have added protection for the same-size-tamper case the existing
tests already exercise; the codebase's existing `FileIdentity` model
elsewhere also uses only size+mtime, so this stays consistent with that.
Regression: `test_promotion_does_not_rehash_unchanged_source_or_output`
(asserts exactly 1 hash call, down from 3) plus the pre-existing tamper
-detection tests still pass unmodified.

F-07: replaced `_set_running`, `_action_finished`'s inline enable/disable
block, `_transcode_busy_changed`, `_compression_busy_changed`, and
`_consolidation_busy_changed` with one `_update_enabled_state()` that reads
all five busy flags (scan/action/transcode/compression/consolidation) every
time. Verified each widget updates its own `is_busy`/`is_loading` property
before emitting `busy_changed`, so reading fresh state instead of trusting
the signal's `bool` parameter is safe. Regression:
`test_finishing_transcode_does_not_reenable_actions_while_consolidation_is_busy`
reproduces the exact divergence the audit flagged (comparison actions
re-enabled after a transcode queue finished, even with a consolidation batch
still moving files) and fails against the old per-method logic.
`main_window.py` net -28 lines.

F-11: new `dedupe/db.py` with one `connect(path)` applying
`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`,
`PRAGMA busy_timeout=5000`; used by `cache.py`, `actions/journal.py`, and
`consolidation.py`'s `_connect()`.

Full suite: 100 passed, 7 subtests, `ruff check .` clean.

### Stage E — Scale and hygiene (~4 days)

| # | Work | Complexity |
| --- | --- | ---: |
| F-01 | Rewrite video pair enumeration; implement or remove the candidate cap | 3/5 |
| F-06 | Deterministic preview handle release; de-flake the GUI test | 3/5 |
| F-21 | `ruff format` commit, GitHub Actions, `mypy` baseline, coverage report | 2/5 |
| F-12 | Cancellable consolidation with byte-level progress | 3/5 |
| F-19 | Move `anthropic` to an extra; review the model id | 1/5 |
| F-04 | Throttle progress events | 2/5 |
| F-22 | `find_name_hints` blocking by `(year, episode)`; resolve ffmpeg once | 1/5 |

F-01 stays in the plan even at 1,000 files: it still materialises ~119 million
tuples, and it gets worse as thresholds are lowered during Stage C.

### Stage F — Product (~6 days)

| # | Work | Complexity |
| --- | --- | ---: |
| F-16 | Per-directory folder comparison instead of root-only (**confirmed wanted**) | 3/5 |
| F-18 | `QSettings` persistence for scan settings and window state | 2/5 |
| F-20 | One-language HTML report with search and filters | 2/5 |
| F-30 + F-31 | Descriptor and sampling investigation on the Stage C corpus | 4/5 |

### Where NumPy fits (approved)

Now permitted. Worth using in exactly three places, in this order:

1. **`ordered_sequence_similarity`** — measured at 0.35 ms per pair, i.e. ~3
   minutes per 500k pairs single-threaded, and it is the whole matching phase.
   A vectorised DP over a precomputed distance matrix is the single biggest win.
2. **`audio_similarity`** (`audio_fingerprint.py:57-75`) — 17 shifts over a
   full-length fingerprint in a Python loop; a direct XOR/`popcount` array
   operation.
3. **`phash`** — measured at ~1.0 ms per frame, i.e. ~0.015 s of CPU per video.
   **Not worth it.** Extraction dominates by a factor of 400.

Keep NumPy **optional**, with the current pure-Python paths retained as the
fallback, so CLI-only installs stay light.

### Remaining open decisions

1. **F-23 cluster policy** — full transitive closure with a visible evidence
   matrix (recommended), closure only above a higher merge threshold, or closure
   with an "incomplete cluster" badge?
2. **F-24 is a breaking change to every threshold value** in configs, docs, and
   the baseline. Do it as one deliberate release with a migration note, or keep
   the raw scale and only relabel it in the UI?
3. **Corpus** — how many real anime pairs can be labelled, and can the missed
   pairs from real use be captured as the first regression cases? Without those
   exact files, Stage C is tuned blind.
4. **F-29** — raising the duration gate helps short content but risks
   re-introducing the episode-in-compilation false positives the gate was added
   to prevent. Confirm against the corpus before changing the default.
