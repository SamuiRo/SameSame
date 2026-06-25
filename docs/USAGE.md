# Usage Guide

SameSame finds duplicate media on five levels:

1. Exact byte duplicates, confirmed by full file hash.
2. Similar videos, confirmed by sampled video frame fingerprints.
3. Similar images, confirmed by perceptual image and color fingerprints.
4. Folder overlap, computed from canonical file clusters.
5. Name-only hints, useful for review but not safe deletion evidence.

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

You can also run without installing:

```powershell
python dedupe.py --help
```

## ffmpeg

Video fingerprinting requires `ffmpeg` and `ffprobe`.

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
samesame --folders "D:\Anime\A" "D:\Anime\B" --skip-video
```

Image fingerprinting does not require ffmpeg and remains enabled with
`--skip-video`.

## Quick Start

Scan one folder recursively:

```powershell
samesame --folders "D:\Media" --skip-video
```

Scan and compare several folder trees:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --output report.html --json-output report.json
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
- `.dedupe_cache.sqlite3`: reusable cache for hashes, video/image fingerprints, and names.

Relative output paths are written to the current working directory. Use
`--output`, `--json-output`, and `--cache` to choose explicit locations.

SameSame never moves, merges, renames, or deletes media. A content cluster is
only a logical group in the HTML/JSON report. Every report entry keeps the
original full paths so the files remain easy to locate.

## Supported Media

The default scan includes:

- video: `.mkv`, `.mp4`, `.avi`, `.mov`, `.ts`, `.m2ts`, `.wmv`, `.flv`,
  `.webm`, `.mpg`, `.mpeg`, `.m4v`;
- images: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.gif`, `.tif`, `.tiff`.

Audio is not included by default. It can currently be scanned for exact
byte-identical copies only:

```powershell
samesame --folders "D:\Music" --extensions .mp3 .flac .wav .m4a .aac --skip-video
```

`--extensions` replaces the default extension set; it does not append to it.
List every format needed for that run. Perceptual audio matching across
different encodes is not implemented yet. Extensions outside the built-in
video/image sets are scanned for exact hashes and name hints, but they do not
automatically gain video or image fingerprinting.

Inspect cache state:

```powershell
samesame --inspect-cache --cache .dedupe_cache.sqlite3
```

Refresh one cache layer without deleting the database:

```powershell
samesame --config samesame.json --refresh-hashes
samesame --config samesame.json --refresh-video
samesame --config samesame.json --refresh-images
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
slightly, SameSame also aligns sampled frames from the start and end to tolerate
short added or removed intros/outros.

Similar image matches can survive resizing, JPEG recompression, and conversion
between supported formats. They remain review candidates: heavy crops, arbitrary
rotation, overlays, or major edits may not match reliably. Animated GIFs and
multi-page TIFF files are currently represented by their first decoded frame/page.

Exact copies and perceptually similar variants can belong to one transitive
content cluster. For example, an exact PNG copy and a resized JPEG derived from
the same PNG are grouped together in folder comparison. The files themselves
remain in their original locations.

Folder pairs show two scores. `content_similarity` counts only exact/video/image-backed
clusters as shared, while all canonical items remain in the union so unmatched
files lower the score. `name_assisted_similarity` also allows name-based clusters
to count as shared. Both are Jaccard similarity: intersection divided by union.

Name hints are intentionally low confidence. They are useful for sorting and
review, but should not be treated as deletion proof unless confirmed by exact
hashes, video fingerprints, or image fingerprints.
