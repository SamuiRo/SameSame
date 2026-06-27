"""Independent, source-preserving FFmpeg transcoding backend."""

from .models import JobStatus, TranscodePlan, TranscodeRequest, TranscodeResult
from .presets import PRESETS, TranscodePreset, get_preset
from .queue import TranscodeQueue

__all__ = [
    "JobStatus",
    "PRESETS",
    "TranscodePlan",
    "TranscodeRequest",
    "TranscodePreset",
    "TranscodeQueue",
    "TranscodeResult",
    "get_preset",
]
