# Configuration

SameSame reads settings from three places, in this order:

1. Built-in defaults.
2. A JSON or YAML config file passed with `--config`.
3. CLI flags.

CLI flags always win over the config file. This means you can keep a stable
project config and override only one value for a specific run.

This page documents the scanner configuration available in SameSame `1.9.0`.
Desktop UI state, quarantine location, review decisions, and operation history
are managed by the GUI and its journal rather than scanner config keys. Anime
transcoding settings are not scanner configuration keys; use the independent
`samesame-transcode` command documented in `docs/USAGE.md`.

When `extensions` is provided, it replaces the built-in video/image/audio extension
set rather than extending it.

## Minimal Config

```json
{
  "folders": ["D:/Anime/A", "D:/Anime/B"]
}
```

Run it:

```powershell
samesame --config samesame.json
```

or, without installing the console script:

```powershell
python dedupe.py --config samesame.json
```

## Full Flat Config

```json
{
  "folders": ["D:/Anime/A", "D:/Anime/B"],
  "output": "reports/samesame.html",
  "json_output": "reports/samesame.json",
  "cache": ".cache/samesame.sqlite3",
  "extensions": [".mkv", ".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mp3", ".flac"],
  "video_threshold": 20,
  "image_threshold": 80,
  "audio_threshold": 88,
  "folder_threshold": 50,
  "name_threshold": 92,
  "name_provider": "none",
  "lmstudio_url": "http://localhost:1234/v1",
  "lmstudio_model": "local-model",
  "workers": 4,
  "refresh_hashes": false,
  "refresh_video": false,
  "refresh_images": false,
  "refresh_audio": false,
  "refresh_names": false,
  "skip_video": false,
  "skip_images": false,
  "skip_audio": false,
  "max_image_candidates": 250,
  "max_video_duration_ratio": 1.5,
  "max_video_duration_delta": 900,
  "ffmpeg": "ffmpeg",
  "ffprobe": "ffprobe",
  "log_level": "INFO"
}
```

## Nested Config

For readability, these nested sections are also supported:

```json
{
  "folders": ["D:/Anime/A", "D:/Anime/B"],
  "reports": {
    "html": "reports/samesame.html",
    "json": "reports/samesame.json"
  },
  "cache": {
    "path": ".cache/samesame.sqlite3"
  },
  "matching": {
    "video": 20,
    "image": 80,
    "audio": 88,
    "folder": 50,
    "name": 92
  },
  "names": {
    "name_provider": "none",
    "lmstudio_url": "http://localhost:1234/v1",
    "lmstudio_model": "local-model"
  },
  "video": {
    "skip": false,
    "duration_ratio": 1.5,
    "duration_delta": 900,
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe"
  },
  "images": {
    "skip": false,
    "max_candidates": 250
  },
  "audio": {
    "skip": false,
    "threshold": 88
  },
  "extensions": [".mkv", ".mp4", ".avi", ".jpg", ".jpeg", ".png", ".webp", ".mp3", ".flac"],
  "workers": 4,
  "refresh_hashes": false,
  "refresh_video": false,
  "refresh_images": false,
  "refresh_audio": false,
  "refresh_names": false,
  "log_level": "INFO"
}
```

## Override Examples

Use the config, but write reports somewhere else:

```powershell
samesame --config samesame.json --output temp.html --json-output temp.json
```

Use LM Studio for this run only:

```powershell
samesame --config samesame.json --name-provider lmstudio --lmstudio-model local-model
```

Disable video fingerprinting even if the config enables it:

```powershell
samesame --config samesame.json --skip-video
```

Enable video fingerprinting even if the config disables it:

```powershell
samesame --config samesame.json --no-skip-video
```

Disable or re-enable perceptual image matching:

```powershell
samesame --config samesame.json --skip-images
samesame --config samesame.json --no-skip-images
```

Disable or re-enable perceptual audio matching:

```powershell
samesame --config samesame.json --skip-audio
samesame --config samesame.json --no-skip-audio
```

Use local heuristic name normalization only:

```powershell
samesame --config samesame.json --name-provider none
```

The legacy flag still works:

