# Usage Guide

This guide describes SameSame `1.5.2`. The current application provides a CLI
and an optional read-only desktop interface. There is no transcoding command or
file deletion workflow yet. Planned features are documented in
`docs/ROADMAP.md`.

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
and HTML/JSON report export. It does not modify source media.

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

## Desktop Review and Planned Actions

The desktop interface currently provides read-only side-by-side review. Safe
quarantine/recycle actions remain planned. A separate transcoding module will
implement the presets in `docs/ANIME_ENCODING_PRESETS.md`; those actions and
transcoding commands are not available in version 1.5.2. See
`docs/ROADMAP.md` for scope, order, safety requirements, and complexity
estimates.
