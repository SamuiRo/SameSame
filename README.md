# SameSame

SameSame `1.6.4` finds duplicate and similar media files through a command-line
scanner or an optional desktop review interface. It scans one or more folder
trees recursively and writes reports for manual review.

The CLI remains report-only. The desktop interface can record keep/ignore
decisions, move explicitly selected content-backed files to reversible
quarantine, or—only after enabling an unsafe opt-in—send an explicitly confirmed
file to the operating-system recycle bin. The CLI and desktop UI can transcode videos with four anime-oriented
FFmpeg presets. Encoding always keeps the source; a separate explicit GUI action
can identity-check and quarantine it before promoting the validated MKV.

## What SameSame Finds

- exact byte-identical files, confirmed by full hashes;
- similar videos across container, resolution, bitrate, and ordinary cut
  differences;
- similar images across resize, recompression, and common format changes;
- similar audio across codec, bitrate, container, and moderate volume changes;
- related folder trees based on their canonical media clusters;
- lower-confidence name hints for files that deserve manual inspection.

Exact, video, image, and audio results are content-backed. Name-only results
are hints and must not be treated as deletion evidence.

## Requirements

- Python 3.11 or newer;
- `ffmpeg` and `ffprobe` for video and audio matching;
- optional Anthropic API access or LM Studio for title normalization.

Pillow and the other Python dependencies are installed with SameSame.

## Install

From the project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Verify the commands:

```powershell
samesame --help
samesame-transcode --help
ffmpeg -version
ffprobe -version
```

Install the optional desktop interface and launch it:

```powershell
python -m pip install -e ".[gui]"
samesame-gui
```

The GUI uses PySide6 Widgets and Qt Multimedia. CLI-only installations do not
install PySide6.

If FFmpeg is missing on Windows, install a build that provides both commands,
then restart the terminal. For example:

```powershell
winget install Gyan.FFmpeg
```

If PowerShell blocks virtual-environment activation, the executable can be
called directly:

```powershell
.\.venv\Scripts\samesame.exe --help
```

## First Scan

The simplest private/local scan uses built-in name heuristics and does not call
an AI service:

```powershell
samesame --folders "D:\Anime" --name-provider none
```

To compare several collection roots:

```powershell
samesame --folders "D:\Anime\Collection A" "E:\Anime\Collection B" --name-provider none
```

One root finds duplicates anywhere below that root. Several roots additionally
produce folder-pair similarity. Every supplied root is scanned recursively.

By default the command writes these files in the current directory:

- `report.html`: expandable report intended for manual review;
- `report.json`: structured report for scripts and other tools;
- `.dedupe_cache.sqlite3`: reusable fingerprint cache.

The first media scan can take time because hashes and fingerprints must be
created. Later runs reuse the cache when files have not changed.

## Recommended Config Workflow

Copy the example instead of typing every option repeatedly:

```powershell
Copy-Item docs\samesame.example.json samesame.json
```

Edit at least the `folders` list, then run:

```powershell
samesame --config samesame.json
```

The example writes its reports under `reports/` and its cache under `.cache/`.
Config values can be overridden for one run:

```powershell
samesame --config samesame.json --video-threshold 88 --log-level DEBUG
```

Configuration precedence is: built-in defaults, config file, then CLI flags.
See [Configuration](docs/CONFIG.md) for every key.

## If FFmpeg Is Not in PATH

Pass the executable paths explicitly:

```powershell
samesame --folders "D:\Anime" `
  --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe" `
  --ffprobe "C:\ffmpeg\bin\ffprobe.exe" `
  --name-provider none
```

Without FFmpeg, exact hashes, image matching, and name hints can still run:

```powershell
samesame --folders "D:\Media" --skip-video --skip-audio --name-provider none
```

## Reviewing Results Safely

Use the report sections in this order:

1. **Exact duplicates** have identical full hashes and are the strongest
   candidates for manual cleanup.
2. **Video, image, and audio matches** are content-backed, but should still be
   compared before changing files because alternate editions may contain
   different tracks, subtitles, credits, or metadata.
3. **Folder matches** summarize overlap between supplied collection roots.
4. **Name hints** are review leads only. Similar names or episode numbers do
   not prove identical content.

SameSame deliberately rejects an individual episode as a content duplicate of
a much longer compilation. Segment matching inside compilations is not part of
the current release.

Desktop file actions are deliberately conservative:

- quarantine is the preferred default and can be restored from the operation journal;
- duplicate review includes a per-result checklist for quarantining or recycling
  several explicitly checked content-backed files in one confirmed batch;
- every quarantine/recycle action rechecks path, size, modification time, and
  a full SHA-256 identity before acting;
- exact groups can quarantine all copies except the explicitly selected keeper;
- exact-group batch cleanup additionally SHA-256-compares every candidate with
  the selected keeper immediately before moving it;
- video, image, and audio matches require an individual confirmation;
- folder and name hints allow keep/ignore decisions but never enable file mutation;
- OS recycle is blocked by default because Windows settings can turn it into
  permanent deletion; Recycle buttons offer SameSame Quarantine as the safe fallback;
- every requested, completed, and failed operation is stored in a SQLite journal.

## Video Compression in the GUI

