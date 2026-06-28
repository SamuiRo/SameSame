# Usage Guide

This guide describes SameSame `1.6.4`. The current application provides a
report-only CLI and an optional desktop review interface with explicit,
journaled quarantine/recycle actions and a transcoding queue. There is no
permanent-delete command.

SameSame finds duplicate media on six levels:

1. Exact byte duplicates, confirmed by full file hash.
2. Similar videos, confirmed by sampled video frame fingerprints.
3. Similar images, confirmed by perceptual image and color fingerprints.
4. Similar audio, confirmed by ffmpeg Chromaprint fingerprints.
5. Folder overlap, computed from canonical file clusters.
6. Name-only hints, useful for review but not safe deletion evidence.

## Install

Create a virtual environment and install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

After installation you can use the console command:

```powershell
samesame --help
```

Install and launch the optional desktop interface:

```powershell
pip install -e ".[gui]"
samesame-gui
```

The desktop interface supports multiple collection roots, background scans,
progress and cancellation, filters for every report category, side-by-side
image/video/audio review, synchronized video seeking, detailed stream metadata,
and HTML/JSON report export. Source media is modified only after an explicit,
confirmed quarantine action or an explicitly enabled unsafe recycle action.

You can also run without installing:

```powershell
python dedupe.py --help
```

## ffmpeg

Video and audio fingerprinting require `ffmpeg` and `ffprobe`.

On Windows:

```powershell
winget install Gyan.FFmpeg
```

Restart the terminal, then verify:

```powershell
ffmpeg -version
ffprobe -version
```

If ffmpeg is not available yet, SameSame can still do exact hashes, perceptual
image matching, and name analysis:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --skip-video --skip-audio
```

Image fingerprinting does not require ffmpeg and remains enabled with
`--skip-video`.

## Quick Start

Scan one folder recursively:

```powershell
samesame --folders "D:\Media" --name-provider none
```

Scan and compare several folder trees:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider none --output report.html --json-output report.json
```

All supported files below each input root are scanned recursively. Duplicate
files are searched both inside each root and across different roots. Folder-pair
scores are produced only when at least two roots are supplied.

Overlapping roots such as both `D:\Media` and `D:\Media\Photos` are supported.
Each resolved file path is scanned only once and is assigned to the first input
root through which it was discovered.

The output contains:

- `report.html`: expandable human review report.
- `report.json`: structured automation-friendly report.
- `.dedupe_cache.sqlite3`: reusable cache for hashes, video/image/audio fingerprints, and names.

Relative output paths are written to the current working directory. Use
`--output`, `--json-output`, and `--cache` to choose explicit locations.

SameSame never moves, merges, renames, or deletes media. A content cluster is
only a logical group in the HTML/JSON report. Every report entry keeps the
original full paths so the files remain easy to locate.

For repeated scans, copy and edit the example config:

```powershell
Copy-Item docs\samesame.example.json samesame.json
samesame --config samesame.json
```

At minimum, replace the example `folders` values. The example uses local name
heuristics and therefore works without Anthropic or LM Studio.

## Supported Media

The default scan includes:

- video: `.mkv`, `.mp4`, `.avi`, `.mov`, `.ts`, `.m2ts`, `.wmv`, `.flv`,
  `.webm`, `.mpg`, `.mpeg`, `.m4v`;
- images: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.gif`, `.tif`, `.tiff`.
- audio: `.mp3`, `.flac`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.wma`,
  `.aiff`, `.aif`.

`--extensions` replaces the default extension set; it does not append to it.
List every format needed for that run. Extensions outside the built-in
video/image/audio sets are scanned for exact hashes and name hints, but they do
not automatically gain perceptual fingerprinting.

Inspect cache state:

```powershell
samesame --inspect-cache --cache .dedupe_cache.sqlite3
```

Refresh one cache layer without deleting the database:

```powershell
samesame --config samesame.json --refresh-hashes
samesame --config samesame.json --refresh-video
samesame --config samesame.json --refresh-images
samesame --config samesame.json --refresh-audio
samesame --config samesame.json --refresh-names
```

## Anthropic Title Normalization

```powershell
$env:ANTHROPIC_API_KEY = "your-key"
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider anthropic
```

`auto` mode uses Anthropic when `ANTHROPIC_API_KEY` is set:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider auto
```

## LM Studio Title Normalization

1. Open LM Studio.
2. Load a chat/instruct model.
3. Start the local OpenAI-compatible server.
4. Run:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider lmstudio --lmstudio-url http://localhost:1234/v1 --lmstudio-model local-model
```

Set defaults for repeated runs:

```powershell
$env:LMSTUDIO_URL = "http://localhost:1234/v1"
$env:LMSTUDIO_MODEL = "qwen2.5-7b-instruct"
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider lmstudio
```

