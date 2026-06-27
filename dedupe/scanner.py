from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from .cache import Cache
from .models import FileRecord
from .progress import tqdm

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".ts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".mts",
    ".vob",
    ".ogv",
    ".3gp",
    ".3g2",
    ".asf",
    ".divx",
    ".f4v",
    ".mxf",
    ".rm",
    ".rmvb",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
}

DEFAULT_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS

LOGGER = logging.getLogger(__name__)


def normalize_extensions(extensions: Iterable[str] | None) -> set[str]:
    if not extensions:
        return set(DEFAULT_EXTENSIONS)
    return {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_audio_path(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def scan_folders(folders: Iterable[Path], extensions: set[str], cache: Cache) -> list[FileRecord]:
    records: list[FileRecord] = []
    seen_paths: set[str] = set()
    skipped_duplicates = 0
    for folder in folders:
        root = folder.expanduser().resolve()
        if not root.exists() or not root.is_dir():
            LOGGER.warning("Skipping missing or non-directory folder: %s", root)
            continue
        candidates = (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in extensions)
        for path in tqdm(candidates, desc=f"Scanning {root}", unit="file"):
            try:
                resolved_path = path.resolve()
                stat = resolved_path.stat()
            except OSError as exc:
                LOGGER.warning("Cannot stat file %s: %s", path, exc)
                continue
            path_identity = os.path.normcase(str(resolved_path))
            if path_identity in seen_paths:
                skipped_duplicates += 1
                continue
            seen_paths.add(path_identity)
            record = FileRecord(
                path=resolved_path,
                root=root,
                size=stat.st_size,
                mtime=stat.st_mtime,
                raw_name=path.stem,
            )
            cache.hydrate_if_current(record)
            records.append(record)
    if skipped_duplicates:
        LOGGER.info("Skipped %d duplicate resolved file paths from overlapping roots", skipped_duplicates)
    cache.upsert_files(records)
    return records