SameSame `1.6.1` has a dedicated **Video compression** tab for folder-based
batch work:

1. Choose a folder and load its videos recursively at any directory depth,
   including linked directory trees without following cycles.
2. Check individual files, or select matching files by extension, minimum or
   maximum size, and minimum or maximum duration.
3. Choose one of the four anime presets, or define custom libx265, AV1 NVENC,
   or HEVC NVENC quality, speed, pixel-format, and encoder options.
4. Choose an optional output folder and open the sequential compression queue.

The cleanup option moves originals to reversible SameSame Quarantine by default,
and only after the encoded MKV passes validation and the source matches its
recorded size, modification time, and SHA-256 identity. OS Recycle requires the
separate red unsafe opt-in in Duplicate review because Windows may permanently
delete when its Recycle Bin is disabled or unavailable.

Reviewed duplicate results can still be sent directly to the same queue with
**Transcode videos…** in the comparison panel.

The queue checks encoder and GPU availability before starting and provides
per-file progress, cancellation, retry, output/log shortcuts, and a before/after
metadata and size comparison. A completed encode keeps the source unchanged.

**Quarantine original + promote…** is a separate confirmed action. SameSame
verifies both files with SHA-256, moves the source to journaled quarantine, and
only then promotes the validated MKV into the collection. Destination conflicts
are rejected before the source is moved, and a failed promotion attempts to
restore the original.

## Common Commands

Inspect the four transcode presets and verify which encoders can initialize:

```powershell
samesame-transcode --list-presets
samesame-transcode --check-capabilities
```

Transcode one or more videos sequentially. Output is always a new MKV and the
source is always kept:

```powershell
samesame-transcode "D:\Anime\Episode 01.mkv" --preset anime_x265_balanced
samesame-transcode "D:\Anime\01.mkv" "D:\Anime\02.mkv" `
  --preset anime_hevc_nvenc --output-dir "D:\Anime\Encoded"
```

Use `--dry-run` to probe files and inspect the exact FFmpeg command first.
Audio, subtitles, chapters, metadata, and attachments are copied where present;
the encoded temporary file is probed and decode-checked before it receives its
final name. Diagnostic FFmpeg logs are retained beside the output.

Refresh one cache layer after changing matching logic or troubleshooting stale
results:

```powershell
samesame --config samesame.json --refresh-hashes
samesame --config samesame.json --refresh-video
samesame --config samesame.json --refresh-images
samesame --config samesame.json --refresh-audio
samesame --config samesame.json --refresh-names
```

Inspect cache statistics without scanning:

```powershell
samesame --inspect-cache --cache .cache\samesame.sqlite3
```

Use Anthropic title normalization:

```powershell
$env:ANTHROPIC_API_KEY = "your-key"
samesame --config samesame.json --name-provider anthropic
```

Use a running LM Studio server:

```powershell
samesame --config samesame.json `
  --name-provider lmstudio `
  --lmstudio-url http://localhost:1234/v1 `
  --lmstudio-model local-model
```

Measure matching thresholds with a labeled manifest:

```powershell
samesame-benchmark `
  --manifest docs\threshold-manifest.example.json `
  --ffmpeg ffmpeg `
  --ffprobe ffprobe `
  --output threshold-results.json
```

## Current and Planned Interfaces

Available now:

- `samesame`: duplicate scanner and HTML/JSON report generator;
- `samesame-benchmark`: threshold benchmark utility.
- `samesame-gui`: desktop review with journaled file actions and a cancellable
  transcoding queue;
- `samesame-transcode`: independent sequential transcoding queue with four
  validated anime presets;
- `dedupe.service.ScanService`: UI-agnostic Python scan API with structured
  progress events, cooperative cancellation, and review metadata models.

The original GUI/transcoding roadmap is complete in `1.6.0`; deferred matching
and advanced encoding ideas remain listed in the roadmap.

## Application Service

Desktop clients and scripts can run the scanner without terminal output:

```python
from pathlib import Path

from dedupe.events import CancellationToken
from dedupe.service import ScanOptions, ScanService

token = CancellationToken()
result = ScanService().run(
    ScanOptions(folders=[Path("D:/Media")], name_provider="none"),
    on_event=lambda event: print(event.event_type, event.stage, event.current, event.total),
    cancellation=token,
)
```

`ScanResult` contains the existing report, scanned file records, and basic
metadata. `dedupe.metadata.probe_media_metadata()` loads detailed stream,
codec, resolution, track, chapter, and attachment information lazily for a
selected review file.

The implementation order, safety requirements, and complexity estimates are in
the [Development roadmap](docs/ROADMAP.md). The exact initial encoding presets
are specified in [Anime encoding presets](docs/ANIME_ENCODING_PRESETS.md).

## Documentation

- [Usage guide](docs/USAGE.md)
- [Configuration reference](docs/CONFIG.md)
- [Threshold benchmarking](docs/THRESHOLDS.md)
- [Development roadmap](docs/ROADMAP.md)
- [Anime encoding preset specification](docs/ANIME_ENCODING_PRESETS.md)
- [Example config](docs/samesame.example.json)
- [Project status and handoff](docs/STATUS.md)
- [Project audit](docs/AUDIT.md)