## Local Heuristic Names Only

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider none
```

This avoids any external or local model calls.

## Interpreting Results

Exact duplicate groups are safe candidates for manual deletion review because
they have identical full hashes.

Similar video matches are strong candidates for manual review. They may differ
by container, bitrate, resolution, or encoding settings. When durations differ
SameSame aligns 15 sampled frames in sequence and checks start/end timing to
tolerate ordinary alternate cuts, added credits, or removed intros. A duration
ratio/delta gate rejects a single episode inside a much longer compilation;
that relationship may still appear as a lower-confidence name hint.

Similar image matches can survive resizing, JPEG recompression, and conversion
between supported formats. They remain review candidates: heavy crops, arbitrary
rotation, overlays, or major edits may not match reliably. Animated GIFs and
multi-page TIFF files are currently represented by their first decoded frame/page.

Similar audio matches use Chromaprint and can survive codec, bitrate, container,
and moderate volume changes. They remain manual review candidates; substantial
remixes, speed/pitch changes, or long added sections may not match.

Exact copies and perceptually similar variants can belong to one transitive
content cluster. For example, an exact PNG copy and a resized JPEG derived from
the same PNG are grouped together in folder comparison. The files themselves
remain in their original locations.

Folder pairs show two scores. `content_similarity` counts only exact/video/image/audio-backed
clusters as shared, while all canonical items remain in the union so unmatched
files lower the score. `name_assisted_similarity` also allows name-based clusters
to count as shared. Both are Jaccard similarity: intersection divided by union.

Name hints are intentionally low confidence. They are useful for sorting and
review, but should not be treated as deletion proof unless confirmed by exact
hashes, video fingerprints, image fingerprints, or audio fingerprints.

## Safe Desktop Actions

The desktop interface supports keep and ignore review states. Content-backed
exact, video, image, and audio results can also quarantine or recycle one
explicitly selected file. Exact groups additionally support a confirmed batch
quarantine that preserves the selected keeper.

The comparison panel also provides a checklist of every file in the current
content-backed result. Use **Check all except current** or check files manually,
then choose **Quarantine checked** or **Recycle checked**. Every checked file is
individually identity-verified and journaled. Selecting every file produces an
additional warning because no copy from that review result will remain in place.

Safe mode is the default. In Safe mode, **Recycle** offers to use SameSame
Quarantine instead. The red **Allow OS Recycle Bin** setting is required before
calling the operating-system recycle integration. Windows Recycle Bin properties
can specify immediate deletion, so enabling this setting may be irreversible.

Before every mutation, SameSame verifies the scan path, size, modification
time, and a newly calculated SHA-256 identity, then verifies quarantined output
again. Quarantine preserves the collection-relative path, handles name
collisions, and can be restored from the operation journal. Unsafe recycle uses
the operating-system recycle integration; SameSame cannot verify or restore its
result. Folder and name-only hints never enable file mutation. Permanent deletion
is not implemented.

## Independent Transcoding

The desktop interface includes a dedicated **Video compression** tab. Choose a
folder to probe its supported videos recursively at any directory depth, then check individual files
or use the extension, size, and duration filters to select a batch. The tab
offers the four anime presets from `ANIME_ENCODING_PRESETS.md` plus custom
libx265, AV1 NVENC, and HEVC NVENC settings. An optional output directory can
be selected before opening the queue.

The cleanup checkbox uses reversible SameSame Quarantine by default. SameSame
first validates the encoded output, then verifies the original against its
recorded size, modification time, and SHA-256 identity before cleanup. It uses
OS Recycle only when the separate unsafe setting is enabled.

Alternatively, select a review result containing videos and choose
**Transcode videos**. The shared queue dialog provides preset selection, encoder/GPU
capability status, progress, cancellation, retry, output/log shortcuts, and a
before/after stream and size summary. Multiple reviewed files run sequentially.

Completing an encode keeps both files. **Quarantine original + promote** is a
separate confirmation: SameSame rechecks the source identity, moves it to the
journaled quarantine, then moves the validated MKV into the collection and
verifies its content identity. Existing destination conflicts fail before the
source is moved; a failed promotion attempts to restore the original.

List presets and test both encoder availability and GPU initialization:

```powershell
samesame-transcode --list-presets
samesame-transcode --check-capabilities
```

Run a CPU encode or queue several inputs sequentially:

```powershell
samesame-transcode "D:\Anime\Episode 01.mkv" --preset anime_x265_balanced
samesame-transcode "D:\Anime\01.mkv" "D:\Anime\02.mkv" `
  --preset anime_x265_max --output-dir "D:\Anime\Encoded"
```

The transcoder probes each input, writes a unique temporary MKV, encodes only
video, and copies audio, subtitles, chapters, metadata, and attachments. It
then probes and decodes the result before assigning the final name. The source
is always retained. Use `--dry-run` to inspect the planned command,
`--json-output` for automation, and `--keep-temporary-on-failure` for debugging.
The exact settings are in `docs/ANIME_ENCODING_PRESETS.md`.
