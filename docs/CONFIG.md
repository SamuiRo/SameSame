# Configuration

SameSame reads settings from three places, in this order:

1. Built-in defaults.
2. A JSON or YAML config file passed with `--config`.
3. CLI flags.

CLI flags always win over the config file. This means you can keep a stable
project config and override only one value for a specific run.

When `extensions` is provided, it replaces the built-in video/image extension
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
  "extensions": [".mkv", ".mp4", ".jpg", ".jpeg", ".png", ".webp"],
  "video_threshold": 90,
  "image_threshold": 90,
  "folder_threshold": 50,
  "name_threshold": 92,
  "name_provider": "lmstudio",
  "lmstudio_url": "http://localhost:1234/v1",
  "lmstudio_model": "local-model",
  "workers": 4,
  "refresh_hashes": false,
  "refresh_video": false,
  "refresh_images": false,
  "refresh_names": false,
  "skip_video": false,
  "skip_images": false,
  "max_video_candidates_per_bucket": 250,
  "max_image_candidates": 250,
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
    "video": 90,
    "image": 90,
    "folder": 50,
    "name": 92
  },
  "names": {
    "name_provider": "lmstudio",
    "lmstudio_url": "http://localhost:1234/v1",
    "lmstudio_model": "local-model"
  },
  "video": {
    "skip": false,
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe"
  },
  "images": {
    "skip": false,
    "max_candidates": 250
  },
  "extensions": [".mkv", ".mp4", ".avi", ".jpg", ".jpeg", ".png", ".webp"],
  "workers": 4,
  "refresh_hashes": false,
  "refresh_video": false,
  "refresh_images": false,
  "refresh_names": false,
  "max_video_candidates_per_bucket": 250,
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
| `extensions` | common video and image extensions | File extensions to scan; a custom list replaces the defaults. |
| `video_threshold` | `90` | Minimum video fingerprint similarity percent. |
| `image_threshold` | `90` | Minimum perceptual image similarity percent. |
| `folder_threshold` | `50` | Minimum folder Jaccard similarity percent. |
| `name_threshold` | `92` | Minimum fuzzy name hint similarity percent. |
| `name_provider` | `auto` | `auto`, `anthropic`, `lmstudio`, or `none`. |
| `lmstudio_url` | `http://localhost:1234/v1` | LM Studio API base URL. |
| `lmstudio_model` | `local-model` | Model name sent to LM Studio. |
| `workers` | `4` | Worker threads for IO-heavy operations. |
| `skip_video` | `false` | Skip ffmpeg/ffprobe video fingerprints. |
| `skip_images` | `false` | Skip perceptual image fingerprints. |
| `refresh_hashes` | `false` | Recompute partial and full hashes for this run. |
| `refresh_video` | `false` | Recompute durations and video fingerprints for this run. |
| `refresh_images` | `false` | Recompute image fingerprints for this run. |
| `refresh_names` | `false` | Recompute normalized names for this run. |
| `max_video_candidates_per_bucket` | `250` | Above this duration-bucket size, use pHash blocking before pairwise video comparison. |
| `max_image_candidates` | `250` | Above this image count, use safe pHash blocking before pairwise comparison. |
| `ffmpeg` | `ffmpeg` | ffmpeg executable path or name. |
| `ffprobe` | `ffprobe` | ffprobe executable path or name. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
