from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Iterator, Protocol

from .audio_fingerprint import check_chromaprint, find_audio_matches
from .cache import Cache
from .events import (
    CancellationToken,
    ScanCancelled,
    ScanEvent,
    ScanEventCallback,
    ScanEventType,
    ScanStage,
)
from .exact_hash import find_exact_duplicates
from .folder_compare import build_cluster_assignments, compare_folders
from .image_fingerprint import PIL_AVAILABLE as IMAGE_PIL_AVAILABLE
from .image_fingerprint import find_image_matches
from .metadata import MediaMetadata, basic_media_metadata
from .models import DedupeReport, FileRecord
from .name_normalizer import LMSTUDIO_MODEL, LMSTUDIO_URL, find_name_hints, normalize_names
from .progress import progress_scope
from .scanner import (
    is_audio_path,
    is_image_path,
    is_video_path,
    normalize_extensions,
    scan_folders,
)
from .video_fingerprint import PIL_AVAILABLE as VIDEO_PIL_AVAILABLE
from .video_fingerprint import check_video_tools, find_video_matches

LOGGER = logging.getLogger(__name__)


class _WarningCollector(logging.Handler):
    def __init__(self, warnings: list[str]) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = warnings
        self.stage: ScanStage | None = None
        self._pending: list[tuple[ScanStage | None, str]] = []
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        with self._lock:
            self.warnings.append(message)
            self._pending.append((self.stage, message))

    def drain(self, callback: ScanEventCallback | None) -> None:
        with self._lock:
            pending = self._pending
            self._pending = []
        if callback is not None:
            for stage, message in pending:
                callback(ScanEvent(ScanEventType.WARNING, stage=stage, message=message))


class ScanConfiguration(Protocol):
    folders: list[Path]
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
    ffmpeg: str
    ffprobe: str


@dataclass(slots=True)
class ScanOptions:
    """UI-agnostic options for calling the scanner from Python."""

    folders: list[Path]
    cache: Path = Path(".dedupe_cache.sqlite3")
    extensions: set[str] = field(default_factory=lambda: normalize_extensions(None))
    video_threshold: float = 85.0
    image_threshold: float = 90.0
    audio_threshold: float = 94.0
    folder_threshold: float = 50.0
    name_threshold: float = 92.0
    name_provider: str = "auto"
    lmstudio_url: str = LMSTUDIO_URL
    lmstudio_model: str = LMSTUDIO_MODEL
    workers: int = 4
    skip_video: bool = False
    skip_images: bool = False
    skip_audio: bool = False
    refresh_hashes: bool = False
    refresh_video: bool = False
    refresh_images: bool = False
    refresh_audio: bool = False
    refresh_names: bool = False
    max_video_candidates_per_bucket: int = 250
    max_image_candidates: int = 250
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass(slots=True)
class ScanResult:
    report: DedupeReport
    records: list[FileRecord]
    metadata: dict[str, MediaMetadata]


