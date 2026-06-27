from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..scanner import VIDEO_EXTENSIONS
from ..transcode.models import MediaInfo
from ..transcode.presets import PRESETS, TranscodePreset
from ..transcode.probe import ProbeError, probe_media


@dataclass(frozen=True, slots=True)
class CompressionVideo:
    path: Path
    size: int
    duration: float
    codec: str
    resolution: str

    @classmethod
    def from_media_info(cls, info: MediaInfo) -> CompressionVideo:
        video = next(stream for stream in info.streams if stream.kind == "video")
        resolution = f"{video.width}×{video.height}" if video.width and video.height else "—"
        return cls(info.path, info.size, info.duration, video.codec or "unknown", resolution)


def discover_video_paths(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )


def matches_filters(
    video: CompressionVideo,
    *,
    extensions: set[str],
    minimum_size_mb: float = 0.0,
    maximum_size_mb: float = 0.0,
    minimum_duration_minutes: float = 0.0,
    maximum_duration_minutes: float = 0.0,
) -> bool:
    size_mb = video.size / (1024 * 1024)
    duration_minutes = video.duration / 60.0
    return bool(
        video.path.suffix.casefold() in extensions
        and size_mb >= minimum_size_mb
        and (maximum_size_mb <= 0 or size_mb <= maximum_size_mb)
        and duration_minutes >= minimum_duration_minutes
        and (maximum_duration_minutes <= 0 or duration_minutes <= maximum_duration_minutes)
    )


def build_custom_preset(
    encoder: str,
    quality: int,
    speed: str,
    pixel_format: str,
    *,
    x265_params: str = "",
    full_resolution_multipass: bool = False,
) -> TranscodePreset:
    if encoder not in {"libx265", "av1_nvenc", "hevc_nvenc"}:
        raise ValueError(f"Unsupported custom encoder: {encoder}")
    if not 0 <= quality <= 51:
        raise ValueError("CRF/CQ must be between 0 and 51")
    if pixel_format not in {"yuv420p", "yuv420p10le"}:
        raise ValueError(f"Unsupported pixel format: {pixel_format}")

    metric = "crf" if encoder == "libx265" else "cq"
    args = ["-c:v", encoder, f"-{metric}", str(quality), "-preset", speed]
    if encoder == "libx265" and x265_params.strip():
        args.extend(("-x265-params", x265_params.strip()))
    if encoder != "libx265":
        args.extend(("-tune", "hq"))
        if encoder == "av1_nvenc" and full_resolution_multipass:
            args.extend(("-multipass", "fullres"))
    args.extend(("-pix_fmt", pixel_format))
    digest = hashlib.sha1("\0".join(args).encode("utf-8")).hexdigest()[:8]
    return TranscodePreset(
        preset_id=f"custom_{encoder}_{metric}{quality}_{speed}_{digest}",
        name=f"Custom {encoder}",
        encoder=encoder,
        video_args=tuple(args),
        hardware=encoder.endswith("_nvenc"),
        description=f"{metric.upper()} {quality} · {speed} · {pixel_format}",
    )


class CompressionFolderWorker(QObject):
    item_ready = Signal(object)
    progress = Signal(int, int, str)
    warning = Signal(str)
    completed = Signal(int, int)
    finished = Signal()

    def __init__(self, folder: Path, ffprobe: str) -> None:
        super().__init__()
        self.folder = folder
        self.ffprobe = ffprobe
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        loaded = 0
        failed = 0
        try:
            paths = discover_video_paths(self.folder)
            total = len(paths)
            for index, path in enumerate(paths, start=1):
                if self._cancelled.is_set():
                    break
                self.progress.emit(index - 1, total, path.name)
                try:
                    self.item_ready.emit(CompressionVideo.from_media_info(probe_media(path, self.ffprobe)))
                    loaded += 1
                except (OSError, ProbeError, StopIteration) as exc:
                    failed += 1
                    self.warning.emit(f"{path}: {exc}")
                self.progress.emit(index, total, path.name)
            self.completed.emit(loaded, failed)
        except OSError as exc:
            self.warning.emit(f"Cannot scan {self.folder}: {exc}")
            self.completed.emit(loaded, failed + 1)
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self._cancelled.set()


