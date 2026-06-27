from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..actions import ActionOutcome, FileAction, FileActionService, OperationStatus
from ..events import ScanEvent, ScanEventType
from ..models import FileRecord
from ..name_normalizer import LMSTUDIO_MODEL, LMSTUDIO_URL
from ..report import write_html_report, write_json_report
from ..service import ScanOptions, ScanResult
from .preview import ComparisonWidget
from .journal_dialog import JournalDialog
from .result_items import CATEGORY_LABELS, ReviewItem, build_review_items, category_counts
from .worker import ActionJob, ActionWorker, ScanWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SameSame · Read-only media review")
        self.resize(1500, 900)
        self.setMinimumSize(1080, 680)
        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._action_thread: QThread | None = None
        self._action_worker: ActionWorker | None = None
        self._result: ScanResult | None = None
        self._items: list[ReviewItem] = []
        self._last_report_path: Path | None = None
        self._active_ffprobe = "ffprobe"
        self._close_when_finished = False
        self._last_terminal_status = "Ready"
        self._journal_dialog: JournalDialog | None = None
        self._review_decisions: dict[str, str] = {}

        self._build_ui()
        self._set_running(False)

    def _build_ui(self) -> None:
        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.addWidget(self._build_scan_panel())
        root_splitter.addWidget(self._build_results_panel())
        self.comparison = ComparisonWidget(self)
        self.comparison.action_requested.connect(self._request_action)
        self.comparison.batch_quarantine_requested.connect(self._request_batch_quarantine)
        root_splitter.addWidget(self.comparison)
        root_splitter.setStretchFactor(0, 0)
        root_splitter.setStretchFactor(1, 0)
        root_splitter.setStretchFactor(2, 1)
        root_splitter.setSizes([280, 360, 860])
        self.setCentralWidget(root_splitter)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_dock = QDockWidget("Scan log and warnings", self)
        log_dock.setObjectName("scanLogDock")
        log_dock.setWidget(self.log_view)
        log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, log_dock)

        self.status_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumWidth(260)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress_bar)

    def _build_scan_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(260)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("<h2>Scan</h2>"))

        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        folder_buttons = QHBoxLayout()
        self.add_folder_button = QPushButton("Add folder")
        self.add_folder_button.clicked.connect(self._add_folder)
        self.remove_folder_button = QPushButton("Remove")
        self.remove_folder_button.clicked.connect(self._remove_folders)
        folder_buttons.addWidget(self.add_folder_button)
        folder_buttons.addWidget(self.remove_folder_button)
        layout.addWidget(QLabel("Collection roots"))
        layout.addWidget(self.folder_list, 1)
        layout.addLayout(folder_buttons)

        settings_group = QGroupBox("Scan settings")
        settings_layout = QFormLayout(settings_group)
        self.name_provider = QComboBox()
        self.name_provider.addItem("Local heuristics", "none")
        self.name_provider.addItem("Automatic", "auto")
        self.name_provider.addItem("Anthropic", "anthropic")
        self.name_provider.addItem("LM Studio", "lmstudio")
        settings_layout.addRow("Name provider", self.name_provider)
        self.lmstudio_url = QLineEdit(LMSTUDIO_URL)
        self.lmstudio_model = QLineEdit(LMSTUDIO_MODEL)
        settings_layout.addRow("LM Studio URL", self.lmstudio_url)
        settings_layout.addRow("LM Studio model", self.lmstudio_model)

        self.workers = QSpinBox()
        self.workers.setRange(1, 32)
        self.workers.setValue(4)
        settings_layout.addRow("Workers", self.workers)
        self.video_threshold = self._threshold_spin(85.0)
        self.image_threshold = self._threshold_spin(90.0)
        self.audio_threshold = self._threshold_spin(94.0)
        self.name_threshold = self._threshold_spin(92.0)
        self.folder_threshold = self._threshold_spin(50.0)
        settings_layout.addRow("Video threshold", self.video_threshold)
        settings_layout.addRow("Image threshold", self.image_threshold)
        settings_layout.addRow("Audio threshold", self.audio_threshold)
        settings_layout.addRow("Name threshold", self.name_threshold)
        settings_layout.addRow("Folder threshold", self.folder_threshold)

        self.skip_video = QCheckBox("Skip video fingerprints")
        self.skip_images = QCheckBox("Skip image fingerprints")
        self.skip_audio = QCheckBox("Skip audio fingerprints")
        settings_layout.addRow(self.skip_video)
        settings_layout.addRow(self.skip_images)
        settings_layout.addRow(self.skip_audio)
        self.ffmpeg_path = QLineEdit("ffmpeg")
        self.ffprobe_path = QLineEdit("ffprobe")
        settings_layout.addRow("FFmpeg", self.ffmpeg_path)
        settings_layout.addRow("FFprobe", self.ffprobe_path)
        quarantine_row = QWidget()
        quarantine_layout = QHBoxLayout(quarantine_row)
        quarantine_layout.setContentsMargins(0, 0, 0, 0)
        self.quarantine_path = QLineEdit(str(self._default_quarantine_path()))
        quarantine_button = QPushButton("Browse")
        quarantine_button.clicked.connect(self._choose_quarantine_folder)
        quarantine_layout.addWidget(self.quarantine_path, 1)
        quarantine_layout.addWidget(quarantine_button)
        settings_layout.addRow("Quarantine", quarantine_row)
        layout.addWidget(settings_group)
        self.settings_group = settings_group

        scan_buttons = QHBoxLayout()
        self.scan_button = QPushButton("Start scan")
        self.scan_button.setDefault(True)
        self.scan_button.clicked.connect(self._start_scan)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_scan)
        scan_buttons.addWidget(self.scan_button)
        scan_buttons.addWidget(self.cancel_button)
        layout.addLayout(scan_buttons)

        report_buttons = QHBoxLayout()
        self.export_button = QPushButton("Export reports")
        self.export_button.clicked.connect(self._export_reports)
        self.open_report_button = QPushButton("Open report")
        self.open_report_button.clicked.connect(self._open_report)
        self.journal_button = QPushButton("Operation journal")
        self.journal_button.clicked.connect(self._open_journal)
        report_buttons.addWidget(self.export_button)
        report_buttons.addWidget(self.open_report_button)
        layout.addLayout(report_buttons)
        layout.addWidget(self.journal_button)
        return panel

    def _build_results_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("<h2>Results</h2>"))
        self.category_filter = QComboBox()
        self.category_filter.currentIndexChanged.connect(self._apply_filter)
        layout.addWidget(self.category_filter)
        self.result_list = QListWidget()
        self.result_list.currentItemChanged.connect(self._result_selected)
        layout.addWidget(self.result_list, 1)
        self.result_summary = QLabel("No scan results")
        self.result_summary.setWordWrap(True)
        layout.addWidget(self.result_summary)
        self._update_category_filter([])
        return panel

    @staticmethod
    def _threshold_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 100.0)
        spin.setDecimals(1)
        spin.setSuffix(" %")
        spin.setValue(value)
        return spin

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add collection root")
        if not folder:
            return
        resolved = str(Path(folder).resolve())
        existing = {self.folder_list.item(index).text() for index in range(self.folder_list.count())}
        if resolved not in existing:
            self.folder_list.addItem(resolved)

    def _remove_folders(self) -> None:
        for item in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(item))

    def _cache_path(self) -> Path:
        base = self._application_data_path()
        base.mkdir(parents=True, exist_ok=True)
        return base / "samesame.sqlite3"

    def _journal_path(self) -> Path:
        return self._application_data_path() / "operations.sqlite3"

    @staticmethod
    def _application_data_path() -> Path:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        return Path(location) if location else Path.home() / ".samesame"

    @staticmethod
    def _default_quarantine_path() -> Path:
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        base = Path(documents) if documents else Path.home()
        return base / "SameSame Quarantine"

    def _quarantine_root(self) -> Path:
        value = self.quarantine_path.text().strip()
        return Path(value).expanduser() if value else self._default_quarantine_path()

    def _choose_quarantine_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose quarantine folder",
            str(self._quarantine_root()),
        )
        if folder:
            self.quarantine_path.setText(folder)

    def _scan_options(self) -> ScanOptions:
        folders = [Path(self.folder_list.item(index).text()) for index in range(self.folder_list.count())]
        return ScanOptions(
            folders=folders,
            cache=self._cache_path(),
            video_threshold=self.video_threshold.value(),
            image_threshold=self.image_threshold.value(),
            audio_threshold=self.audio_threshold.value(),
            folder_threshold=self.folder_threshold.value(),
            name_threshold=self.name_threshold.value(),
            name_provider=str(self.name_provider.currentData()),
            lmstudio_url=self.lmstudio_url.text().strip() or LMSTUDIO_URL,
            lmstudio_model=self.lmstudio_model.text().strip() or LMSTUDIO_MODEL,
            workers=self.workers.value(),
            skip_video=self.skip_video.isChecked(),
            skip_images=self.skip_images.isChecked(),
            skip_audio=self.skip_audio.isChecked(),
            ffmpeg=self.ffmpeg_path.text().strip() or "ffmpeg",
            ffprobe=self.ffprobe_path.text().strip() or "ffprobe",
        )

    def _start_scan(self) -> None:
        if self._scan_thread is not None:
            return
        if self.folder_list.count() == 0:
            QMessageBox.information(self, "No folders", "Add at least one collection root before scanning.")
            return
        options = self._scan_options()
        self._active_ffprobe = options.ffprobe
        self._result = None
        self._items = []
        self._last_report_path = None
        self._review_decisions = {}
        self.log_view.clear()
        self.result_list.clear()
        self.comparison.clear()
        self._update_category_filter([])
        self._append_log("Starting read-only scan")
        self._last_terminal_status = "Scanning"

        thread = QThread(self)
        worker = ScanWorker(options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.event.connect(self._handle_event)
        worker.completed.connect(self._scan_completed)
        worker.cancelled.connect(self._scan_cancelled)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._scan_thread = thread
        self._scan_worker = worker
        self._set_running(True)
        thread.start()

    def _cancel_scan(self) -> None:
        if self._scan_worker is None:
            return
        self._append_log("Cancellation requested; committed cache work will be retained")
        self.status_label.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        self._scan_worker.cancel()

    def _handle_event(self, event: ScanEvent) -> None:
        if event.event_type == ScanEventType.STAGE_STARTED:
            stage = event.stage.value.replace("_", " ").title() if event.stage else "Scan"
            self.status_label.setText(stage)
            self._append_log(event.message or stage)
            if event.total is None:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, max(1, event.total))
                self.progress_bar.setValue(0)
        elif event.event_type == ScanEventType.PROGRESS:
            if event.total is None:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, max(1, event.total))
                self.progress_bar.setValue(event.current or 0)
            if event.message:
                self.status_label.setText(event.message)
        elif event.event_type == ScanEventType.STAGE_COMPLETED and event.total is not None:
            self.progress_bar.setRange(0, max(1, event.total))
            self.progress_bar.setValue(event.total)
        elif event.event_type == ScanEventType.WARNING:
            self._append_log(f"WARNING: {event.message}")
        elif event.event_type == ScanEventType.FAILED:
            self._append_log(f"ERROR: {event.message}")

    def _scan_completed(self, result: object) -> None:
        if not isinstance(result, ScanResult):
            self._scan_failed("Scanner returned an unexpected result")
            return
        self._result = result
        self._items = build_review_items(result.report)
        self._load_review_decisions()
        self.comparison.configure(result, self._active_ffprobe)
        self._update_category_filter(self._items)
        self._apply_filter()
        self.result_summary.setText(
            f"{result.report.scanned_files} files scanned · {len(self._items)} review results · "
            f"{len(result.report.warnings)} warnings"
        )
        self._append_log(f"Completed: {result.report.scanned_files} files, {len(self._items)} results")
        self._last_terminal_status = "Scan completed"

    def _scan_cancelled(self) -> None:
        self._append_log("Scan cancelled")
        self._last_terminal_status = "Scan cancelled"

    def _scan_failed(self, message: str) -> None:
        self._append_log(f"Scan failed: {message}")
        self._last_terminal_status = "Scan failed"
        QMessageBox.critical(self, "Scan failed", message)

    def _thread_finished(self) -> None:
        self._scan_thread = None
        self._scan_worker = None
        self._set_running(False)
        self.status_label.setText(self._last_terminal_status)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if self._result is not None else 0)
        if self._close_when_finished:
            QTimer.singleShot(0, self.close)

    def _set_running(self, running: bool) -> None:
        self.folder_list.setEnabled(not running)
        self.add_folder_button.setEnabled(not running)
        self.remove_folder_button.setEnabled(not running)
        self.settings_group.setEnabled(not running)
        self.scan_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.export_button.setEnabled(not running and self._result is not None)
        self.open_report_button.setEnabled(not running and self._last_report_path is not None)
        self.journal_button.setEnabled(not running and self._action_thread is None)
        self.comparison.set_actions_enabled(not running and self._action_thread is None)

    def _update_category_filter(self, items: list[ReviewItem]) -> None:
        counts = category_counts(items)
        selected = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        for category, label in CATEGORY_LABELS.items():
            self.category_filter.addItem(f"{label} ({counts[category]})", category)
        index = self.category_filter.findData(selected)
        self.category_filter.setCurrentIndex(max(0, index))
        self.category_filter.blockSignals(False)

    def _apply_filter(self) -> None:
        category = str(self.category_filter.currentData() or "all")
        self.result_list.clear()
        visible = self._items if category == "all" else [item for item in self._items if item.category == category]
        for item in visible:
            list_item = QListWidgetItem(f"{item.title}\n{Path(item.paths[0]).name if item.paths else ''}")
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            list_item.setToolTip("\n".join(item.paths))
            self.result_list.addItem(list_item)
        if visible:
            self.result_list.setCurrentRow(0)
        else:
            self.comparison.clear()

    def _result_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.comparison.clear()
            return
        item = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(item, ReviewItem):
            self.comparison.set_item(item)
            decision = self._review_decisions.get(self._group_id(item))
            if decision:
                self.comparison.set_decision(decision)

    def _export_reports(self) -> None:
        if self._result is None:
            return
        default = str(Path.home() / "samesame-report.html")
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export SameSame reports",
            default,
            "HTML report (*.html)",
        )
        if not filename:
            return
        html_path = Path(filename)
        if html_path.suffix.casefold() != ".html":
            html_path = html_path.with_suffix(".html")
        json_path = html_path.with_suffix(".json")
        try:
            write_html_report(self._result.report, html_path)
            write_json_report(self._result.report, json_path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._last_report_path = html_path
        self.open_report_button.setEnabled(True)
        self._append_log(f"Exported {html_path} and {json_path}")

    def _open_report(self) -> None:
        if self._last_report_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_report_path)))

    def _current_review_item(self) -> ReviewItem | None:
        current = self.result_list.currentItem()
        item = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        return item if isinstance(item, ReviewItem) else None

    @staticmethod
    def _group_id(item: ReviewItem) -> str:
        payload = "\0".join((item.category, *sorted(item.paths))).encode("utf-8")
        return f"{item.category}:{hashlib.sha256(payload).hexdigest()[:16]}"

    def _record_for_path(self, path_text: str, *, allow_directory: bool = False) -> FileRecord | None:
        if self._result is not None:
            for record in self._result.records:
                if record.path_key == path_text:
                    return record
        path = Path(path_text)
        if allow_directory and path.exists():
            stat = path.stat()
            return FileRecord(path.resolve(), path.resolve(), stat.st_size if path.is_file() else 0, stat.st_mtime, path.stem)
        return None

    def _request_action(self, action_value: object, path_text: str) -> None:
        if not isinstance(action_value, FileAction) or self._scan_thread is not None or self._action_thread is not None:
            return
        item = self._current_review_item()
        if item is None:
            return
        allow_directory = action_value in {FileAction.KEEP, FileAction.IGNORE}
        record = self._record_for_path(path_text, allow_directory=allow_directory)
        if record is None:
            QMessageBox.warning(self, "Unavailable file", "The selected file is no longer available in this scan.")
            return
        if action_value in {FileAction.QUARANTINE, FileAction.RECYCLE} and item.category not in {
            "exact",
            "video",
            "image",
            "audio",
        }:
            QMessageBox.warning(self, "Content evidence required", "File actions are disabled for name and folder hints.")
            return
        if not self._confirm_action(action_value, [record.path]):
            return
        self._start_action_worker([ActionJob(record, action_value, self._group_id(item))])

    def _request_batch_quarantine(self, paths_value: object, keep_path: str) -> None:
        if self._scan_thread is not None or self._action_thread is not None:
            return
        item = self._current_review_item()
        if item is None or item.category != "exact" or not isinstance(paths_value, tuple):
            return
        records = [
            record
            for path in paths_value
            if path != keep_path
            for record in [self._record_for_path(str(path))]
            if record is not None
        ]
        if not records:
            return
        if not self._confirm_action(FileAction.QUARANTINE, [record.path for record in records], keep_path=keep_path):
            return
        group_id = self._group_id(item)
        keeper = self._record_for_path(keep_path)
        jobs = [ActionJob(keeper, FileAction.KEEP, group_id)] if keeper is not None else []
        jobs.extend(ActionJob(record, FileAction.QUARANTINE, group_id) for record in records)
        self._start_action_worker(jobs)

    def _confirm_action(self, action: FileAction, paths: list[Path], *, keep_path: str | None = None) -> bool:
        if action in {FileAction.KEEP, FileAction.IGNORE}:
            return True
        path_lines = "\n".join(f"• {path}" for path in paths[:12])
        if len(paths) > 12:
            path_lines += f"\n… and {len(paths) - 12} more"
        if action == FileAction.QUARANTINE:
            keep_line = f"\nKept in place:\n{keep_path}\n" if keep_path else ""
            text = (
                f"Move {len(paths)} file(s) to quarantine?\n{keep_line}\n{path_lines}\n\n"
                "Every file will be revalidated against its scan identity and the operation will be journaled."
            )
            title = "Confirm quarantine"
        else:
            text = (
                f"Send {len(paths)} file(s) to the operating-system recycle bin?\n\n{path_lines}\n\n"
                "Every file will be revalidated. SameSame cannot automatically restore recycle-bin items."
            )
            title = "Confirm recycle"
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _start_action_worker(
        self,
        jobs: list[ActionJob] | None = None,
        *,
        restore_operation_id: str | None = None,
    ) -> None:
        if self._action_thread is not None:
            return
        thread = QThread(self)
        worker = ActionWorker(
            self._journal_path(),
            self._quarantine_root(),
            jobs=jobs,
            restore_operation_id=restore_operation_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._action_progress)
        worker.outcome.connect(self._action_outcome)
        worker.failed.connect(self._action_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._action_finished)
        thread.finished.connect(thread.deleteLater)
        self._action_thread = thread
        self._action_worker = worker
        self.comparison.set_actions_enabled(False)
        self.scan_button.setEnabled(False)
        self.journal_button.setEnabled(False)
        self.folder_list.setEnabled(False)
        self.settings_group.setEnabled(False)
        self.category_filter.setEnabled(False)
        self.result_list.setEnabled(False)
        thread.start()

    def _action_progress(self, current: int, total: int, message: str) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(current)
        self.status_label.setText(message)

    def _action_outcome(self, outcome_value: object) -> None:
        if not isinstance(outcome_value, ActionOutcome):
            return
        prefix = outcome_value.status.value.upper()
        self._append_log(f"{prefix} {outcome_value.action.value}: {outcome_value.source} · {outcome_value.message}")
        if outcome_value.status == OperationStatus.COMPLETED:
            decision = f"{outcome_value.action.value}: completed"
            self.comparison.set_decision(decision)
            if outcome_value.group_id:
                self._review_decisions[outcome_value.group_id] = decision
        elif outcome_value.status == OperationStatus.SKIPPED:
            self.comparison.set_decision(f"{outcome_value.action.value}: skipped")
        else:
            self.comparison.set_decision(f"{outcome_value.action.value}: failed")
            QMessageBox.warning(self, "File action failed", outcome_value.message)

    def _action_failed(self, message: str) -> None:
        self._append_log(f"Action worker failed: {message}")
        QMessageBox.critical(self, "Action worker failed", message)

    def _action_finished(self) -> None:
        self._action_thread = None
        self._action_worker = None
        self.scan_button.setEnabled(self._scan_thread is None)
        self.journal_button.setEnabled(self._scan_thread is None)
        self.comparison.set_actions_enabled(self._scan_thread is None)
        self.folder_list.setEnabled(self._scan_thread is None)
        self.settings_group.setEnabled(self._scan_thread is None)
        self.category_filter.setEnabled(True)
        self.result_list.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("File action finished")
        item = self._current_review_item()
        if item is not None:
            self.comparison.set_item(item)
            decision = self._review_decisions.get(self._group_id(item))
            if decision:
                self.comparison.set_decision(decision)
        if self._journal_dialog is not None:
            self._journal_dialog.refresh()
        if self._close_when_finished:
            QTimer.singleShot(0, self.close)

    def _open_journal(self) -> None:
        if self._journal_dialog is None:
            dialog = JournalDialog(self._journal_path(), self._quarantine_root(), self)
            dialog.restore_requested.connect(self._request_restore)
            dialog.finished.connect(self._journal_closed)
            self._journal_dialog = dialog
        self._journal_dialog.refresh()
        self._journal_dialog.show()
        self._journal_dialog.raise_()
        self._journal_dialog.activateWindow()

    def _journal_closed(self, _result: int) -> None:
        self._journal_dialog = None

    def _load_review_decisions(self) -> None:
        journal_path = self._journal_path()
        if not journal_path.exists():
            return
        service = FileActionService(journal_path, self._quarantine_root())
        for operation in reversed(service.recent_operations()):
            if operation.group_id and operation.status == OperationStatus.COMPLETED:
                self._review_decisions[operation.group_id] = f"{operation.action.value}: completed"

    def _request_restore(self, operation_id: str) -> None:
        if self._scan_thread is not None or self._action_thread is not None:
            return
        answer = QMessageBox.question(
            self,
            "Restore quarantined file",
            "Restore this file to its original path? The destination and content identity will be checked first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_action_worker(restore_operation_id=operation_id)

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API naming.
        if self._scan_worker is not None:
            self._close_when_finished = True
            self._cancel_scan()
            event.ignore()
            return
        if self._action_thread is not None:
            self._close_when_finished = True
            self.status_label.setText("Waiting for the journaled file action to finish…")
            event.ignore()
            return
        self.comparison.stop()
        event.accept()
