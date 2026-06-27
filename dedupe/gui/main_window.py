from __future__ import annotations

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

from ..events import ScanEvent, ScanEventType
from ..name_normalizer import LMSTUDIO_MODEL, LMSTUDIO_URL
from ..report import write_html_report, write_json_report
from ..service import ScanOptions, ScanResult
from .preview import ComparisonWidget
from .result_items import CATEGORY_LABELS, ReviewItem, build_review_items, category_counts
from .worker import ScanWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SameSame · Read-only media review")
        self.resize(1500, 900)
        self.setMinimumSize(1080, 680)
        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._result: ScanResult | None = None
        self._items: list[ReviewItem] = []
        self._last_report_path: Path | None = None
        self._active_ffprobe = "ffprobe"
        self._close_when_finished = False
        self._last_terminal_status = "Ready"

        self._build_ui()
        self._set_running(False)

    def _build_ui(self) -> None:
        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.addWidget(self._build_scan_panel())
        root_splitter.addWidget(self._build_results_panel())
        self.comparison = ComparisonWidget(self)
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
        report_buttons.addWidget(self.export_button)
        report_buttons.addWidget(self.open_report_button)
        layout.addLayout(report_buttons)
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
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        base = Path(location) if location else Path.home() / ".samesame"
        base.mkdir(parents=True, exist_ok=True)
        return base / "samesame.sqlite3"

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

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API naming.
        if self._scan_worker is not None:
            self._close_when_finished = True
            self._cancel_scan()
            event.ignore()
            return
        self.comparison.stop()
        event.accept()
