from __future__ import annotations

import subprocess

from ..video_fingerprint import resolve_binary
from .models import EncoderCapability
from .presets import TranscodePreset
from .probe import ProbeError, list_encoders


def check_encoder_capability(
    preset: TranscodePreset,
    ffmpeg: str = "ffmpeg",
    *,
    test_hardware: bool = True,
) -> EncoderCapability:
    """Check both FFmpeg encoder registration and hardware initialization."""
    try:
        encoders = list_encoders(ffmpeg)
    except ProbeError as exc:
        return EncoderCapability(preset.encoder, listed=False, initialized=False, message=str(exc))
    if preset.encoder not in encoders:
        return EncoderCapability(
            preset.encoder,
            listed=False,
            initialized=False,
            message=f"FFmpeg encoder is not available: {preset.encoder}",
        )
    if not preset.hardware or not test_hardware:
        return EncoderCapability(preset.encoder, listed=True, initialized=True)

    executable = resolve_binary(ffmpeg)
    if not executable:
        return EncoderCapability(
            preset.encoder,
            listed=True,
            initialized=False,
            message=f"ffmpeg executable not found: {ffmpeg}",
        )
    command = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:r=1:d=0.1",
        *preset.video_args,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return EncoderCapability(preset.encoder, listed=True, initialized=False, message=str(exc))
    if completed.returncode == 0:
        return EncoderCapability(preset.encoder, listed=True, initialized=True)
    diagnostic = completed.stderr.strip().splitlines()
    message = "\n".join(diagnostic[-3:]) if diagnostic else f"{preset.encoder} failed to initialize"
    return EncoderCapability(preset.encoder, listed=True, initialized=False, message=message)