class CompressionTab(QWidget):
    queue_requested = Signal(object, object, object, object)
    busy_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: CompressionFolderWorker | None = None
        self._videos: list[CompressionVideo] = []
        self._build_ui()

    @property
    def is_loading(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        folder_row = QHBoxLayout()
        self.folder_path = QLineEdit()
        self.folder_path.setPlaceholderText("Choose a folder containing videos")
        self.browse_button = QPushButton("Choose folder…")
        self.browse_button.clicked.connect(self._choose_folder)
        self.load_button = QPushButton("Load videos")
        self.load_button.clicked.connect(self._load_folder)
        self.cancel_button = QPushButton("Cancel loading")
        self.cancel_button.clicked.connect(self.cancel_loading)
        folder_row.addWidget(QLabel("Folder"))
        folder_row.addWidget(self.folder_path, 1)
        folder_row.addWidget(self.browse_button)
        folder_row.addWidget(self.load_button)
        folder_row.addWidget(self.cancel_button)

        body = QHBoxLayout()
        body.addWidget(self._build_filter_panel())
        body.addLayout(self._build_video_panel(), 1)

        layout = QVBoxLayout(self)
        title = QLabel("<h2>Batch video compression</h2>")
        subtitle = QLabel(
            "Load one folder recursively, check individual videos or select them by extension, size, and duration. "
            "Encoding creates validated MKV files and keeps every original."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(folder_row)
        layout.addLayout(body, 1)
        self._set_loading(False)
        self._preset_changed()

    def _build_filter_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(410)
        layout = QVBoxLayout(panel)

        filters = QGroupBox("Quick selection filters")
        filter_layout = QFormLayout(filters)
        self.extension_list = QListWidget()
        self.extension_list.setMaximumHeight(145)
        for extension in sorted(VIDEO_EXTENSIONS):
            item = QListWidgetItem(extension)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.extension_list.addItem(item)
        filter_layout.addRow("Extensions", self.extension_list)
        self.minimum_size = self._range_spin(" MB", 100_000.0)
        self.maximum_size = self._range_spin(" MB", 100_000.0)
        self.minimum_duration = self._range_spin(" min", 100_000.0)
        self.maximum_duration = self._range_spin(" min", 100_000.0)
        filter_layout.addRow("Minimum size", self.minimum_size)
        filter_layout.addRow("Maximum size (0 = any)", self.maximum_size)
        filter_layout.addRow("Minimum duration", self.minimum_duration)
        filter_layout.addRow("Maximum duration (0 = any)", self.maximum_duration)
        layout.addWidget(filters)

        selection_row = QHBoxLayout()
        self.select_matching_button = QPushButton("Select matching")
        self.select_matching_button.clicked.connect(self._select_matching)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear_selection)
        selection_row.addWidget(self.select_matching_button)
        selection_row.addWidget(self.clear_button)
        layout.addLayout(selection_row)

        preset_group = QGroupBox("Compression settings")
        preset_layout = QVBoxLayout(preset_group)
        self.preset_combo = QComboBox()
        for preset in PRESETS.values():
            self.preset_combo.addItem(f"{preset.name} — {preset.description}", preset.preset_id)
        self.preset_combo.addItem("Custom settings…", "custom")
        self.preset_combo.setCurrentIndex(self.preset_combo.findData("anime_x265_balanced"))
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        preset_layout.addWidget(self.preset_combo)

        self.custom_group = QGroupBox("Custom preset")
        custom_layout = QFormLayout(self.custom_group)
        self.encoder_combo = QComboBox()
        self.encoder_combo.addItems(("libx265", "av1_nvenc", "hevc_nvenc"))
        self.encoder_combo.currentTextChanged.connect(self._encoder_changed)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(0, 51)
        self.quality_spin.setValue(22)
        self.speed_combo = QComboBox()
        self.pixel_format_combo = QComboBox()
        self.pixel_format_combo.addItems(("yuv420p10le", "yuv420p"))
        self.x265_params = QLineEdit("no-sao=1:aq-mode=3:deblock=-1,-1")
        self.multipass = QCheckBox("Full-resolution multipass (AV1 NVENC)")
        custom_layout.addRow("Encoder", self.encoder_combo)
        custom_layout.addRow("CRF / CQ", self.quality_spin)
        custom_layout.addRow("Speed preset", self.speed_combo)
        custom_layout.addRow("Pixel format", self.pixel_format_combo)
        custom_layout.addRow("x265 parameters", self.x265_params)
        custom_layout.addRow(self.multipass)
        preset_layout.addWidget(self.custom_group)
        self._encoder_changed("libx265")
        layout.addWidget(preset_group)

        output_group = QGroupBox("Output")
        output_layout = QHBoxLayout(output_group)
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Beside each source")
        self.output_button = QPushButton("Browse…")
        self.output_button.clicked.connect(self._choose_output)
        output_layout.addWidget(self.output_dir, 1)
        output_layout.addWidget(self.output_button)
        layout.addWidget(output_group)
        layout.addStretch(1)
        return panel

    def _build_video_panel(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(("Use", "Video", "Folder", "Ext", "Size", "Duration", "Codec / resolution"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemChanged.connect(self._update_summary)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("No folder loaded")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        status_row = QHBoxLayout()
        status_row.addWidget(self.summary_label, 1)
        status_row.addWidget(self.progress)
        layout.addLayout(status_row)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b06000")
        layout.addWidget(self.warning_label)
        self.queue_button = QPushButton("Open compression queue with checked videos")
        self.queue_button.clicked.connect(self._request_queue)
        layout.addWidget(self.queue_button)
        return layout

    @staticmethod
    def _range_spin(suffix: str, maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setDecimals(1)
        spin.setSuffix(suffix)
        return spin

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose video folder", self.folder_path.text())
        if folder:
            self.folder_path.setText(folder)

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)

    def _load_folder(self) -> None:
        if self.is_loading:
            return
        folder = Path(self.folder_path.text().strip()).expanduser()
        if not folder.is_dir():
            QMessageBox.information(self, "Folder required", "Choose an existing folder before loading videos.")
            return
        self.folder_path.setText(str(folder.resolve()))
        self.table.setRowCount(0)
        self._videos.clear()
        self.warning_label.clear()
        self.summary_label.setText("Discovering videos…")
        thread = QThread(self)
        worker = CompressionFolderWorker(folder.resolve(), self._ffprobe())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.item_ready.connect(self._add_video)
        worker.progress.connect(self._loading_progress)
        worker.warning.connect(self._loading_warning)
        worker.completed.connect(self._loading_completed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._loading_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self._set_loading(True)
        thread.start()

    def _ffprobe(self) -> str:
        window = self.window()
        field = getattr(window, "ffprobe_path", None)
        return field.text().strip() if field is not None and field.text().strip() else "ffprobe"

    def _add_video(self, value: object) -> None:
        if not isinstance(value, CompressionVideo):
            return
        self._videos.append(value)
        row = self.table.rowCount()
        self.table.insertRow(row)
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        check.setCheckState(Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, value)
        self.table.setItem(row, 0, check)
        self.table.setItem(row, 1, QTableWidgetItem(value.path.name))
        self.table.setItem(row, 2, QTableWidgetItem(str(value.path.parent)))
        self.table.setItem(row, 3, QTableWidgetItem(value.path.suffix.casefold()))
        self.table.setItem(row, 4, QTableWidgetItem(self._format_size(value.size)))
        self.table.setItem(row, 5, QTableWidgetItem(self._format_duration(value.duration)))
        self.table.setItem(row, 6, QTableWidgetItem(f"{value.codec} · {value.resolution}"))

    def _loading_progress(self, current: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.summary_label.setText(f"Reading metadata {current}/{total}: {name}")

    def _loading_warning(self, message: str) -> None:
        self.warning_label.setText(message)

    def _loading_completed(self, loaded: int, failed: int) -> None:
        suffix = f" · {failed} could not be read" if failed else ""
        self.summary_label.setText(f"Loaded {loaded} video(s){suffix}")

    def _loading_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_loading(False)
        self._update_summary()

    def cancel_loading(self) -> None:
        if self._worker is not None:
            self.summary_label.setText("Cancelling folder load…")
            self.cancel_button.setEnabled(False)
            self._worker.cancel()

    def _set_loading(self, loading: bool) -> None:
        self.folder_path.setEnabled(not loading)
        self.browse_button.setEnabled(not loading)
        self.load_button.setEnabled(not loading)
        self.cancel_button.setEnabled(loading)
        self.queue_button.setEnabled(not loading and bool(self._checked_videos()))
        self.busy_changed.emit(loading)

    def _extensions(self) -> set[str]:
        return {
            self.extension_list.item(index).text()
            for index in range(self.extension_list.count())
            if self.extension_list.item(index).checkState() == Qt.CheckState.Checked
        }

    def _select_matching(self) -> None:
        extensions = self._extensions()
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                video = item.data(Qt.ItemDataRole.UserRole)
                match = isinstance(video, CompressionVideo) and matches_filters(
                    video,
                    extensions=extensions,
                    minimum_size_mb=self.minimum_size.value(),
                    maximum_size_mb=self.maximum_size.value(),
                    minimum_duration_minutes=self.minimum_duration.value(),
                    maximum_duration_minutes=self.maximum_duration.value(),
                )
                item.setCheckState(Qt.CheckState.Checked if match else Qt.CheckState.Unchecked)
        finally:
            self.table.blockSignals(False)
        self._update_summary()

    def _clear_selection(self) -> None:
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        finally:
            self.table.blockSignals(False)
        self._update_summary()

    def _checked_videos(self) -> list[CompressionVideo]:
        videos: list[CompressionVideo] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            value = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked and isinstance(value, CompressionVideo):
                videos.append(value)
        return videos

    def _update_summary(self, *_args: object) -> None:
        selected = self._checked_videos()
        selected_size = sum(video.size for video in selected)
        self.summary_label.setText(
            f"{len(self._videos)} video(s) loaded · {len(selected)} checked · {self._format_size(selected_size)} selected"
        )
        self.queue_button.setEnabled(not self.is_loading and bool(selected))

    def _preset_changed(self, *_args: object) -> None:
        self.custom_group.setEnabled(self.preset_combo.currentData() == "custom")

    def _encoder_changed(self, encoder: str) -> None:
        current = self.speed_combo.currentText()
        self.speed_combo.clear()
        if encoder == "libx265":
            self.speed_combo.addItems(("medium", "slow", "slower", "veryslow"))
        else:
            self.speed_combo.addItems(tuple(f"p{index}" for index in range(1, 8)))
        index = self.speed_combo.findText(current)
        self.speed_combo.setCurrentIndex(index if index >= 0 else self.speed_combo.count() - 1)
        is_x265 = encoder == "libx265"
        preferred_pixel_format = "yuv420p10le" if is_x265 else "yuv420p"
        self.pixel_format_combo.setCurrentIndex(self.pixel_format_combo.findText(preferred_pixel_format))
        self.x265_params.setEnabled(is_x265)
        self.multipass.setEnabled(encoder == "av1_nvenc")

    def _selected_preset(self) -> TranscodePreset:
        preset_id = str(self.preset_combo.currentData())
        if preset_id != "custom":
            return PRESETS[preset_id]
        return build_custom_preset(
            self.encoder_combo.currentText(),
            self.quality_spin.value(),
            self.speed_combo.currentText(),
            self.pixel_format_combo.currentText(),
            x265_params=self.x265_params.text(),
            full_resolution_multipass=self.multipass.isChecked(),
        )

    def _request_queue(self) -> None:
        videos = self._checked_videos()
        if not videos:
            return
        output_value = self.output_dir.text().strip()
        output_dir = Path(output_value).expanduser() if output_value else None
        root = Path(self.folder_path.text()).expanduser().resolve()
        self.queue_requested.emit([video.path for video in videos], self._selected_preset(), output_dir, root)

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, round(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
