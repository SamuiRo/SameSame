from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..actions import FileAction, FileActionService
from ..events import CancellationToken, ScanCancelled, ScanEvent
from ..metadata import MediaMetadata, probe_media_metadata
from ..models import FileRecord
from ..service import ScanOptions, ScanService
from ..transcode.capabilities import check_encoder_capability
from ..transcode.models import TranscodeRequest
from ..transcode.presets import TranscodePreset, get_preset
from ..transcode.promotion import promote_transcode
from ..transcode.queue import TranscodeQueue


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


@dataclass(frozen=True, slots=True)
class ActionJob:
    record: FileRecord
    action: FileAction
    group_id: str | None = None


class ActionWorker(QObject):
    progress = Signal(int, int, str)
    outcome = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        journal_path: Path,
        quarantine_root: Path,
        *,
        jobs: list[ActionJob] | None = None,
        restore_operation_id: str | None = None,
    ) -> None:
        super().__init__()
        self.journal_path = journal_path
        self.quarantine_root = quarantine_root
        self.jobs = jobs or []
        self.restore_operation_id = restore_operation_id

    @Slot()
    def run(self) -> None:
        service = FileActionService(self.journal_path, self.quarantine_root)
        try:
            if self.restore_operation_id is not None:
                self.progress.emit(0, 1, "Validating quarantine restore")
                outcome = service.restore(self.restore_operation_id)
                self.outcome.emit(outcome)
                self.progress.emit(1, 1, "Restore finished")
            else:
                total = len(self.jobs)
                seen_sources: set[str] = set()
                for index, job in enumerate(self.jobs, start=1):
                    self.progress.emit(index - 1, total, f"Preflight: {job.record.path.name}")
                    if job.record.path_key in seen_sources:
                        outcome = service.record_skipped(
                            job.record,
                            job.action,
                            "Duplicate source in the same action batch",
                            group_id=job.group_id,
                        )
                    else:
                        seen_sources.add(job.record.path_key)
                        outcome = service.perform(job.record, job.action, group_id=job.group_id)
                    self.outcome.emit(outcome)
                    self.progress.emit(index, total, outcome.message)
        except Exception as exc:  # noqa: BLE001 - surface unexpected journal/action worker failures.
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class TranscodeCapabilityWorker(QObject):
    completed = Signal(str, object)
    finished = Signal()

    def __init__(self, preset_id: str, ffmpeg: str, preset: TranscodePreset | None = None) -> None:
        super().__init__()
        self.preset_id = preset_id
        self.ffmpeg = ffmpeg
        self.preset = preset

    @Slot()
    def run(self) -> None:
        try:
            capability = check_encoder_capability(self.preset or get_preset(self.preset_id), self.ffmpeg)
            self.completed.emit(self.preset_id, capability)
        finally:
            self.finished.emit()


class TranscodeWorker(QObject):
    progress = Signal(object)
    result = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, requests: list[TranscodeRequest], ffmpeg: str, ffprobe: str) -> None:
        super().__init__()
        self.requests = requests
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.cancellation = CancellationToken()

    @Slot()
    def run(self) -> None:
        try:
            queue = TranscodeQueue(
                ffmpeg=self.ffmpeg,
                ffprobe=self.ffprobe,
                progress_callback=self.progress.emit,
                result_callback=self.result.emit,
            )
            queue.run(self.requests, cancellation=self.cancellation)
        except Exception as exc:  # noqa: BLE001 - surface unexpected queue failures in the GUI.
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self.cancellation.cancel()


class TranscodePromotionWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        result: object,
        journal_path: Path,
        quarantine_root: Path,
        collection_root: Path | None,
    ) -> None:
        super().__init__()
        self.result = result
        self.journal_path = journal_path
        self.quarantine_root = quarantine_root
        self.collection_root = collection_root

    @Slot()
    def run(self) -> None:
        from ..transcode.models import TranscodeResult

        try:
            if not isinstance(self.result, TranscodeResult):
                raise TypeError("Invalid transcode result")
            outcome = promote_transcode(
                self.result,
                journal_path=self.journal_path,
                quarantine_root=self.quarantine_root,
                collection_root=self.collection_root,
            )
            self.completed.emit(outcome)
        except Exception as exc:  # noqa: BLE001 - surface unexpected promotion failures in the GUI.
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
