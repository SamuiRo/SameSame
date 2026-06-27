from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..scanner import is_video_path
from ..transcode.command_builder import default_output_path
from ..transcode.models import (
    EncoderCapability,
    JobStatus,
    MediaInfo,
    TranscodeProgress,
    TranscodeRequest,
    TranscodeResult,
)
from ..transcode.presets import PRESETS
from ..transcode.promotion import PromotionResult
from .worker import TranscodeCapabilityWorker, TranscodePromotionWorker, TranscodeWorker


class TranscodeDialog(QDialog):
    journal_changed = Signal()
    busy_changed = Signal(bool)

    def __init__(
        self,
        paths: list[Path],
        *,
        ffmpeg: str,
        ffprobe: str,
        journal_path: Path,
        quarantine_root: Path,
        collection_roots: dict[str, Path] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.journal_path = journal_path
        self.quarantine_root = quarantine_root
        self.collection_roots = collection_roots or {}
        self._capability: EncoderCapability | None = None
        self._capability_preset = ""
        self._capability_thread: QThread | None = None
        self._capability_worker: TranscodeCapabilityWorker | None = None
        self._queue_thread: QThread | None = None
        self._queue_worker: TranscodeWorker | None = None
        self._promotion_thread: QThread | None = None
        self._promotion_worker: TranscodePromotionWorker | None = None
        self._results: dict[str, TranscodeResult] = {}
        self._close_when_finished = False
        self._externally_enabled = True

        self.setWindowTitle("SameSame · Transcode queue")
        self.resize(1120, 650)
        self._build_ui()
        self._add_paths(paths)
        self._check_capability()

    @property
    def is_busy(self) -> bool:
        return self._queue_thread is not None or self._promotion_thread is not None

    @property
    def has_background_work(self) -> bool:
        return self.is_busy or self._capability_thread is not None

    def add_paths(self, paths: list[Path]) -> None:
        self._add_paths(paths)

    def set_external_actions_enabled(self, enabled: bool) -> None:
        self._externally_enabled = enabled
        self.start_button.setEnabled(
            enabled and not self.is_busy and self.table.rowCount() > 0 and self._capability_available()
        )
        self._selection_changed()

    def _build_ui(self) -> None:
        settings = QHBoxLayout()
        self.preset_combo = QComboBox()
        for preset in PRESETS.values():
            self.preset_combo.addItem(f"{preset.name} — {preset.description}", preset.preset_id)
        self.preset_combo.setCurrentIndex(self.preset_combo.findData("anime_x265_balanced"))
        self.preset_combo.currentIndexChanged.connect(self._check_capability)
        self.capability_label = QLabel("Checking encoder…")
        self.capability_label.setWordWrap(True)
        self.check_button = QPushButton("Recheck")
        self.check_button.clicked.connect(self._check_capability)
        settings.addWidget(QLabel("Preset"))
        settings.addWidget(self.preset_combo, 1)
        settings.addWidget(self.capability_label, 1)
        settings.addWidget(self.check_button)

        output_row = QHBoxLayout()
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Output beside each source")
        self.browse_output_button = QPushButton("Output folder…")
        self.browse_output_button.clicked.connect(self._choose_output_dir)
        self.add_files_button = QPushButton("Add videos…")
        self.add_files_button.clicked.connect(self._choose_files)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove_selected)
        output_row.addWidget(QLabel("Output"))
        output_row.addWidget(self.output_dir, 1)
        output_row.addWidget(self.browse_output_button)
        output_row.addWidget(self.add_files_button)
        output_row.addWidget(self.remove_button)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Input", "Preset", "Status", "Progress", "Output", "Size / savings"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(130)
        self.details.setPlaceholderText("Select a completed job to compare input and output metadata.")

        controls = QHBoxLayout()
        self.start_button = QPushButton("Start queue")
        self.start_button.clicked.connect(self._start_queue)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_queue)
        self.retry_button = QPushButton("Retry failed")
        self.retry_button.clicked.connect(self._retry_failed)
        self.open_output_button = QPushButton("Open output")
        self.open_output_button.clicked.connect(self._open_output)
        self.open_log_button = QPushButton("Open log")
        self.open_log_button.clicked.connect(self._open_log)
        self.promote_button = QPushButton("Quarantine original + promote…")
        self.promote_button.clicked.connect(self._promote_selected)
        controls.addWidget(self.start_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.retry_button)
        controls.addStretch(1)
        controls.addWidget(self.open_output_button)
        controls.addWidget(self.open_log_button)
        controls.addWidget(self.promote_button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label = QLabel("Ready")
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.progress)

        layout = QVBoxLayout(self)
        layout.addLayout(settings)
        layout.addLayout(output_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.details)
        layout.addLayout(controls)
        layout.addLayout(status_row)
        self._set_queue_running(False)
        self._selection_changed()

    @staticmethod
    def _key(path: Path) -> str:
        return str(path.expanduser().resolve())

    def _add_paths(self, paths: list[Path]) -> None:
        existing = {self._key(Path(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))) for row in range(self.table.rowCount())}
        for path in paths:
            resolved = path.expanduser().resolve()
            if not resolved.is_file() or not is_video_path(resolved) or self._key(resolved) in existing:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            input_item = QTableWidgetItem(str(resolved))
            input_item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            self.table.setItem(row, 0, input_item)
            self.table.setItem(row, 1, QTableWidgetItem(str(self.preset_combo.currentData())))
            self.table.setItem(row, 2, QTableWidgetItem("Ready"))
            self.table.setItem(row, 3, QTableWidgetItem("0.00%"))
            self.table.setItem(row, 4, QTableWidgetItem(""))
            self.table.setItem(row, 5, QTableWidgetItem(f"{resolved.stat().st_size:,} bytes"))
            existing.add(self._key(resolved))
        self.start_button.setEnabled(
            self._externally_enabled and self.table.rowCount() > 0 and self._capability_available()
        )

    def _choose_files(self) -> None:
        filenames, _selected = QFileDialog.getOpenFileNames(self, "Add videos to transcode")
        self._add_paths([Path(name) for name in filenames])

    def _choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose transcode output folder", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)

    def _remove_selected(self) -> None:
        for row in sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True):
            self.table.removeRow(row)
        self._selection_changed()

    def _capability_available(self) -> bool:
        return bool(
            self._capability
            and self._capability.available
            and self._capability_preset == str(self.preset_combo.currentData())
        )

    def _check_capability(self, *_args: object) -> None:
        if self._capability_thread is not None or self._queue_thread is not None:
            return
        preset_id = str(self.preset_combo.currentData())
        self._capability = None
        self._capability_preset = ""
        self.capability_label.setText("Checking encoder and hardware initialization…")
        self.start_button.setEnabled(False)
        thread = QThread(self)
        worker = TranscodeCapabilityWorker(preset_id, self.ffmpeg)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._capability_ready)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._capability_finished)
        thread.finished.connect(thread.deleteLater)
        self._capability_thread = thread
        self._capability_worker = worker
        thread.start()

    def _capability_ready(self, preset_id: str, value: object) -> None:
        if not isinstance(value, EncoderCapability):
            return
        self._capability = value
        self._capability_preset = preset_id
        if value.available:
            self.capability_label.setText(f"Available: {value.encoder}")
        else:
            self.capability_label.setText(f"Unavailable: {value.message}")

    def _capability_finished(self) -> None:
        self._capability_thread = None
        self._capability_worker = None
        current = str(self.preset_combo.currentData())
        if current != self._capability_preset and not self._close_when_finished:
            self._check_capability()
            return
        self.start_button.setEnabled(
            self._externally_enabled and self.table.rowCount() > 0 and self._capability_available()
        )
        self._finish_close_if_requested()

    def _requests(self, *, retry_only: bool = False) -> list[TranscodeRequest]:
        preset_id = str(self.preset_combo.currentData())
        output_value = self.output_dir.text().strip()
        output_dir = Path(output_value).expanduser() if output_value else None
        requests: list[TranscodeRequest] = []
        allocated_outputs: set[str] = set()
        retry_statuses = {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED}
        for row in range(self.table.rowCount()):
            source = Path(str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))
            previous = self._results.get(self._key(source))
            if retry_only and (previous is None or previous.status not in retry_statuses):
                continue
            if not retry_only and previous is not None and previous.status == JobStatus.COMPLETED:
                continue
            output = default_output_path(source, preset_id, output_dir)
            if self._key(output) in allocated_outputs:
                for index in range(2, 10_000):
                    candidate = output.with_name(f"{output.stem} ({index}){output.suffix}")
                    if self._key(candidate) not in allocated_outputs:
                        output = candidate
                        break
            allocated_outputs.add(self._key(output))
            requests.append(TranscodeRequest(source, output, preset_id))
            self.table.item(row, 1).setText(preset_id)
            self.table.item(row, 2).setText("Queued")
            self.table.item(row, 3).setText("0.00%")
            self.table.item(row, 4).setText(str(output))
        return requests

    def _start_queue(self) -> None:
        if self.is_busy or not self._externally_enabled or not self._capability_available():
            return
        requests = self._requests()
        if not requests:
            return
        self._launch_queue(requests)

    def _retry_failed(self) -> None:
        if self.is_busy or not self._externally_enabled or not self._capability_available():
            return
        requests = self._requests(retry_only=True)
        if requests:
            self._launch_queue(requests)

    def _launch_queue(self, requests: list[TranscodeRequest]) -> None:
        thread = QThread(self)
        worker = TranscodeWorker(requests, self.ffmpeg, self.ffprobe)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._transcode_progress)
        worker.result.connect(self._transcode_result)
        worker.failed.connect(self._transcode_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._queue_finished)
        thread.finished.connect(thread.deleteLater)
        self._queue_thread = thread
        self._queue_worker = worker
        self._set_queue_running(True)
        self.status_label.setText(f"Running {len(requests)} queued job(s)")
        thread.start()

    def _row_for_path(self, path: Path) -> int:
        key = self._key(path)
        for row in range(self.table.rowCount()):
            if self._key(Path(str(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)))) == key:
                return row
        return -1

    def _transcode_progress(self, value: object) -> None:
        if not isinstance(value, TranscodeProgress):
            return
        row = self._row_for_path(value.input_path)
        if row >= 0:
            self.table.item(row, 2).setText("Running")
            self.table.item(row, 3).setText(f"{value.percent:.2f}%")
        self.progress.setValue(round(value.percent))
        speed = f" · {value.speed}" if value.speed else ""
        self.status_label.setText(f"{value.input_path.name}: {value.percent:.2f}%{speed}")

    def _transcode_result(self, value: object) -> None:
        if not isinstance(value, TranscodeResult):
            return
        self._results[self._key(value.input_path)] = value
        row = self._row_for_path(value.input_path)
        if row >= 0:
            self.table.item(row, 2).setText(value.status.value.title())
            self.table.item(row, 3).setText("100.00%" if value.status == JobStatus.COMPLETED else "—")
            self.table.item(row, 4).setText(str(value.output_path))
            if value.output_size:
                self.table.item(row, 5).setText(
                    f"{value.input_size:,} → {value.output_size:,} bytes ({value.savings_percent:.1f}% saved)"
                )
        self.status_label.setText(f"{value.input_path.name}: {value.status.value} — {value.message}")
        self._selection_changed()

    def _transcode_failed(self, message: str) -> None:
        self.status_label.setText(f"Queue failed: {message}")
        QMessageBox.critical(self, "Transcode queue failed", message)

    def _queue_finished(self) -> None:
        self._queue_thread = None
        self._queue_worker = None
        self._set_queue_running(False)
        self.progress.setValue(0)
        self.status_label.setText("Queue finished")
        self._selection_changed()
        self._finish_close_if_requested()

    def _cancel_queue(self) -> None:
        if self._queue_worker is not None:
            self.status_label.setText("Cancelling after FFmpeg stops safely…")
            self.cancel_button.setEnabled(False)
            self._queue_worker.cancel()

    def _set_queue_running(self, running: bool) -> None:
        self.preset_combo.setEnabled(not running)
        self.output_dir.setEnabled(not running)
        self.start_button.setEnabled(
            self._externally_enabled and not running and self.table.rowCount() > 0 and self._capability_available()
        )
        self.cancel_button.setEnabled(running)
        self.retry_button.setEnabled(not running)
        self.remove_button.setEnabled(not running)
        self.browse_output_button.setEnabled(not running)
        self.add_files_button.setEnabled(not running)
        self.check_button.setEnabled(not running)
        self.busy_changed.emit(self.is_busy)

    def _selected_result(self) -> TranscodeResult | None:
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        source = Path(str(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)))
        return self._results.get(self._key(source))

    @staticmethod
    def _media_line(label: str, info: MediaInfo | None) -> str:
        if info is None:
            return f"{label}: unavailable"
        counts = ", ".join(
            f"{kind}={info.stream_count(kind)}" for kind in ("video", "audio", "subtitle", "attachment")
        )
        return f"{label}: {info.size:,} bytes · {info.duration:.3f}s · {counts} · chapters={info.chapter_count}"

    def _selection_changed(self) -> None:
        result = self._selected_result()
        completed = bool(result and result.status == JobStatus.COMPLETED and result.output_path.exists())
        self.open_output_button.setEnabled(completed)
        self.open_log_button.setEnabled(bool(result and result.log_path and result.log_path.exists()))
        self.promote_button.setEnabled(
            self._externally_enabled and completed and self._promotion_thread is None and self._queue_thread is None
        )
        if result is None:
            self.details.clear()
            return
        output_info = result.validation.output_info if result.validation else None
        lines = [
            self._media_line("Input", result.input_info),
            self._media_line("Output", output_info),
            f"Savings: {result.saved_bytes:,} bytes ({result.savings_percent:.2f}%)",
        ]
        lines.extend(f"Warning: {warning}" for warning in result.warnings)
        if result.message:
            lines.append(result.message)
        self.details.setPlainText("\n".join(lines))

    def _open_output(self) -> None:
        result = self._selected_result()
        if result and result.output_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.output_path)))

    def _open_log(self) -> None:
        result = self._selected_result()
        if result and result.log_path and result.log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.log_path)))

    def _promote_selected(self) -> None:
        result = self._selected_result()
        if result is None or result.status != JobStatus.COMPLETED or self.is_busy or not self._externally_enabled:
            return
        target = result.input_path if result.input_path.suffix.casefold() == ".mkv" else result.input_path.with_suffix(".mkv")
        answer = QMessageBox.question(
            self,
            "Confirm source replacement",
            "The original will first be identity-checked and moved to journaled quarantine.\n\n"
            f"Original: {result.input_path}\nEncoded: {result.output_path}\nPromoted target: {target}\n\n"
            "Only then will the validated encoded MKV be moved into the collection. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        root = self.collection_roots.get(self._key(result.input_path))
        thread = QThread(self)
        worker = TranscodePromotionWorker(
            result,
            self.journal_path,
            self.quarantine_root,
            root,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._promotion_completed)
        worker.failed.connect(self._promotion_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._promotion_finished)
        thread.finished.connect(thread.deleteLater)
        self._promotion_thread = thread
        self._promotion_worker = worker
        self.status_label.setText("Validating and quarantining original…")
        self._set_queue_running(True)
        self.cancel_button.setEnabled(False)
        thread.start()

    def _promotion_completed(self, value: object) -> None:
        if not isinstance(value, PromotionResult):
            return
        self.status_label.setText(value.message)
        if value.quarantine is not None:
            self.journal_changed.emit()
        if value.success:
            row = self._row_for_path(value.source_path)
            if row >= 0:
                self.table.item(row, 2).setText("Promoted; original quarantined")
                self.table.item(row, 4).setText(str(value.target_path))
            QMessageBox.information(self, "Transcode promoted", value.message)
        else:
            QMessageBox.warning(self, "Promotion failed", value.message)

    def _promotion_failed(self, message: str) -> None:
        self.status_label.setText(f"Promotion worker failed: {message}")
        QMessageBox.critical(self, "Promotion failed", message)

    def _promotion_finished(self) -> None:
        self._promotion_thread = None
        self._promotion_worker = None
        self._set_queue_running(False)
        self._selection_changed()
        self._finish_close_if_requested()

    def request_close(self) -> None:
        self._close_when_finished = True
        if self._queue_worker is not None:
            self._queue_worker.cancel()
        self._finish_close_if_requested()

    def _finish_close_if_requested(self) -> None:
        if self._close_when_finished and not self.is_busy and self._capability_thread is None:
            self._close_when_finished = False
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API naming.
        if self.is_busy or self._capability_thread is not None:
            self.request_close()
            event.ignore()
            return
        event.accept()
