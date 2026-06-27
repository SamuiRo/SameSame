from __future__ import annotations

import uuid
from pathlib import Path

from ..video_fingerprint import resolve_binary
from .models import MediaInfo, TranscodePlan
from .presets import TranscodePreset


class PlanError(RuntimeError):
    pass


def _log_path(output_path: Path) -> Path:
    base = output_path.with_suffix(output_path.suffix + ".ffmpeg.log")
    if not base.exists():
        return base
    for index in range(1, 10000):
        candidate = output_path.with_suffix(output_path.suffix + f".ffmpeg.{index}.log")
        if not candidate.exists():
            return candidate
    raise PlanError(f"Cannot allocate a diagnostic log path for {output_path}")


def default_output_path(input_path: Path, preset_id: str, output_dir: Path | None = None) -> Path:
    directory = output_dir.expanduser() if output_dir is not None else input_path.parent
    return directory / f"{input_path.stem}.{preset_id}.mkv"


def build_plan(
    input_info: MediaInfo,
    output_path: Path,
    preset: TranscodePreset,
    *,
    ffmpeg: str = "ffmpeg",
) -> TranscodePlan:
    executable = resolve_binary(ffmpeg)
    if not executable:
        raise PlanError(f"ffmpeg executable not found: {ffmpeg}")
    source = input_info.path.resolve()
    output = output_path.expanduser().resolve()
    if output.suffix.casefold() != ".mkv":
        raise PlanError("Transcode output must use the .mkv container")
    if source == output:
        raise PlanError("Transcode output cannot overwrite the source file")
    if output.exists():
        raise PlanError(f"Output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    temporary = output.with_name(f".{output.stem}.{job_id[:12]}.part.mkv")
    if temporary.exists():
        raise PlanError(f"Temporary output path already exists: {temporary}")
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "0:t?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        *preset.video_args,
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-c:t",
        "copy",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary),
    )
    return TranscodePlan(
        job_id=job_id,
        input_path=source,
        output_path=output,
        temporary_path=temporary,
        log_path=_log_path(output),
        preset_id=preset.preset_id,
        input_info=input_info,
        command=command,
    )
