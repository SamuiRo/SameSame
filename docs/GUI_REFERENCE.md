# Desktop Interface Reference

This page documents the SameSame `1.10.3` desktop interface button by button.
The GUI is launched with `samesame-gui` and contains three main tabs:
**Duplicate review**, **Video compression**, and **Folder consolidation**.

Collection roots, thresholds, worker count, ffmpeg/ffprobe paths, the
quarantine path, skip flags, window geometry, and pane sizes are remembered
between launches. The **Allow OS Recycle Bin** opt-in is the one exception:
it is never persisted and always starts unchecked, so an unsafe setting from
a previous session can never carry over silently.

## Shared status area

The bottom status bar shows the current scan step, active fingerprint/hash
operation, completed and total item counts, percentage, and elapsed time. The
elapsed timer continues to update while a slow first item is being processed,
so an active scan remains visibly alive. The **Scan log and warnings** dock
records scan stages, warnings, completed file actions, and transcode-related
journal events.

Controls that could conflict with a running scan, file move, promotion, or
transcode are disabled until that operation finishes safely.

## Duplicate review tab

### Scan panel

| Control | Purpose |
| --- | --- |
| **Add folder** | Adds one collection root. Every normal subdirectory below it is scanned recursively. |
| **Remove** | Removes the selected roots from the pending scan list. It does not delete folders. |
| **Name provider** | Selects local heuristics, automatic Anthropic fallback, Anthropic, or LM Studio title normalization. |
| **LM Studio URL / model** | Configures the local OpenAI-compatible server when LM Studio is selected. |
| **Workers** | Sets background worker threads for the scan. |
| **Video / Image / Audio / Name / Folder threshold** | Sets the minimum match confidence for the corresponding evidence type. Video/image/audio use a calibrated scale where `0` means indistinguishable from unrelated content and `100` means identical (see `docs/THRESHOLDS.md`) - not a naive "percent similar." Defaults: video `20` (A-02), image `60` (A-06), audio `60` (A-14) — each derived from real measured evidence, and all three on the same calibrated scale, unlike name/folder which are plain percentages (name `92`, folder `50`). |
| **Skip video fingerprints** | Skips FFmpeg video sampling. Exact hashes and other enabled media stages still run. |
| **Skip image fingerprints** | Skips perceptual image matching. |
| **Skip audio fingerprints** | Skips FFmpeg Chromaprint audio matching. |
| **FFmpeg / FFprobe** | Overrides executable names or paths used by scanning, metadata, and transcoding. |
| **Quarantine → Browse** | Chooses the reversible SameSame Quarantine root. |
| **Allow OS Recycle Bin (may permanently delete)** | Unsafe opt-in. When unchecked, every Recycle command offers SameSame Quarantine instead. Enable only when accepting that Windows settings or storage type may permanently delete files. |
| **Start scan** | Starts a read-only background scan of every listed root. |
| **Cancel** | Requests cooperative cancellation. Already committed cache work is retained. |
| **Export reports** | Writes the current result as adjacent HTML and JSON reports. Enabled after a successful scan. |
| **Open report** | Opens the last exported HTML report. |
| **Operation journal** | Opens the persistent history of keep, ignore, quarantine, recycle, restore, and transcode source actions. |

The normal duplicate scanner does not follow directory symlinks or junctions.
Add the real target as another collection root when it must be included.

### Results panel

The category list filters the current results:

- **All results**;
- **Exact duplicates**;
- **Similar videos**;
- **Similar images**;
- **Similar audio**;
- **Mixed-evidence clusters** — a group of files connected by more than one
  evidence type (for example, two videos linked by both frame and audio
  similarity);
- **Folder pairs**;
- **Name hints**.

Each result is one transitive cluster of related files, not one pair: three
copies of the same episode across three folders appear as a single result
listing all three files, with per-pair similarity shown as evidence, rather
than three separate pairwise cases. Selecting a result loads its evidence and
candidates into the comparison panel. Folder and name-only results are review
hints and never enable file mutation.

**Exact duplicates** is the exception to "one cluster per result": it lists
every byte-identical group the scan found, including groups whose files are
*also* perceptually similar to something else and therefore appear again
inside a larger image or video cluster. Those two views of the same file are
both true, and listing exact groups separately is what makes them reachable -
clustering is single-linkage over all evidence, so on a large library most
exact groups otherwise vanish inside a bigger cluster (A-37).

**Quarantine all exact duplicates**, below the result list, acts on every one
of those groups in a single confirmed pass. It keeps one copy of each group -
the shallowest path, then the shortest, then alphabetically, so the copy
nearest the collection root survives and a `name (1).png` beside a `name.png`
loses - and moves the rest to quarantine. Nothing is lost: every file it moves
is byte-identical to one that stays. The confirmation names the group count,
the file count and the space it frees; every file is revalidated against its
scan identity before it moves, and the whole operation is journalled and
restorable like any other. Emptying the quarantine afterwards is the separate,
separately confirmed purge.

### Comparison and playback

