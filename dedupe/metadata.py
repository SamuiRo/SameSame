from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import FileRecord
from .scanner import is_audio_path, is_image_path, is_video_path
from .video_fingerprint import resolve_binary

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class MediaStreamMetadata:
    index: int
    kind: str
    codec: str | None = None
    codec_long_name: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    pixel_format: str | None = None
    frame_rate: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    path: str
    media_type: str
    size: int
    modified_at: float
    duration: float | None = None
    container: str | None = None
    bit_rate: int | None = None
    streams: list[MediaStreamMetadata] = field(default_factory=list)
    chapter_count: int = 0
    attachment_count: int = 0
    error: str | None = None

    @property
    def video_streams(self) -> list[MediaStreamMetadata]:
        return [stream for stream in self.streams if stream.kind == "video"]

    @property
    def image_streams(self) -> list[MediaStreamMetadata]:
        return [stream for stream in self.streams if stream.kind == "image"]

    @property
    def audio_streams(self) -> list[MediaStreamMetadata]:
        return [stream for stream in self.streams if stream.kind == "audio"]

    @property
    def subtitle_streams(self) -> list[MediaStreamMetadata]:
        return [stream for stream in self.streams if stream.kind == "subtitle"]


def media_type_for_path(path: Path) -> str:
    if is_video_path(path):
        return "video"
    if is_image_path(path):
        return "image"
    if is_audio_path(path):
        return "audio"
    return "file"


def basic_media_metadata(record: FileRecord) -> MediaMetadata:
    return MediaMetadata(
        path=record.path_key,
        media_type=media_type_for_path(record.path),
        size=record.size,
        modified_at=record.mtime,
        duration=record.duration,
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _frame_rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except (ValueError, ZeroDivisionError):
        return None


def _stream_metadata(raw: dict[str, Any]) -> MediaStreamMetadata:
    tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
    disposition = raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
    return MediaStreamMetadata(
        index=_optional_int(raw.get("index")) or 0,
        kind=str(raw.get("codec_type") or "unknown"),
        codec=str(raw["codec_name"]) if raw.get("codec_name") else None,
        codec_long_name=str(raw["codec_long_name"]) if raw.get("codec_long_name") else None,
        profile=str(raw["profile"]) if raw.get("profile") else None,
        width=_optional_int(raw.get("width")),
        height=_optional_int(raw.get("height")),
        pixel_format=str(raw["pix_fmt"]) if raw.get("pix_fmt") else None,
        frame_rate=_frame_rate(raw.get("avg_frame_rate") or raw.get("r_frame_rate")),
        sample_rate=_optional_int(raw.get("sample_rate")),
        channels=_optional_int(raw.get("channels")),
        channel_layout=str(raw["channel_layout"]) if raw.get("channel_layout") else None,
        language=str(tags["language"]) if tags.get("language") else None,
        title=str(tags["title"]) if tags.get("title") else None,
        is_default=bool(disposition.get("default")),
        is_forced=bool(disposition.get("forced")),
    )


def _probe_image(record: FileRecord) -> MediaMetadata:
    basic = basic_media_metadata(record)
    if Image is None or ImageOps is None:
        return MediaMetadata(
            path=basic.path,
            media_type=basic.media_type,
            size=basic.size,
            modified_at=basic.modified_at,
            error="Pillow is not installed",
        )
    try:
        with Image.open(record.path) as source:
            image = ImageOps.exif_transpose(source)
            stream = MediaStreamMetadata(
                index=0,
                kind="image",
                codec=(source.format or record.path.suffix.lstrip(".")).casefold(),
                width=image.width,
                height=image.height,
                pixel_format=image.mode,
            )
            return MediaMetadata(
                path=basic.path,
                media_type=basic.media_type,
                size=basic.size,
                modified_at=basic.modified_at,
                container=source.format,
                streams=[stream],
            )
    except (OSError, ValueError) as exc:
        return MediaMetadata(
            path=basic.path,
            media_type=basic.media_type,
            size=basic.size,
            modified_at=basic.modified_at,
            error=str(exc),
        )


def probe_media_metadata(record: FileRecord, ffprobe: str = "ffprobe") -> MediaMetadata:
    """Read review-oriented media details without modifying the source file."""

    if is_image_path(record.path):
        return _probe_image(record)

    basic = basic_media_metadata(record)
    if not (is_video_path(record.path) or is_audio_path(record.path)):
        return basic
    resolved_ffprobe = resolve_binary(ffprobe)
    if not resolved_ffprobe:
        return MediaMetadata(
            path=basic.path,
            media_type=basic.media_type,
            size=basic.size,
            modified_at=basic.modified_at,
            duration=basic.duration,
            error=f"ffprobe executable not found: {ffprobe}",
        )
    command = [
        resolved_ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(record.path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        payload = json.loads(completed.stdout)
        raw_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
        streams = [_stream_metadata(stream) for stream in raw_streams if isinstance(stream, dict)]
        return MediaMetadata(
            path=basic.path,
            media_type=basic.media_type,
            size=basic.size,
            modified_at=basic.modified_at,
            duration=_optional_float(raw_format.get("duration")) or basic.duration,
            container=str(raw_format["format_name"]) if raw_format.get("format_name") else None,
            bit_rate=_optional_int(raw_format.get("bit_rate")),
            streams=streams,
            chapter_count=len(payload.get("chapters") or []),
            attachment_count=sum(stream.kind == "attachment" for stream in streams),
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return MediaMetadata(
            path=basic.path,
            media_type=basic.media_type,
            size=basic.size,
            modified_at=basic.modified_at,
            duration=basic.duration,
            error=str(exc),
        )
