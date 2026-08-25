from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import Cache
from .models import DedupeReport
from .name_normalizer import LMSTUDIO_MODEL, LMSTUDIO_URL
from .report import write_html_report, write_json_report
from .scanner import normalize_extensions
from .service import ScanService
from .video_fingerprint import DEFAULT_MAX_DURATION_DELTA, DEFAULT_MAX_DURATION_RATIO

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG: dict[str, Any] = {
    "folders": None,
    "output": "report.html",
    "json_output": "report.json",
    "cache": ".dedupe_cache.sqlite3",
    "extensions": None,
    # Calibrated scale (F-24): 0 = indistinguishable from unrelated content,
    # 100 = identical. video_threshold is evidence-derived from a full scan of
    # a real 276-video library (A-02), image_threshold from 132 real images
    # against 720 transformed duplicates (A-06), and audio_threshold from the
    # real audio tracks of confirmed duplicate videos (A-14). All three are
    # now measured rather than carried over from the pre-F-24 scale.
    "video_threshold": 20.0,
    "image_threshold": 60.0,
    "audio_threshold": 60.0,
    "folder_threshold": 50.0,
    "name_threshold": 92.0,
    "name_provider": "auto",
    "lmstudio_url": None,
    "lmstudio_model": None,
    "workers": 4,
    "skip_video": False,
    "skip_images": False,
    "skip_audio": False,
    "refresh_hashes": False,
    "refresh_video": False,
    "refresh_images": False,
    "refresh_audio": False,
    "refresh_names": False,
    # Tile size for the batched image comparison, not a selectivity filter
    # (A-18): every image is compared against every other, in tiles this
    # large per side, so memory stays bounded on large libraries.
    "max_image_candidates": 4000,
    # "full" detects rotated/mirrored duplicates (A-15); "canonical" skips
    # that to fingerprint and compare faster - see IMAGE_ORIENTATION_MODES.
    "image_orientations": "full",
    "max_video_duration_ratio": DEFAULT_MAX_DURATION_RATIO,
    "max_video_duration_delta": DEFAULT_MAX_DURATION_DELTA,
    "inspect_cache": False,
    "vacuum_cache": False,
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "log_level": "INFO",
}


@dataclass(slots=True)
class Config:
    folders: list[Path]
    output: Path
    json_output: Path
    cache: Path
    extensions: set[str]
    video_threshold: float
    image_threshold: float
    audio_threshold: float
    folder_threshold: float
    name_threshold: float
    name_provider: str
    lmstudio_url: str
    lmstudio_model: str
    workers: int
    skip_video: bool
    skip_images: bool
    skip_audio: bool
    refresh_hashes: bool
    refresh_video: bool
    refresh_images: bool
    refresh_audio: bool
    refresh_names: bool
    max_image_candidates: int
    image_orientations: str
    max_video_duration_ratio: float
    max_video_duration_delta: float
    inspect_cache: bool
    vacuum_cache: bool
    ffmpeg: str
    ffprobe: str
    log_level: str


def _load_config_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("YAML config requires PyYAML. Install it or use JSON config.") from exc
        return dict(yaml.safe_load(raw) or {})
    return dict(json.loads(raw))


def _canonical_key(key: str) -> str:
    return key.replace("-", "_")


