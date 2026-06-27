from __future__ import annotations

import subprocess

from ..video_fingerprint import resolve_binary
from .models import TranscodePlan, ValidationResult
from .probe import ProbeError, probe_media


def validate_output(
    plan: TranscodePlan,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if not plan.temporary_path.is_file() or plan.temporary_path.stat().st_size <= 0:
        return ValidationResult(False, ("FFmpeg did not produce a non-empty output file",))
    try:
        output_info = probe_media(plan.temporary_path, ffprobe)
    except ProbeError as exc:
        return ValidationResult(False, (str(exc),))

    tolerance = max(1.0, plan.input_info.duration * 0.005)
    duration_delta = abs(output_info.duration - plan.input_info.duration)
    if duration_delta > tolerance:
        errors.append(
            f"Duration differs by {duration_delta:.3f}s "
            f"(allowed {tolerance:.3f}s)"
        )
    for kind in ("video", "audio", "subtitle", "attachment"):
        expected = plan.input_info.stream_count(kind)
        actual = output_info.stream_count(kind)
        if actual < expected:
            errors.append(f"Missing {kind} streams: expected {expected}, found {actual}")
    if output_info.chapter_count < plan.input_info.chapter_count:
        errors.append(
            f"Missing chapters: expected {plan.input_info.chapter_count}, found {output_info.chapter_count}"
        )

    executable = resolve_binary(ffmpeg)
    if not executable:
        errors.append(f"ffmpeg executable not found: {ffmpeg}")
    else:
        try:
            decoded = subprocess.run(
                [
                    executable,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(plan.temporary_path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if decoded.returncode != 0:
                diagnostic = decoded.stderr.strip().splitlines()
                errors.append(diagnostic[-1] if diagnostic else "Cannot decode the first output video frame")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"Cannot decode the first output video frame: {exc}")

    if output_info.size >= plan.input_info.size:
        warnings.append("Output is not smaller than the source; the original was kept")
    return ValidationResult(not errors, tuple(errors), tuple(warnings), output_info)
