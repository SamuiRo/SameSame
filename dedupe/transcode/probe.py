from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .. import process
from ..video_fingerprint import resolve_binary
from .models import MediaInfo, StreamInfo


class ProbeError(RuntimeError):
    pass


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _frame_rate(raw: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = str(raw.get(key) or "")
        numerator, _, denominator = value.partition("/")
        try:
            rate = float(numerator) / float(denominator or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < rate < 1000:
            return rate
    return None


def _bit_rate(raw: dict[str, Any], tags: Any) -> int | None:
    value = _optional_int(raw.get("bit_rate"))
    if value:
        return value
    # Matroska keeps per-stream bitrate in mkvmerge's statistics tags.
    for key, tag in tags.items():
        if str(key).upper() in {"BPS", "BPS-ENG"}:
            value = _optional_int(tag)
            if value:
                return value
    return None


def _stream(raw: dict[str, Any]) -> StreamInfo:
    tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
    return StreamInfo(
        index=_optional_int(raw.get("index")) or 0,
        kind=str(raw.get("codec_type") or "unknown"),
        codec=str(raw["codec_name"]) if raw.get("codec_name") else None,
        width=_optional_int(raw.get("width")),
        height=_optional_int(raw.get("height")),
        channels=_optional_int(raw.get("channels")),
        language=str(tags["language"]) if tags.get("language") else None,
        title=str(tags["title"]) if tags.get("title") else None,
        frame_rate=_frame_rate(raw) if raw.get("codec_type") == "video" else None,
        bit_rate=_bit_rate(raw, tags),
    )


def probe_media(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise ProbeError(f"Input is not a file: {resolved_path}")
    executable = resolve_binary(ffprobe)
    if not executable:
        raise ProbeError(f"ffprobe executable not found: {ffprobe}")
    command = [
        executable,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(resolved_path),
    ]
    try:
        completed = process.run(command, capture_output=True, text=True, check=True, timeout=60)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ProbeError(f"Cannot probe media file {resolved_path}: {exc}") from exc
    raw_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    try:
        duration = float(raw_format.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        stream_durations: list[float] = []
        for item in raw_streams:
            if not isinstance(item, dict):
                continue
            try:
                stream_durations.append(float(item.get("duration") or 0.0))
            except (TypeError, ValueError):
                continue
        duration = max(stream_durations, default=0.0)
    streams = tuple(_stream(item) for item in raw_streams if isinstance(item, dict))
    if not any(stream.kind == "video" for stream in streams):
        raise ProbeError(f"Input does not contain a video stream: {resolved_path}")
    if duration <= 0:
        raise ProbeError(f"Input duration is unavailable or invalid: {resolved_path}")
    return MediaInfo(
        path=resolved_path,
        size=resolved_path.stat().st_size,
        duration=duration,
        format_name=str(raw_format["format_name"]) if raw_format.get("format_name") else None,
        streams=streams,
        chapter_count=len(payload.get("chapters") or []),
    )


def list_encoders(ffmpeg: str = "ffmpeg") -> set[str]:
    executable = resolve_binary(ffmpeg)
    if not executable:
        raise ProbeError(f"ffmpeg executable not found: {ffmpeg}")
    try:
        completed = process.run(
            [executable, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"Cannot list FFmpeg encoders: {exc}") from exc
    encoders: set[str] = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and len(fields[0]) == 6 and fields[0][0] in {"V", "A", "S", "."}:
            encoders.add(fields[1])
    return encoders