def _flatten_config(config: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def assign(key: str, value: Any, source: str) -> None:
        # Several settings have two accepted spellings - `matching.audio` and
        # `audio.threshold` both mean `audio_threshold`. A config that set both
        # used to be resolved by document order, silently taking whichever came
        # last: that is how docs/CONFIG.md's own example shipped an audio
        # threshold of 88 while the `matching` block above it said 60, which is
        # the value A-14 measured as missing 16 of 20 confirmed duplicates
        # (A-21). Agreeing duplicates are harmless and still allowed; a
        # disagreement is a mistake the user wants to hear about, not one to
        # guess at.
        previous = sources.get(key)
        if previous is not None and flattened[key] != value:
            raise SystemExit(
                f"Config sets {key} twice with different values: "
                f"{previous} = {flattened[key]!r} and {source} = {value!r}. Remove one of them."
            )
        flattened[key] = value
        sources[key] = source

    for key, value in config.items():
        canonical = _canonical_key(key)
        if isinstance(value, dict):
            if canonical == "reports":
                if "html" in value:
                    assign("output", value["html"], f"{key}.html")
                if "json" in value:
                    assign("json_output", value["json"], f"{key}.json")
            elif canonical == "names":
                for nested_key, nested_value in value.items():
                    assign(_canonical_key(nested_key), nested_value, f"{key}.{nested_key}")
            elif canonical == "video":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    source = f"{key}.{nested_key}"
                    if nested == "skip":
                        assign("skip_video", nested_value, source)
                    elif nested in {"duration_ratio", "max_duration_ratio"}:
                        assign("max_video_duration_ratio", nested_value, source)
                    elif nested in {"duration_delta", "max_duration_delta"}:
                        assign("max_video_duration_delta", nested_value, source)
                    else:
                        assign(nested, nested_value, source)
            elif canonical == "images":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    source = f"{key}.{nested_key}"
                    if nested == "skip":
                        assign("skip_images", nested_value, source)
                    elif nested == "threshold":
                        assign("image_threshold", nested_value, source)
                    elif nested in {"max_candidates", "max_image_candidates"}:
                        assign("max_image_candidates", nested_value, source)
                    elif nested in {"orientations", "image_orientations"}:
                        assign("image_orientations", nested_value, source)
                    else:
                        assign(f"images_{nested}", nested_value, source)
            elif canonical == "audio":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    source = f"{key}.{nested_key}"
                    if nested == "skip":
                        assign("skip_audio", nested_value, source)
                    elif nested == "threshold":
                        assign("audio_threshold", nested_value, source)
                    else:
                        assign(f"audio_{nested}", nested_value, source)
            elif canonical == "matching":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    source = f"{key}.{nested_key}"
                    if nested in {"video", "video_similarity"}:
                        assign("video_threshold", nested_value, source)
                    elif nested in {"image", "images", "image_similarity"}:
                        assign("image_threshold", nested_value, source)
                    elif nested in {"audio", "audio_similarity"}:
                        assign("audio_threshold", nested_value, source)
                    elif nested in {"folder", "folder_similarity"}:
                        assign("folder_threshold", nested_value, source)
                    elif nested in {"name", "name_similarity"}:
                        assign("name_threshold", nested_value, source)
                    else:
                        assign(nested, nested_value, source)
            elif canonical == "cache" and "path" in value:
                assign("cache", value["path"], f"{key}.path")
            else:
                for nested_key, nested_value in value.items():
                    assign(f"{canonical}_{_canonical_key(nested_key)}", nested_value, f"{key}.{nested_key}")
        else:
            assign(canonical, value, key)
    # Deliberately not routed through `assign`: this is a derived override of
    # an explicit setting, not a second spelling of it.
    if flattened.get("ai_names") is False:
        flattened["name_provider"] = "none"
    return flattened


def _load_cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key, value in vars(args).items():
        if key == "config" or value is None:
            continue
        if key == "no_ai_names":
            if value:
                overrides["name_provider"] = "none"
            continue
        overrides[key] = value
    return overrides


def _merge_config(file_config: dict[str, Any], cli_overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(_flatten_config(file_config))
    merged.update(cli_overrides)
    if merged.get("lmstudio_url") is None:
        merged["lmstudio_url"] = os.environ.get("LMSTUDIO_URL", LMSTUDIO_URL)
    if merged.get("lmstudio_model") is None:
        merged["lmstudio_model"] = os.environ.get("LMSTUDIO_MODEL", LMSTUDIO_MODEL)
    return merged


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Find duplicate and near-duplicate media files across folders.")
    parser.add_argument("--folders", nargs="+", help="Folders to scan.")
    parser.add_argument("--config", type=Path, help="JSON/YAML config file.")
    parser.add_argument("--output", type=Path, default=None, help="HTML report path.")
    parser.add_argument("--json-output", type=Path, default=None, help="JSON report path.")
    parser.add_argument("--cache", type=Path, default=None, help="SQLite cache path.")
    parser.add_argument("--extensions", nargs="*", help="File extensions to include.")
    parser.add_argument(
        "--video-threshold",
        type=float,
        default=None,
        help="Video fingerprint match confidence, percent (0 = indistinguishable from unrelated, 100 = identical).",
    )
    parser.add_argument(
        "--image-threshold",
        type=float,
        default=None,
        help="Image fingerprint match confidence, percent (0 = indistinguishable from unrelated, 100 = identical).",
    )
    parser.add_argument(
        "--audio-threshold",
        type=float,
        default=None,
        help="Audio fingerprint match confidence, percent (0 = indistinguishable from unrelated, 100 = identical).",
    )
    parser.add_argument("--folder-threshold", type=float, default=None, help="Folder Jaccard threshold, percent.")
    parser.add_argument("--name-threshold", type=float, default=None, help="Fuzzy title hint threshold, percent.")
    parser.add_argument(
        "--name-provider",
        default=None,
        choices=["auto", "anthropic", "lmstudio", "none"],
        help="Provider for AI title normalization. 'none' uses local heuristics only.",
    )
    parser.add_argument(
        "--lmstudio-url",
        default=None,
        help="LM Studio OpenAI-compatible base URL. Defaults to LMSTUDIO_URL or http://localhost:1234/v1.",
    )
    parser.add_argument(
        "--lmstudio-model",
        default=None,
        help="LM Studio model name. Defaults to LMSTUDIO_MODEL or local-model.",
    )
    parser.add_argument("--workers", type=int, default=None, help="Worker threads for IO-heavy steps.")
    parser.add_argument("--no-ai-names", action="store_true", help="Use local heuristic title normalization only.")
    parser.add_argument(
        "--skip-video",
        dest="skip_video",
        action="store_true",
        default=None,
        help="Skip ffmpeg/ffprobe video fingerprinting.",
    )
    parser.add_argument(
        "--no-skip-video", dest="skip_video", action="store_false", help="Enable ffmpeg/ffprobe video fingerprinting."
    )
    parser.add_argument(
        "--skip-images",
        dest="skip_images",
        action="store_true",
        default=None,
        help="Skip perceptual image fingerprinting.",
    )
    parser.add_argument(
        "--no-skip-images",
        dest="skip_images",
        action="store_false",
        help="Enable perceptual image fingerprinting.",
    )
    parser.add_argument(
        "--skip-audio",
        dest="skip_audio",
        action="store_true",
        default=None,
        help="Skip Chromaprint audio fingerprinting.",
    )
    parser.add_argument(
        "--no-skip-audio",
        dest="skip_audio",
        action="store_false",
        help="Enable Chromaprint audio fingerprinting.",
    )
    parser.add_argument(
        "--refresh-hashes", action="store_true", default=None, help="Recompute partial and full file hashes."
    )
    parser.add_argument(
        "--refresh-video", action="store_true", default=None, help="Recompute cached video durations and fingerprints."
    )
    parser.add_argument(
        "--refresh-images",
        action="store_true",
        default=None,
        help="Recompute cached image fingerprints.",
    )
    parser.add_argument(
        "--refresh-audio",
        action="store_true",
        default=None,
        help="Recompute cached audio durations and fingerprints.",
    )
    parser.add_argument(
        "--refresh-names", action="store_true", default=None, help="Recompute cached title normalization results."
    )
    parser.add_argument(
        "--max-image-candidates",
        type=int,
        default=None,
        help="Tile size (per side) for the batched image comparison; does not filter which pairs are compared.",
    )
    parser.add_argument(
        "--image-orientations",
        choices=["full", "canonical"],
        default=None,
        help=(
            "'full' (default) detects rotated/mirrored image duplicates. 'canonical' skips that to "
            "fingerprint and compare faster. Switching modes needs --refresh-images to apply to "
            "already-cached files."
        ),
    )
    parser.add_argument(
        "--max-video-duration-ratio",
        type=float,
        default=None,
        help=(
            "Reject a video candidate pair before content comparison when the longer file is "
            f"more than this many times the shorter file's duration. Default {DEFAULT_MAX_DURATION_RATIO}."
        ),
    )
    parser.add_argument(
        "--max-video-duration-delta",
        type=float,
        default=None,
        help=(
            "Reject a video candidate pair before content comparison when durations differ by more "
            f"than this many seconds. Default {DEFAULT_MAX_DURATION_DELTA:.0f}."
        ),
    )
    parser.add_argument(
        "--inspect-cache", action="store_true", default=None, help="Print cache statistics as JSON and exit."
    )
    parser.add_argument(
        "--vacuum-cache",
        action="store_true",
        default=None,
        help="Remove cache rows for files that no longer exist on disk, VACUUM, and exit.",
    )
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg executable path/name.")
    parser.add_argument("--ffprobe", default=None, help="ffprobe executable path/name.")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    file_config: dict[str, Any] = _load_config_file(args.config) if args.config else {}
    merged = _merge_config(file_config, _load_cli_overrides(args))
    folders = merged.get("folders")
    if not folders and not merged.get("inspect_cache") and not merged.get("vacuum_cache"):
        raise SystemExit("At least one folder is required. Use --folders or a config file.")

    thresholds: dict[str, float] = {}
    for key in ("video_threshold", "image_threshold", "audio_threshold", "folder_threshold", "name_threshold"):
        try:
            value = float(merged[key])
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{key} must be a number between 0 and 100") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 100.0:
            raise SystemExit(f"{key} must be a finite number between 0 and 100")
        thresholds[key] = value

    try:
        workers = int(merged["workers"])
    except (TypeError, ValueError) as exc:
        raise SystemExit("workers must be an integer between 1 and 64") from exc
    if not 1 <= workers <= 64:
        raise SystemExit("workers must be between 1 and 64")
    if merged["name_provider"] not in {"auto", "anthropic", "lmstudio", "none"}:
        raise SystemExit("name_provider must be one of: auto, anthropic, lmstudio, none")
    if merged["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise SystemExit("log_level must be one of: DEBUG, INFO, WARNING, ERROR")
    if merged["image_orientations"] not in {"full", "canonical"}:
        raise SystemExit("image_orientations must be one of: full, canonical")

    try:
        max_video_duration_ratio = float(merged["max_video_duration_ratio"])
    except (TypeError, ValueError) as exc:
        raise SystemExit("max_video_duration_ratio must be a finite number greater than 1") from exc
    if not math.isfinite(max_video_duration_ratio) or max_video_duration_ratio <= 1.0:
        raise SystemExit("max_video_duration_ratio must be a finite number greater than 1")

    try:
        max_video_duration_delta = float(merged["max_video_duration_delta"])
    except (TypeError, ValueError) as exc:
        raise SystemExit("max_video_duration_delta must be a finite number >= 0") from exc
    if not math.isfinite(max_video_duration_delta) or max_video_duration_delta < 0.0:
        raise SystemExit("max_video_duration_delta must be a finite number >= 0")

    return Config(
        folders=[Path(folder) for folder in folders or []],
        output=Path(str(merged["output"])),
        json_output=Path(str(merged["json_output"])),
        cache=Path(str(merged["cache"])),
        extensions=normalize_extensions(merged.get("extensions")),
        video_threshold=thresholds["video_threshold"],
        image_threshold=thresholds["image_threshold"],
        audio_threshold=thresholds["audio_threshold"],
        folder_threshold=thresholds["folder_threshold"],
        name_threshold=thresholds["name_threshold"],
        name_provider=str(merged["name_provider"]),
        lmstudio_url=str(merged["lmstudio_url"]),
        lmstudio_model=str(merged["lmstudio_model"]),
        workers=workers,
        skip_video=bool(merged["skip_video"]),
        skip_images=bool(merged["skip_images"]),
        skip_audio=bool(merged["skip_audio"]),
        refresh_hashes=bool(merged["refresh_hashes"]),
        refresh_video=bool(merged["refresh_video"]),
        refresh_images=bool(merged["refresh_images"]),
        refresh_audio=bool(merged["refresh_audio"]),
        refresh_names=bool(merged["refresh_names"]),
        max_image_candidates=max(2, int(merged["max_image_candidates"])),
        image_orientations=str(merged["image_orientations"]),
        max_video_duration_ratio=max_video_duration_ratio,
        max_video_duration_delta=max_video_duration_delta,
        inspect_cache=bool(merged["inspect_cache"]),
        vacuum_cache=bool(merged["vacuum_cache"]),
        ffmpeg=str(merged["ffmpeg"]),
        ffprobe=str(merged["ffprobe"]),
        log_level=str(merged["log_level"]),
    )


def run(config: Config) -> DedupeReport:
    return ScanService(show_terminal_progress=True).run(config).report


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.inspect_cache:
        with Cache(config.cache) as cache:
            print(json.dumps(cache.stats(), ensure_ascii=False, indent=2))
        return 0
    if config.vacuum_cache:
        with Cache(config.cache) as cache:
            removed = cache.vacuum()
        print(f"Removed {removed} cache row(s) for files that no longer exist and vacuumed {config.cache}.")
        return 0
    try:
        report = run(config)
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted by user. Cached progress is preserved.")
        return 130

    write_html_report(report, config.output)
    write_json_report(report, config.json_output)
    LOGGER.info("Wrote HTML report to %s", config.output)
    LOGGER.info("Wrote JSON report to %s", config.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
