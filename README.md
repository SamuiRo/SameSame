# SameSame

SameSame is a Python CLI tool for finding duplicate media files across multiple
folders. It is built for large media collections where the same video, image,
or audio recording may exist under different filenames, encodes, sizes,
formats, or folder structures.

It detects:

- exact byte-identical duplicates by partial and full hashing;
- similar video content by ffmpeg frame fingerprints with start/end alignment
  for small duration changes;
- similar images after resize, recompression, or format conversion by perceptual fingerprints;
- similar audio across common encodes using ffmpeg Chromaprint fingerprints;
- overlapping folders by canonical cluster IDs;
- low-confidence name-only hints using Anthropic, LM Studio, or local heuristics.

## Requirements

- Python 3.11+
- Pillow for image fingerprinting (installed automatically)
- `ffmpeg` and `ffprobe` for video and audio fingerprinting
- Optional `ANTHROPIC_API_KEY` for Claude title normalization
- Optional LM Studio local server for private/local title normalization

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

After installation:

```powershell
samesame --help
```

You can also run directly from the repository:

```powershell
python dedupe.py --help
```

## ffmpeg On Windows

```powershell
winget install Gyan.FFmpeg
```

Restart the terminal, then verify:

```powershell
ffmpeg -version
ffprobe -version
```

If ffmpeg is not installed yet, run with `--skip-video --skip-audio`. Exact
hashing, similar image detection, name hints, and folder comparison remain enabled.

## Quick Start

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --output report.html --json-output report.json
```

Pass one folder or several folders. SameSame scans each folder recursively,
including all nested subfolders, and compares files both within and across them.
With one input folder, file-level duplicates are still found; folder-pair
comparison naturally requires at least two input roots.

SameSame is report-only: it does not move, merge, rename, or delete files.
Content clusters exist only in the generated reports.

Use a config file:

```powershell
samesame --config docs/samesame.example.json
```

Override any config value from CLI:

```powershell
samesame --config samesame.json --name-provider lmstudio --no-skip-video --video-threshold 94
```

Refresh only one cache layer when needed:

```powershell
samesame --config samesame.json --refresh-names
samesame --config samesame.json --refresh-hashes
samesame --config samesame.json --refresh-video
samesame --config samesame.json --refresh-images
samesame --config samesame.json --refresh-audio
```

Inspect the cache without scanning folders:

```powershell
samesame --inspect-cache --cache .dedupe_cache.sqlite3
```

Benchmark matching thresholds:

```powershell
samesame-benchmark --ffmpeg ffmpeg --ffprobe ffprobe --output threshold-benchmark.json
```

## Name Providers

Anthropic:

```powershell
$env:ANTHROPIC_API_KEY = "your-key"
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider anthropic
```

LM Studio:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider lmstudio --lmstudio-url http://localhost:1234/v1 --lmstudio-model local-model
```

Local heuristics only:

```powershell
samesame --folders "D:\Anime\A" "D:\Anime\B" --name-provider none
```

## Output

SameSame writes:

- `report.html`: human-readable report with expandable sections;
- `report.json`: machine-readable report for automation;
- `.dedupe_cache.sqlite3`: reusable cache for hashes, video/image/audio fingerprints, and names.

Name-only matches are intentionally not treated as safe deletion candidates.
They are hints unless confirmed by exact hashes, video fingerprints, image
fingerprints, or audio fingerprints.
Folder reports include both content-backed similarity and broader name-assisted
similarity.

## Documentation

- [Usage guide](docs/USAGE.md)
- [Configuration reference](docs/CONFIG.md)
- [Threshold benchmarking](docs/THRESHOLDS.md)
- [Example config](docs/samesame.example.json)
- [Project status and roadmap](docs/STATUS.md)