| Control | Purpose |
| --- | --- |
| Candidate dropdown above each side | Selects which file from the current result is shown on the left or right. |
| **Play / Pause** | Controls playback for that side. |
| **Mute** | Mutes only that side. The right side starts muted. |
| Timeline slider | Seeks within the selected audio or video. |
| **Synchronize seeking** | Keeps both preview positions aligned while seeking. |
| **Play/Pause both** | Starts or pauses both valid media sources together. |
| **Open file** | Opens the selected file with the operating system. |
| **Open folder** | Opens its containing directory. |

The metadata box shows size, modification time, container, codecs, resolution,
frame rate, audio/subtitle languages, chapters, attachments, and probe warnings
where available.

### Review and individual file actions

| Control | Purpose |
| --- | --- |
| **Left selected file / Right selected file** | Chooses the target for the individual action buttons. |
| **Keep** | Records a review decision only; it does not move the file. |
| **Ignore** | Records an ignored review decision only. |
| **Quarantine…** | Revalidates the selected file and moves it to reversible SameSame Quarantine after confirmation. |
| **Recycle…** | In default Safe mode, offers Quarantine instead. With the red unsafe opt-in enabled, calls the OS Recycle Bin after another confirmation. |
| **Quarantine other copies…** | Any content-backed result (exact, video, image, audio, or mixed-evidence). Keeps the current target, SHA-256-compares every other copy in the cluster to it, and quarantines matching copies. |
| **Transcode videos…** | Adds supported videos in the current result to the shared transcode queue. |

Before a mutation, SameSame releases its own media previews, waits one UI event
turn, and retries transient Windows sharing violation error 32. It then checks
the recorded path, size, modification time, and SHA-256 identity.

### Batch cleanup checklist

The checklist contains every path in the current content-backed result.

| Control | Purpose |
| --- | --- |
| File checkbox | Explicitly includes that file in the next batch action. |
| **Check all except current** | Checks every candidate except the individual action target selected above. |
| **Clear checks** | Unchecks the entire list. |
| **Quarantine checked…** | Revalidates and quarantines every checked file sequentially. |
| **Recycle checked…** | Uses the same Safe-mode fallback or unsafe OS Recycle policy as the individual button. |

Selecting every file triggers an additional warning because no copy from that
review result will remain in its original location.

## Video compression tab

### Folder loading

| Control | Purpose |
| --- | --- |
| **Choose folder…** | Selects the batch source root. |
| **Load videos** | Searches to unlimited depth, follows linked directories with cycle protection, and probes every supported video. |
| **Cancel loading** | Stops after the current metadata probe returns. |

A red metadata cell means the file was discovered but FFprobe could not read
it. It remains selectable, but the transcode job will fail safely if probing
still fails when the queue starts.

### Quick selection filters

| Control | Purpose |
| --- | --- |
| Extension checklist | Chooses which containers can match the quick-selection rule. |
| **Minimum / Maximum size** | Filters by MiB. A maximum of zero means no upper limit. |
| **Minimum / Maximum duration** | Filters by minutes. A maximum of zero means no upper limit. |
| **Select matching** | Checks files satisfying every active extension, size, and duration rule; nonmatching rows are unchecked. |
| **Clear** | Unchecks every loaded video. |

### Compression settings

The preset dropdown contains the four presets from
`ANIME_ENCODING_PRESETS.md` and **Custom settings…**.

| Custom control | Purpose |
| --- | --- |
| **Encoder** | Selects `libx265`, `av1_nvenc`, or `hevc_nvenc`. |
| **CRF / CQ** | Sets libx265 CRF or NVENC constant quality from 0 through 51. |
| **Speed preset** | Uses x265 speed names or NVENC `p1` through `p7`. |
| **Pixel format** | Selects 8-bit `yuv420p` or 10-bit `yuv420p10le`. |
| **x265 parameters** | Adds the provided `-x265-params` string for libx265. |
| **Full-resolution multipass (AV1 NVENC)** | Adds NVENC full-resolution multipass for AV1. |

| Other control | Purpose |
| --- | --- |
| Output layout | **Replace each original in place** (default) gives every result its original's exact path and name, so the folder tree is unchanged and only the sizes drop; a non-MKV source keeps its name and gains the `.mkv` extension. **Mirror the folder tree** recreates the subfolder structure under a chosen output folder with original filenames — the only layout that can write to another drive. **Beside each source** keeps the old `<original>.<preset>.mkv` naming so both files sit together. |
| **Output → Browse…** | Selects the output folder for the mirror layout. Disabled while replacing in place, since results go back where their sources were. |
| Video checkbox in the table | Explicitly includes the row in the queue. |
| **Remove each original** | Enables post-compression cleanup. Only a validated output that is genuinely smaller than its source ever triggers it, and each original is SHA-256 identity-checked immediately beforehand. |
| Cleanup mode | Chooses **Quarantine** (default, reversible through the journal), **Recycle Bin** (still requires the red unsafe opt-in in Duplicate review), or **Delete permanently** (irreversible; no journal restore). |
| Cleanup note | States what the chosen mode will actually do to free space, and warns when the quarantine folder is on the same drive as the videos — that move is a rename, so it releases nothing and the run ends with less free space than it started. |
| **Open compression queue with checked videos** | Opens the queue with the checked paths, selected preset, output folder, collection root, and cleanup policy. |

