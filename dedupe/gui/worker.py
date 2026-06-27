from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..events import CancellationToken, ScanCancelled, ScanEvent
from ..metadata import MediaMetadata, probe_media_metadata
from ..models import FileRecord
from ..service import ScanOptions, ScanService


class ScanWorker(QObject):
    event = Signal(object)
    completed = Signal(object)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, options: ScanOptions) -> None:
        super().__init__()
        self.options = options
        self.cancellation = CancellationToken()

    @Slot()
    def run(self) -> None:
        try:
            result = ScanService().run(
                self.options,
                on_event=self._forward_event,
                cancellation=self.cancellation,
            )
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - surface worker failures in the GUI.
            self.failed.emit(str(exc))
        else:
            self.completed.emit(result)
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self.cancellation.cancel()

    def _forward_event(self, event: ScanEvent) -> None:
        self.event.emit(event)


class MetadataSignals(QObject):
    completed = Signal(str, object)


class MetadataTask(QRunnable):
    def __init__(self, record: FileRecord, ffprobe: str) -> None:
        super().__init__()
        self.record = record
        self.ffprobe = ffprobe
        self.signals = MetadataSignals()

    @Slot()
    def run(self) -> None:
        metadata: MediaMetadata = probe_media_metadata(self.record, self.ffprobe)
        self.signals.completed.emit(self.record.path_key, metadata)
