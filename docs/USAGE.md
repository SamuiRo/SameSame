# Usage Guide

SameSame finds duplicate media on four levels:

1. Exact byte duplicates, confirmed by full file hash.
2. Similar videos, confirmed by sampled video frame fingerprints.
3. Folder overlap, computed from canonical file clusters.
4. Name-only hints, useful for review but not safe deletion evidence.

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

If ffmpeg is not available yet, SameSame can still do exact hashes and name
analysis:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --skip-video
```

## Quick Start

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --output report.html --json-output report.json
```

The output contains:

- `report.html`: expandable human review report.
- `report.json`: structured automation-friendly report.
- `.dedupe_cache.sqlite3`: reusable cache for hashes, durations, fingerprints, and names.

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
by container, bitrate, resolution, or encoding settings.

Folder pairs show how much two scanned roots overlap by canonical file cluster.
The percent is Jaccard similarity: intersection divided by union.

Name hints are intentionally low confidence. They are useful for sorting and
review, but should not be treated as deletion proof unless confirmed by exact
hashes or video fingerprints.

