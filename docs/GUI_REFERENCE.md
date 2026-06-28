# Desktop Interface Reference

This page documents the SameSame `1.6.6` desktop interface button by button.
The GUI is launched with `samesame-gui` and contains two main tabs:
**Duplicate review** and **Video compression**.

## Shared status area

The bottom status bar shows the current operation and progress. The **Scan log
and warnings** dock records scan stages, warnings, completed file actions, and
transcode-related journal events.

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
| **Video / Image / Audio / Name / Folder threshold** | Sets the minimum similarity percentage for the corresponding evidence type. |
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
- **Folder pairs**;
- **Name hints**.

Selecting a result loads its evidence and candidates into the comparison panel.
Folder and name-only results are review hints and never enable file mutation.

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
| **Quarantine other copies…** | Exact groups only. Keeps the current target, SHA-256-compares every other copy to it, and quarantines matching copies. |
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
| **Output → Browse…** | Selects one output directory. Blank means beside each source. |
| Video checkbox in the table | Explicitly includes the row in the queue. |
| **Remove originals after compression (safe quarantine by default)** | After validated output, uses reversible Quarantine by default. It uses unsafe OS Recycle only when that separate setting is enabled in Duplicate review. |
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
| Cleanup checkbox | Uses SameSame Quarantine in Safe mode or clearly labels unsafe OS Recycle when enabled. |
| **Start queue** | Runs pending jobs sequentially. Existing successful jobs are skipped. |
| **Cancel** | Stops the active FFmpeg process and marks remaining queued jobs skipped. |
| **Retry failed** | Requeues failed, cancelled, or skipped rows. |
| **Open output** | Opens the completed validated MKV. |
| **Open log** | Opens the retained FFmpeg diagnostic log. |
| **Quarantine original + promote…** | Separately confirms source quarantine, then promotes the validated MKV into the collection with identity checks and rollback attempts. |

The details pane compares input/output duration, stream counts, chapters, size,
savings, validation warnings, and source-cleanup outcome. Encoding never writes
over the source file.

## Operation journal dialog

| Control | Purpose |
| --- | --- |
| **Refresh** | Reloads recent operations from SQLite. |
| **Restore quarantine** | Restores one completed reversible quarantine operation after identity and destination checks. |
| **Open quarantine folder** | Opens the configured quarantine root. |
| **Close** | Closes the journal window. |

Restore is disabled for keep/ignore decisions, failed/skipped actions, OS
Recycle operations, and quarantined sources whose original path is occupied.