## Transcode queue dialog

| Control | Purpose |
| --- | --- |
| **Preset** | Selects the preset applied to jobs that have not already completed. |
| Capability status | Reports whether FFmpeg lists the encoder and whether required GPU initialization succeeds. |
| **Recheck** | Runs the encoder/capability test again. |
| **Output folder…** | Changes the output directory. Blank keeps outputs beside their sources. |
| **Add videos…** | Adds supported individual video files without duplicating existing rows. |
| **Remove selected** | Removes selected queue rows only; it does not touch source files. |
| Cleanup checkbox and mode | Mirrors the compression tab's selection: quarantine, Recycle Bin, or permanent delete, each with its own start confirmation. Recycle stays disabled until the unsafe opt-in in Duplicate review is enabled. |
| **Start queue** | Runs pending jobs sequentially. Existing successful jobs are skipped. |
| **Cancel** | Stops the active FFmpeg process and marks remaining queued jobs skipped. |
| **Retry failed** | Requeues failed, cancelled, or skipped rows. |
| **Open output** | Opens the completed validated MKV. |
| **Open log** | Opens the retained FFmpeg diagnostic log. |
| **Quarantine original + promote…** | Separately confirms source quarantine, then promotes the validated MKV into the collection with identity checks and rollback attempts. |

The details pane compares input/output duration, stream counts, chapters, size,
savings, validation warnings, and source-cleanup outcome. Encoding never writes
over the source file.

Replacing in place requires a cleanup mode and forces the checkbox on: the
encode cannot take the source's path while the source is still there. The encode
is staged as `<name>.samesame-new.mkv` beside its source, and only after the
source is cleaned up does it move onto the final path and re-verify its SHA-256.
If that move fails after a quarantine, the original is restored automatically;
after a recycle or a permanent delete there is nothing to restore, so the queue
reports loudly that the validated encode under its staging name is now the only
copy. A successful replacement also removes its FFmpeg log, which is named after
the staging file; a failed one keeps the log.

A job whose encode came out **no smaller than its source** is reported as
`Skipped`, not `Completed`: the encode is deleted instead of committed, and the
source is never eligible for cleanup. Without that rule a library holding
already-efficient files could lose originals while using *more* disk than
before. The size column reads `larger; encode discarded`, and `Retry failed`
requeues the row so a different preset can be tried.

## Operation journal dialog

| Control | Purpose |
| --- | --- |
| **Refresh** | Reloads recent operations from SQLite. |
| **Restore quarantine** | Restores one completed reversible quarantine operation after identity and destination checks. |
| **Empty quarantine…** | Permanently deletes every restorable file in the quarantine folder, after a confirmation stating the count and total size. This is what actually releases space a quarantine move only reserved on a single drive. Each purged operation keeps its recorded identity but stops being restorable. Only paths the journal itself recorded, and that still live under the configured quarantine root, are touched — anything else is reported as refused and left alone. |
| **Open quarantine folder** | Opens the configured quarantine root. |
| **Close** | Closes the journal window. |

Restore is disabled for keep/ignore decisions, failed/skipped actions, OS
Recycle operations, and quarantined sources whose original path is occupied.

## Folder consolidation tab

### Folder and name selection

| Control | Purpose |
| --- | --- |
| **Choose title folder** | Selects the title-level folder containing the surviving files and source wrappers. |
| **Analyze folders** | Reads the current filesystem, discovers folders containing files, and proposes mappings. |
| **Final folder name** | Accepts the program suggestion or an edited canonical title name. The complete destination path is always shown. |

### Mapping and exact preview

The mapping table displays **Source folder → Target subfolder**. A target of
`.` means the files move directly into the final title folder. Relative target
folders preserve seasons, sequels, or other meaningful structure. Each source
folder can be excluded.

**Generate exact move preview** validates every mapping and fills the file
table with the complete source and destination paths. Ready files start
checked, can be unchecked individually, and are the only files eligible to
move. Existing or duplicate destinations are marked as conflicts and disabled;
SameSame never overwrites them.

### Execution and undo

| Control | Purpose |
| --- | --- |
| **Consolidate selected files** | Shows the final folder and folder mappings again, then performs the confirmed journaled batch. |
| **Cancel** | Requests cooperative cancellation, checked between files. Only available while a batch is executing, not while an undo is running. |
| **Undo last consolidation** | SHA-256-verifies and restores the latest completed consolidation batch when original paths are free. |

Every move rechecks the previewed size and modification time, calculates
SHA-256, and verifies destination content. A failed batch attempts to restore
all already moved files in reverse order. A cancelled batch is left exactly
as undo-able as a completed one: every file moved so far is fully verified,
and **Undo last consolidation** restores them. Closing the window while a
batch is running requests cancellation the same way. Empty source
directories are removed only after the complete batch succeeds.
