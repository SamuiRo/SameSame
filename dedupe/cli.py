from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_fingerprint import check_chromaprint, find_audio_matches
from .cache import Cache
from .exact_hash import find_exact_duplicates
from .folder_compare import build_cluster_assignments, compare_folders
from .image_fingerprint import PIL_AVAILABLE as IMAGE_PIL_AVAILABLE
from .image_fingerprint import find_image_matches
from .models import DedupeReport
from .name_normalizer import LMSTUDIO_MODEL, LMSTUDIO_URL, find_name_hints, normalize_names
from .report import write_html_report, write_json_report
from .scanner import is_audio_path, is_image_path, is_video_path, normalize_extensions, scan_folders
from .video_fingerprint import PIL_AVAILABLE as VIDEO_PIL_AVAILABLE
from .video_fingerprint import check_video_tools, find_video_matches

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG: dict[str, Any] = {
    "folders": None,
    "output": "report.html",
    "json_output": "report.json",
    "cache": ".dedupe_cache.sqlite3",
    "extensions": None,
    "video_threshold": 90.0,
    "image_threshold": 90.0,
    "audio_threshold": 90.0,
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
    "max_video_candidates_per_bucket": 250,
    "max_image_candidates": 250,
    "inspect_cache": False,
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
    max_video_candidates_per_bucket: int
    max_image_candidates: int
    inspect_cache: bool
    ffmpeg: str
    ffprobe: str
    log_level: str


def _load_config_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit("YAML config requires PyYAML. Install it or use JSON config.") from exc
        return dict(yaml.safe_load(raw) or {})
    return dict(json.loads(raw))


def _canonical_key(key: str) -> str:
    return key.replace("-", "_")


def _flatten_config(config: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in config.items():
        canonical = _canonical_key(key)
        if isinstance(value, dict):
            if canonical == "reports":
                if "html" in value:
                    flattened["output"] = value["html"]
                if "json" in value:
                    flattened["json_output"] = value["json"]
            elif canonical == "names":
                for nested_key, nested_value in value.items():
                    flattened[_canonical_key(nested_key)] = nested_value
            elif canonical == "video":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    if nested == "skip":
                        flattened["skip_video"] = nested_value
                    else:
                        flattened[nested] = nested_value
            elif canonical == "images":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    if nested == "skip":
                        flattened["skip_images"] = nested_value
                    elif nested == "threshold":
                        flattened["image_threshold"] = nested_value
                    elif nested in {"max_candidates", "max_image_candidates"}:
                        flattened["max_image_candidates"] = nested_value
                    else:
                        flattened[f"images_{nested}"] = nested_value
            elif canonical == "audio":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    if nested == "skip":
                        flattened["skip_audio"] = nested_value
                    elif nested == "threshold":
                        flattened["audio_threshold"] = nested_value
                    else:
                        flattened[f"audio_{nested}"] = nested_value
            elif canonical == "matching":
                for nested_key, nested_value in value.items():
                    nested = _canonical_key(nested_key)
                    if nested in {"video", "video_similarity"}:
                        flattened["video_threshold"] = nested_value
                    elif nested in {"image", "images", "image_similarity"}:
                        flattened["image_threshold"] = nested_value
                    elif nested in {"audio", "audio_similarity"}:
                        flattened["audio_threshold"] = nested_value
                    elif nested in {"folder", "folder_similarity"}:
                        flattened["folder_threshold"] = nested_value
                    elif nested in {"name", "name_similarity"}:
                        flattened["name_threshold"] = nested_value
                    else:
                        flattened[nested] = nested_value
            elif canonical == "cache" and "path" in value:
                flattened["cache"] = value["path"]
            else:
                for nested_key, nested_value in value.items():
                    flattened[f"{canonical}_{_canonical_key(nested_key)}"] = nested_value
        else:
            flattened[canonical] = value
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
    parser.add_argument("--video-threshold", type=float, default=None, help="Video fingerprint match threshold, percent.")
    parser.add_argument("--image-threshold", type=float, default=None, help="Image fingerprint match threshold, percent.")
    parser.add_argument("--audio-threshold", type=float, default=None, help="Audio fingerprint match threshold, percent.")
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
    parser.add_argument("--skip-video", dest="skip_video", action="store_true", default=None, help="Skip ffmpeg/ffprobe video fingerprinting.")
    parser.add_argument("--no-skip-video", dest="skip_video", action="store_false", help="Enable ffmpeg/ffprobe video fingerprinting.")
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
    parser.add_argument("--refresh-hashes", action="store_true", default=None, help="Recompute partial and full file hashes.")
    parser.add_argument("--refresh-video", action="store_true", default=None, help="Recompute cached video durations and fingerprints.")
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
    parser.add_argument("--refresh-names", action="store_true", default=None, help="Recompute cached title normalization results.")
    parser.add_argument(
        "--max-video-candidates-per-bucket",
        type=int,
        default=None,
        help="Use fingerprint blocking when a duration bucket has more candidates than this.",
    )
    parser.add_argument(
        "--max-image-candidates",
        type=int,
        default=None,
        help="Use fingerprint blocking when there are more image candidates than this.",
    )
    parser.add_argument("--inspect-cache", action="store_true", default=None, help="Print cache statistics as JSON and exit.")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg executable path/name.")
    parser.add_argument("--ffprobe", default=None, help="ffprobe executable path/name.")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    file_config: dict[str, Any] = _load_config_file(args.config) if args.config else {}
    merged = _merge_config(file_config, _load_cli_overrides(args))
    folders = merged.get("folders")
    if not folders and not merged.get("inspect_cache"):
        raise SystemExit("At least one folder is required. Use --folders or a config file.")

    return Config(
        folders=[Path(folder) for folder in folders or []],
        output=Path(str(merged["output"])),
        json_output=Path(str(merged["json_output"])),
        cache=Path(str(merged["cache"])),
        extensions=normalize_extensions(merged.get("extensions")),
        video_threshold=float(merged["video_threshold"]),
        image_threshold=float(merged["image_threshold"]),
        audio_threshold=float(merged["audio_threshold"]),
        folder_threshold=float(merged["folder_threshold"]),
        name_threshold=float(merged["name_threshold"]),
        name_provider=str(merged["name_provider"]),
        lmstudio_url=str(merged["lmstudio_url"]),
        lmstudio_model=str(merged["lmstudio_model"]),
        workers=max(1, int(merged["workers"])),
        skip_video=bool(merged["skip_video"]),
        skip_images=bool(merged["skip_images"]),
        skip_audio=bool(merged["skip_audio"]),
        refresh_hashes=bool(merged["refresh_hashes"]),
        refresh_video=bool(merged["refresh_video"]),
        refresh_images=bool(merged["refresh_images"]),
        refresh_audio=bool(merged["refresh_audio"]),
        refresh_names=bool(merged["refresh_names"]),
        max_video_candidates_per_bucket=max(2, int(merged["max_video_candidates_per_bucket"])),
        max_image_candidates=max(2, int(merged["max_image_candidates"])),
        inspect_cache=bool(merged["inspect_cache"]),
        ffmpeg=str(merged["ffmpeg"]),
        ffprobe=str(merged["ffprobe"]),
        log_level=str(merged["log_level"]),
    )


def run(config: Config) -> DedupeReport:
    warnings: list[str] = []
    with Cache(config.cache) as cache:
        records = scan_folders(config.folders, config.extensions, cache)
        LOGGER.info("Scanned %d media files", len(records))

        if config.refresh_hashes:
            LOGGER.info("Refreshing cached hashes for %d files", len(records))
            cache.clear_hashes(records)
        exact_groups = find_exact_duplicates(records, cache, workers=config.workers)
        LOGGER.info("Found %d exact duplicate groups", len(exact_groups))

        video_records = [record for record in records if is_video_path(record.path)]
        video_matches = []
        if video_records and not config.skip_video:
            ffmpeg, ffprobe = check_video_tools(config.ffmpeg, config.ffprobe)
            if not ffmpeg or not ffprobe:
                message = (
                    "ffmpeg/ffprobe were not found. Install them and ensure they are in PATH, "
                    "or pass --ffmpeg/--ffprobe, or use --skip-video."
                )
                LOGGER.warning(message)
                warnings.append(message)
            elif not VIDEO_PIL_AVAILABLE:
                message = "Pillow is not installed; video fingerprinting is skipped. Run pip install -e ."
                LOGGER.warning(message)
                warnings.append(message)
            else:
                if config.refresh_video:
                    LOGGER.info("Refreshing cached video metadata for %d files", len(video_records))
                    cache.clear_video(video_records)
                video_matches = find_video_matches(
                    video_records,
                    cache,
                    threshold=config.video_threshold,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    workers=min(config.workers, 4),
                    max_candidates_per_bucket=config.max_video_candidates_per_bucket,
                )
        LOGGER.info("Found %d video matches", len(video_matches))

        image_records = [record for record in records if is_image_path(record.path)]
        image_matches = []
        if image_records and not config.skip_images:
            if not IMAGE_PIL_AVAILABLE:
                message = "Pillow is not installed; image fingerprinting is skipped. Run pip install -e ."
                LOGGER.warning(message)
                warnings.append(message)
            else:
                if config.refresh_images:
                    LOGGER.info("Refreshing cached image fingerprints for %d files", len(image_records))
                    cache.clear_images(image_records)
                image_matches = find_image_matches(
                    image_records,
                    cache,
                    threshold=config.image_threshold,
                    workers=config.workers,
                    max_candidates=config.max_image_candidates,
                )
        LOGGER.info("Found %d image matches", len(image_matches))

        audio_records = [record for record in records if is_audio_path(record.path)]
        audio_matches = []
        if audio_records and not config.skip_audio:
            ffmpeg, ffprobe = check_video_tools(config.ffmpeg, config.ffprobe)
            if not ffmpeg or not ffprobe:
                message = (
                    "ffmpeg/ffprobe were not found. Audio fingerprinting is skipped; "
                    "install them, pass --ffmpeg/--ffprobe, or use --skip-audio."
                )
                LOGGER.warning(message)
                warnings.append(message)
            elif not check_chromaprint(ffmpeg):
                message = (
                    "This ffmpeg build does not provide the Chromaprint muxer; "
                    "audio fingerprinting is skipped."
                )
                LOGGER.warning(message)
                warnings.append(message)
            else:
                if config.refresh_audio:
                    LOGGER.info("Refreshing cached audio metadata for %d files", len(audio_records))
                    cache.clear_audio(audio_records)
                audio_matches = find_audio_matches(
                    audio_records,
                    cache,
                    threshold=config.audio_threshold,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    workers=min(config.workers, 4),
                )
        LOGGER.info("Found %d audio matches", len(audio_matches))

        normalized = normalize_names(
            records,
            cache,
            name_provider=config.name_provider,
            lmstudio_url=config.lmstudio_url,
            lmstudio_model=config.lmstudio_model,
            workers=min(config.workers, 5),
            refresh_names=config.refresh_names,
        )
        exact_paths = {path for group in exact_groups for path in group.paths}
        video_paths = {match.left for match in video_matches} | {match.right for match in video_matches}
        image_paths = {match.left for match in image_matches} | {match.right for match in image_matches}
        audio_paths = {match.left for match in audio_matches} | {match.right for match in audio_matches}
        name_hints = find_name_hints(
            records,
            normalized,
            exact_cluster_paths=exact_paths,
            video_cluster_paths=video_paths,
            image_cluster_paths=image_paths,
            audio_cluster_paths=audio_paths,
            fuzzy_threshold=config.name_threshold,
        )
        LOGGER.info("Found %d name-only hints", len(name_hints))

        assignments = build_cluster_assignments(
            records,
            exact_groups,
            video_matches,
            normalized,
            image_matches=image_matches,
            audio_matches=audio_matches,
        )
        folder_pairs = compare_folders(records, assignments, threshold=config.folder_threshold)
        LOGGER.info("Found %d folder pairs", len(folder_pairs))

    return DedupeReport(
        scanned_files=len(records),
        exact_duplicates=exact_groups,
        video_matches=video_matches,
        image_matches=image_matches,
        audio_matches=audio_matches,
        folder_pairs=folder_pairs,
        name_hints=name_hints,
        warnings=warnings,
    )


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
