from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import Callable


class ScanStage(str, Enum):
    SCANNING = "scanning"
    EXACT_MATCHING = "exact_matching"
    VIDEO_MATCHING = "video_matching"
    IMAGE_MATCHING = "image_matching"
    AUDIO_MATCHING = "audio_matching"
    NAME_MATCHING = "name_matching"
    CLUSTERING = "clustering"


class ScanEventType(str, Enum):
    STAGE_STARTED = "stage_started"
    PROGRESS = "progress"
    WARNING = "warning"
    STAGE_COMPLETED = "stage_completed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScanEvent:
    event_type: ScanEventType
    stage: ScanStage | None = None
    message: str = ""
    current: int | None = None
    total: int | None = None
    unit: str | None = None


ScanEventCallback = Callable[[ScanEvent], None]


class ScanCancelled(Exception):
    """Raised when a cooperative scan cancellation is observed."""


class CancellationToken:
    """Thread-safe cancellation signal shared by a caller and scan worker."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise ScanCancelled("Scan cancelled")