```powershell
samesame --config samesame.json --no-ai-names
```

Refresh one cache layer:

```powershell
samesame --config samesame.json --refresh-hashes
samesame --config samesame.json --refresh-video
samesame --config samesame.json --refresh-images
samesame --config samesame.json --refresh-audio
samesame --config samesame.json --refresh-names
```

Inspect cache metadata without scanning folders:

```powershell
samesame --inspect-cache --cache .dedupe_cache.sqlite3
```

## Name Providers

`name_provider` controls title normalization:

- `auto`: use Anthropic when `ANTHROPIC_API_KEY` exists, otherwise local heuristics.
- `anthropic`: use Claude via `ANTHROPIC_API_KEY`; falls back to local heuristics if the key is missing.
- `lmstudio`: use LM Studio's local OpenAI-compatible server.
- `none`: use local regex heuristics only.

For LM Studio, the defaults are:

```json
{
  "lmstudio_url": "http://localhost:1234/v1",
  "lmstudio_model": "local-model"
}
```

You can also set environment variables:

```powershell
$env:LMSTUDIO_URL = "http://localhost:1234/v1"
$env:LMSTUDIO_MODEL = "qwen2.5-7b-instruct"
```

## All Supported Keys

| Key | Default | Meaning |
| --- | --- | --- |
| `folders` | required | Folders to scan recursively. |
| `output` | `report.html` | Human-readable HTML report path. |
| `json_output` | `report.json` | Machine-readable JSON report path. |
| `cache` | `.dedupe_cache.sqlite3` | SQLite cache path. |
| `extensions` | common video, image, and audio extensions | File extensions to scan; a custom list replaces the defaults. |
| `video_threshold` | `20` | Minimum sequence-aligned video fingerprint match confidence; finite `0`–`100`. Calibrated scale: `0` = indistinguishable from unrelated content, `100` = identical (F-24) — not a naive "percent similar." See `docs/THRESHOLDS.md`. |
| `image_threshold` | `80` | Minimum perceptual image match confidence; finite `0`–`100`. Same calibrated scale as `video_threshold`. |
| `audio_threshold` | `88` | Minimum Chromaprint audio match confidence; finite `0`–`100`. Same calibrated scale as `video_threshold`. |
| `folder_threshold` | `50` | Minimum folder Jaccard similarity percent; finite `0`–`100`. Applies to both the directory-level comparison (default) and the collection-root roll-up (F-16). |
| `name_threshold` | `92` | Minimum fuzzy name hint similarity percent; finite `0`–`100`. |
| `name_provider` | `auto` | `auto`, `anthropic`, `lmstudio`, or `none`. |
| `lmstudio_url` | `http://localhost:1234/v1` | LM Studio API base URL. |
| `lmstudio_model` | `local-model` | Model name sent to LM Studio. |
| `workers` | `4` | Worker threads for IO-heavy operations; accepted range `1`–`64`. |
| `skip_video` | `false` | Skip ffmpeg/ffprobe video fingerprints. |
| `skip_images` | `false` | Skip perceptual image fingerprints. |
| `skip_audio` | `false` | Skip Chromaprint audio fingerprints. |
| `refresh_hashes` | `false` | Recompute partial and full hashes for this run. |
| `refresh_video` | `false` | Recompute durations and video fingerprints for this run. |
| `refresh_images` | `false` | Recompute image fingerprints for this run. |
| `refresh_audio` | `false` | Recompute audio durations and fingerprints for this run. |
| `refresh_names` | `false` | Recompute normalized names for this run. |
| `max_image_candidates` | `250` | Above this image count, use safe pHash blocking before pairwise comparison. |
| `max_video_duration_ratio` | `1.5` | Reject a video candidate pair before content comparison when the longer file is more than this many times the shorter file's duration; finite, `> 1`. |
| `max_video_duration_delta` | `900` | Reject a video candidate pair before content comparison when durations differ by more than this many seconds; finite, `>= 0`. |
| `ffmpeg` | `ffmpeg` | ffmpeg executable path or name. |
| `ffprobe` | `ffprobe` | ffprobe executable path or name. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
