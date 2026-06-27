from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .presets import TranscodePreset


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TranscodeRequest:
    input_path: Path
    output_path: Path
    preset_id: str
    preset: TranscodePreset | None = None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    index: int
    kind: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    language: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: Path
    size: int
    duration: float
    format_name: str | None
    streams: tuple[StreamInfo, ...]
    chapter_count: int = 0

    def stream_count(self, kind: str) -> int:
        return sum(stream.kind == kind for stream in self.streams)


@dataclass(frozen=True, slots=True)
class EncoderCapability:
    encoder: str
    listed: bool
    initialized: bool
    message: str = ""

    @property
    def available(self) -> bool:
        return self.listed and self.initialized


@dataclass(frozen=True, slots=True)
class TranscodePlan:
    job_id: str
    input_path: Path
    output_path: Path
    temporary_path: Path
    log_path: Path
    preset_id: str
    input_info: MediaInfo
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TranscodeProgress:
    job_id: str
    input_path: Path
    seconds: float
    duration: float
    percent: float
    speed: str | None = None
    fps: float | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    output_info: MediaInfo | None = None


@dataclass(frozen=True, slots=True)
class RunnerResult:
    status: JobStatus
    return_code: int | None
    elapsed_seconds: float
    message: str = ""


@dataclass(frozen=True, slots=True)
class TranscodeResult:
    job_id: str
    status: JobStatus
    input_path: Path
    output_path: Path
    log_path: Path | None
    preset_id: str
    message: str = ""
    input_size: int = 0
    output_size: int = 0
    elapsed_seconds: float = 0.0
    validation: ValidationResult | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    input_info: MediaInfo | None = None
    output_sha256: str | None = None
    input_modified_at: float | None = None
    input_sha256: str | None = None

    @property
    def saved_bytes(self) -> int:
        return self.input_size - self.output_size

    @property
    def savings_percent(self) -> float:
        if self.input_size <= 0:
            return 0.0
        return 100.0 * self.saved_bytes / self.input_size