class ScanService:
    """Reusable scan orchestration for the CLI, desktop UI, tests, and scripts."""

    def __init__(self, *, show_terminal_progress: bool = False) -> None:
        self.show_terminal_progress = show_terminal_progress

    def run(
        self,
        config: ScanConfiguration,
        *,
        on_event: ScanEventCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ScanResult:
        token = cancellation or CancellationToken()
        warnings: list[str] = []
        warning_collector = _WarningCollector(warnings)
        package_logger = logging.getLogger("dedupe")
        package_logger.addHandler(warning_collector)
        try:
            return self._run(
                config,
                on_event=on_event,
                cancellation=token,
                warnings=warnings,
                warning_collector=warning_collector,
            )
        except ScanCancelled:
            self._emit(on_event, ScanEvent(ScanEventType.CANCELLED, message="Scan cancelled"))
            raise
        except Exception as exc:
            self._emit(on_event, ScanEvent(ScanEventType.FAILED, message=str(exc)))
            raise
        finally:
            warning_collector.drain(on_event)
            package_logger.removeHandler(warning_collector)

    @staticmethod
    def _emit(callback: ScanEventCallback | None, event: ScanEvent) -> None:
        if callback is not None:
            callback(event)

    @contextmanager
    def _stage(
        self,
        stage: ScanStage,
        *,
        on_event: ScanEventCallback | None,
        cancellation: CancellationToken,
        warning_collector: _WarningCollector,
        total: int | None = None,
        message: str = "",
    ) -> Iterator[None]:
        cancellation.raise_if_cancelled()
        previous_stage = warning_collector.stage
        warning_collector.stage = stage
        self._emit(
            on_event,
            ScanEvent(ScanEventType.STAGE_STARTED, stage=stage, message=message, current=0, total=total),
        )
        try:
            with progress_scope(
                stage=stage,
                callback=on_event,
                cancellation=cancellation,
                show_terminal=self.show_terminal_progress,
            ):
                yield
            cancellation.raise_if_cancelled()
            warning_collector.drain(on_event)
            self._emit(
                on_event,
                ScanEvent(ScanEventType.STAGE_COMPLETED, stage=stage, message=message, current=total, total=total),
            )
        finally:
            warning_collector.drain(on_event)
            warning_collector.stage = previous_stage

    def _warning(
        self,
        message: str,
    ) -> None:
        LOGGER.warning(message)

    def _run(
        self,
        config: ScanConfiguration,
        *,
        on_event: ScanEventCallback | None,
        cancellation: CancellationToken,
        warnings: list[str],
        warning_collector: _WarningCollector,
    ) -> ScanResult:
        with Cache(config.cache) as cache:
            with self._stage(
                ScanStage.SCANNING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                message="Discovering media files",
            ):
                records = scan_folders(config.folders, config.extensions, cache)
            LOGGER.info("Scanned %d media files", len(records))

            with self._stage(
                ScanStage.EXACT_MATCHING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(records),
                message="Finding exact duplicates",
            ):
                if config.refresh_hashes:
                    LOGGER.info("Refreshing cached hashes for %d files", len(records))
                    cache.clear_hashes(records)
                exact_groups = find_exact_duplicates(records, cache, workers=config.workers)
            LOGGER.info("Found %d exact duplicate groups", len(exact_groups))

            video_records = [record for record in records if is_video_path(record.path)]
            video_matches = []
            with self._stage(
                ScanStage.VIDEO_MATCHING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(video_records),
                message="Finding similar videos",
            ):
                if video_records and not config.skip_video:
                    ffmpeg, ffprobe = check_video_tools(config.ffmpeg, config.ffprobe)
                    if not ffmpeg or not ffprobe:
                        self._warning(
                            "ffmpeg/ffprobe were not found. Install them and ensure they are in PATH, "
                            "or pass --ffmpeg/--ffprobe, or use --skip-video.",
                        )
                    elif not VIDEO_PIL_AVAILABLE:
                        self._warning(
                            "Pillow is not installed; video fingerprinting is skipped. Run pip install -e .",
                        )
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
            with self._stage(
                ScanStage.IMAGE_MATCHING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(image_records),
                message="Finding similar images",
            ):
                if image_records and not config.skip_images:
                    if not IMAGE_PIL_AVAILABLE:
                        self._warning(
                            "Pillow is not installed; image fingerprinting is skipped. Run pip install -e .",
                        )
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
            with self._stage(
                ScanStage.AUDIO_MATCHING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(audio_records),
                message="Finding similar audio",
            ):
                if audio_records and not config.skip_audio:
                    ffmpeg, ffprobe = check_video_tools(config.ffmpeg, config.ffprobe)
                    if not ffmpeg or not ffprobe:
                        self._warning(
                            "ffmpeg/ffprobe were not found. Audio fingerprinting is skipped; "
                            "install them, pass --ffmpeg/--ffprobe, or use --skip-audio.",
                        )
                    elif not check_chromaprint(ffmpeg):
                        self._warning(
                            "This ffmpeg build does not provide the Chromaprint muxer; audio fingerprinting is skipped.",
                        )
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

            with self._stage(
                ScanStage.NAME_MATCHING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(records),
                message="Normalizing names and finding hints",
            ):
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

            with self._stage(
                ScanStage.CLUSTERING,
                on_event=on_event,
                cancellation=cancellation,
                warning_collector=warning_collector,
                total=len(records),
                message="Building content clusters",
            ):
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

        report = DedupeReport(
            scanned_files=len(records),
            exact_duplicates=exact_groups,
            video_matches=video_matches,
            image_matches=image_matches,
            audio_matches=audio_matches,
            folder_pairs=folder_pairs,
            name_hints=name_hints,
            warnings=warnings,
        )
        result = ScanResult(
            report=report,
            records=records,
            metadata={record.path_key: basic_media_metadata(record) for record in records},
        )
        self._emit(
            on_event,
            ScanEvent(
                ScanEventType.COMPLETED,
                message="Scan completed",
                current=len(records),
                total=len(records),
                unit="file",
            ),
        )
        return result
