from __future__ import annotations

import hashlib
import html
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
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

from ..actions import FileAction
from ..scanner import VIDEO_EXTENSIONS
from ..transcode.assessment import AudioTrack, CompressionAssessment, Verdict, assess_compression
from ..transcode.models import MediaInfo, OutputLayout
from ..transcode.presets import PRESETS, TranscodePreset
from ..transcode.probe import ProbeError, probe_media
from ..transcode.source_cleanup import cleanup_frees_source_space
from .transcode_dialog import add_builtin_presets, preset_help_label


@dataclass(frozen=True, slots=True)
class CompressionVideo:
    path: Path
    size: int
    duration: float
    codec: str
    resolution: str
    probe_error: str = ""
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    audio: tuple[AudioTrack, ...] = ()

    @classmethod
    def from_media_info(cls, info: MediaInfo) -> CompressionVideo:
        video = next(stream for stream in info.streams if stream.kind == "video")
        resolution = f"{video.width}×{video.height}" if video.width and video.height else "—"
        audio = tuple(
            AudioTrack(stream.codec or "unknown", stream.bit_rate) for stream in info.streams if stream.kind == "audio"
        )
        return cls(
            info.path,
            info.size,
            info.duration,
            video.codec or "unknown",
            resolution,
            width=video.width,
            height=video.height,
            frame_rate=video.frame_rate,
            audio=audio,
        )

    def assess(self, preset: TranscodePreset) -> CompressionAssessment | None:
        if self.probe_error:
            return None
        return assess_compression(
            size=self.size,
            duration=self.duration,
            codec=self.codec,
            width=self.width,
            height=self.height,
            frame_rate=self.frame_rate,
            audio=self.audio,
            category=preset.category,
            encoder=preset.encoder,
        )


#: Choices for the "Worth compressing" quick filter: label, minimum verdict rank.
VERDICT_FILTERS = (
    ("Any verdict", 0),
    ("Maybe or Worth it", Verdict.MAYBE.rank),
    ("Worth it only", Verdict.WORTH.rank),
)

#: Translucent tints that read on both light and dark palettes.
_VERDICT_TINT = {
    Verdict.WORTH: QColor(46, 160, 67, 70),
    Verdict.MAYBE: QColor(210, 150, 20, 70),
    Verdict.SKIP: QColor(128, 128, 128, 40),
}
_VERDICT_DOT = {Verdict.WORTH: "#2e9e46", Verdict.MAYBE: "#c98a00", Verdict.SKIP: "#888888"}

#: Item data roles on the "Use" cell, and the numeric sort key of any cell.
_ASSESSMENT_ROLE = Qt.ItemDataRole.UserRole + 2
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class SortableItem(QTableWidgetItem):
    """Sorts by the numeric key in SORT_ROLE when both cells have one, else by text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None:
            return float(mine) < float(theirs)
        return super().__lt__(other)


def discover_video_paths(folder: Path, on_error: Callable[[str], None] | None = None) -> list[Path]:
    """Walk every directory depth, including linked directories, without following cycles."""
    pending = [folder.expanduser().resolve()]
    visited_directories: set[str] = set()
    discovered_files: dict[str, Path] = {}
    while pending:
        current = pending.pop()
        directory_key = os.path.normcase(os.path.realpath(current))
        if directory_key in visited_directories:
            continue
        visited_directories.add(directory_key)
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name.casefold(), reverse=True)
        except OSError as exc:
            if on_error is not None:
                on_error(f"Cannot read folder {current}: {exc}")
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=True) and Path(entry.name).suffix.casefold() in VIDEO_EXTENSIONS:
                    path = Path(entry.path).resolve()
                    discovered_files.setdefault(os.path.normcase(os.path.realpath(path)), path)
            except OSError as exc:
                if on_error is not None:
                    on_error(f"Cannot inspect {entry.path}: {exc}")
    return sorted(discovered_files.values(), key=lambda path: str(path).casefold())


def matches_filters(
    video: CompressionVideo,
    *,
    extensions: set[str],
    minimum_size_mb: float = 0.0,
    maximum_size_mb: float = 0.0,
    minimum_duration_minutes: float = 0.0,
    maximum_duration_minutes: float = 0.0,
    assessment: CompressionAssessment | None = None,
    minimum_verdict_rank: int = 0,
) -> bool:
    size_mb = video.size / (1024 * 1024)
    duration_minutes = video.duration / 60.0
    verdict_ok = minimum_verdict_rank <= 0 or (
        assessment is not None and assessment.verdict.rank >= minimum_verdict_rank
    )
    return bool(
        verdict_ok
        and video.path.suffix.casefold() in extensions
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
            paths = discover_video_paths(self.folder, self.warning.emit)
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
                    try:
                        self.item_ready.emit(
                            CompressionVideo(path, path.stat().st_size, 0.0, "metadata unavailable", "—", str(exc))
                        )
                        loaded += 1
                    except OSError:
                        pass
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
    queue_requested = Signal(object, object, object, object, object)
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
            "Encoding creates validated MKV files and keeps every original unless Recycle Bin cleanup is enabled."
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
        self.verdict_filter = QComboBox()
        for label, rank in VERDICT_FILTERS:
            self.verdict_filter.addItem(label, rank)
        self.verdict_filter.setToolTip(
            "Keep only videos whose estimate says a re-encode with the preset chosen below pays off."
        )
        filter_layout.addRow("Worth compressing", self.verdict_filter)
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
        add_builtin_presets(self.preset_combo)
        self.preset_combo.insertSeparator(self.preset_combo.count())
        self.preset_combo.addItem("Custom settings…", "custom")
        self.preset_combo.setItemData(
            self.preset_combo.count() - 1,
            "Your own encoder, quality and speed, set in <b>Custom preset</b> below.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.preset_combo.setCurrentIndex(self.preset_combo.findData("anime_x265_balanced"))
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(preset_help_label(self.preset_combo))
        preset_layout.addLayout(preset_row)

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
        output_group_layout = QVBoxLayout(output_group)
        self.output_layout_combo = QComboBox()
        self.output_layout_combo.addItem("Replace each original in place", OutputLayout.REPLACE.value)
        self.output_layout_combo.addItem("Mirror the folder tree into an output folder", OutputLayout.MIRROR.value)
        self.output_layout_combo.addItem("Beside each source, preset name suffix", OutputLayout.BESIDE.value)
        self.output_layout_combo.currentIndexChanged.connect(self._layout_changed)
        output_group_layout.addWidget(self.output_layout_combo)
        output_row = QHBoxLayout()
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Beside each source")
        self.output_button = QPushButton("Browse…")
        self.output_button.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_dir, 1)
        output_row.addWidget(self.output_button)
        output_group_layout.addLayout(output_row)
        self.output_note = QLabel()
        self.output_note.setWordWrap(True)
        output_group_layout.addWidget(self.output_note)
        layout.addWidget(output_group)

        cleanup_group = QGroupBox("After a validated compression")
        cleanup_layout = QVBoxLayout(cleanup_group)
        self.recycle_originals = QCheckBox("Remove each original")
        self.recycle_originals.setStyleSheet("color: #c62828; font-weight: 600")
        self.recycle_originals.setToolTip(
            "Only a validated output that is actually smaller than its source triggers cleanup."
        )
        self.recycle_originals.toggled.connect(self._cleanup_changed)
        self.cleanup_mode = QComboBox()
        # Store the plain enum *value*: Qt hands user data back across the
        # QVariant boundary as a bare string, so an enum member put in here does
        # not come out as one, and findData() would never match it either.
        self.cleanup_mode.addItem("Quarantine — reversible through the journal", FileAction.QUARANTINE.value)
        self.cleanup_mode.addItem("Operating-system Recycle Bin", FileAction.RECYCLE.value)
        self.cleanup_mode.addItem("Delete permanently — cannot be undone", FileAction.DELETE.value)
        self.cleanup_mode.currentIndexChanged.connect(self._cleanup_changed)
        self.cleanup_note = QLabel()
        self.cleanup_note.setWordWrap(True)
        cleanup_layout.addWidget(self.recycle_originals)
        cleanup_layout.addWidget(self.cleanup_mode)
        cleanup_layout.addWidget(self.cleanup_note)
        layout.addWidget(cleanup_group)
        # Runs last: it reaches back into the cleanup controls built just above.
        self._layout_changed()
        layout.addStretch(1)
        return panel

    def output_layout(self) -> OutputLayout:
        try:
            return OutputLayout(str(self.output_layout_combo.currentData()))
        except ValueError:
            return OutputLayout.BESIDE

    def _layout_changed(self, *_args: object) -> None:
        layout = self.output_layout()
        needs_folder = layout == OutputLayout.MIRROR
        self.output_dir.setEnabled(layout != OutputLayout.REPLACE)
        self.output_button.setEnabled(layout != OutputLayout.REPLACE)
        if layout == OutputLayout.REPLACE:
            # Replacing means the source has to go; there is nowhere else for
            # the encode to live, so cleanup is part of the layout, not a choice.
            self.recycle_originals.setChecked(True)
            self.recycle_originals.setEnabled(False)
            self.output_note.setStyleSheet("color: palette(mid)")
            self.output_note.setText(
                "Each result takes its original's exact path and name. Folder structure is untouched, and "
                "the original is removed as part of the swap — a non-MKV source keeps its name but gains "
                "the .mkv extension."
            )
        else:
            self.recycle_originals.setEnabled(True)
            if needs_folder and not self.output_dir.text().strip():
                self.output_note.setStyleSheet("color: #b06000")
                self.output_note.setText("Choose an output folder — without one, results land beside each source.")
            elif needs_folder:
                self.output_note.setStyleSheet("color: palette(mid)")
                self.output_note.setText(
                    "Subfolders are recreated under the output folder and originals keep their names. "
                    "Point this at another drive to actually free space here."
                )
            else:
                self.output_note.setStyleSheet("color: palette(mid)")
                self.output_note.setText("Results are named <original>.<preset>.mkv so both files sit side by side.")
        self._cleanup_changed()

    def cleanup_action(self) -> FileAction | None:
        """Return the cleanup the user asked for, or None to keep every original."""
        if not self.recycle_originals.isChecked():
            return None
        try:
            return FileAction(str(self.cleanup_mode.currentData()))
        except ValueError:
            return FileAction.QUARANTINE

    def _quarantine_root(self) -> Path:
        window = self.window()
        # `window()` returns the tab itself when it has no parent window, and
        # this method is the one it would find — never call back into it.
        getter = getattr(window, "_quarantine_root", None) if window is not self else None
        if callable(getter):
            root = getter()
            if isinstance(root, Path):
                return root
        return Path.home() / "SameSame Quarantine"

    def _cleanup_changed(self, *_args: object) -> None:
        """Explain, before the queue runs, whether this cleanup will free any space."""
        action = self.cleanup_action()
        self.cleanup_mode.setEnabled(self.recycle_originals.isChecked())
        if action is None:
            self.cleanup_note.setStyleSheet("color: palette(mid)")
            self.cleanup_note.setText("Every original is kept. Outputs are written alongside them.")
            return
        if action == FileAction.DELETE:
            self.cleanup_note.setStyleSheet("color: #c62828; font-weight: 600")
            self.cleanup_note.setText(
                "Originals are erased as each file finishes. Space is released immediately and "
                "there is no restore — not from the journal and not from the Recycle Bin."
            )
            return
        if action == FileAction.RECYCLE:
            self.cleanup_note.setStyleSheet("color: #b06000")
            self.cleanup_note.setText(
                "Space is released only once the Recycle Bin is emptied, and Windows deletes "
                "immediately when the bin is disabled for that drive."
            )
            return
        sources = [video.path for video in self._videos] or [Path(self.folder_path.text().strip() or ".")]
        if cleanup_frees_source_space(action, sources, self._quarantine_root()):
            self.cleanup_note.setStyleSheet("color: palette(mid)")
            self.cleanup_note.setText(
                f"Originals move to {self._quarantine_root()}, on another drive, so this frees space here."
            )
        else:
            self.cleanup_note.setStyleSheet("color: #b06000; font-weight: 600")
            self.cleanup_note.setText(
                f"Quarantine ({self._quarantine_root()}) is on the same drive as these videos, so moving "
                "originals there frees no space at all — the run will end with less free space than it "
                "started. Choose another quarantine folder in Duplicate review, or pick a mode above "
                "that actually releases the bytes."
            )

    def _build_video_panel(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        self.verdict_overview = QLabel()
        self.verdict_overview.setWordWrap(True)
        self.verdict_overview.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.verdict_overview)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ("Use", "Video", "Folder", "Ext", "Size", "Duration", "Codec / resolution", "Bitrate", "Worth compressing?")
        )
        self.table.horizontalHeaderItem(7).setToolTip(
            "Total bitrate and how raw the video is — Raw, Heavy, Moderate or Compact — measured against an "
            "efficient encode in the file's own codec at its resolution and frame rate."
        )
        self.table.horizontalHeaderItem(8).setToolTip(
            "Estimate for the selected preset: the share and amount of space a re-encode would likely free. "
            "Click the header to sort by the expected saving."
        )
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemChanged.connect(self._update_summary)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (3, 4, 5, 6, 7, 8):
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
        # Rows stream in during the load; sorting resumes once it finishes.
        self.table.setSortingEnabled(False)
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
        sorting = self.table.isSortingEnabled()
        # A sorting table would move the row while its cells are being filled.
        self.table.setSortingEnabled(False)
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
        size_item = SortableItem(self._format_size(value.size))
        size_item.setData(SORT_ROLE, value.size)
        self.table.setItem(row, 4, size_item)
        duration_item = SortableItem(self._format_duration(value.duration))
        duration_item.setData(SORT_ROLE, value.duration)
        self.table.setItem(row, 5, duration_item)
        metadata_item = QTableWidgetItem(f"{value.codec} · {value.resolution}")
        if value.probe_error:
            metadata_item.setToolTip(value.probe_error)
            metadata_item.setForeground(Qt.GlobalColor.red)
        self.table.setItem(row, 6, metadata_item)
        self.table.setItem(row, 7, SortableItem())
        self.table.setItem(row, 8, SortableItem())
        # Filling the estimate edits cells already in the table; without this
        # every row would re-run the whole-table summary through itemChanged.
        self.table.blockSignals(True)
        try:
            self._show_assessment(row, value, self._assessment_preset())
        finally:
            self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)
        if not self.is_loading:
            self._update_verdict_overview()

    def _assessment_preset(self) -> TranscodePreset:
        try:
            return self._selected_preset()
        except ValueError:
            # A half-edited custom preset still deserves an estimate.
            return PRESETS["anime_x265_balanced"]

    def _show_assessment(self, row: int, video: CompressionVideo, preset: TranscodePreset) -> None:
        assessment = video.assess(preset)
        self.table.item(row, 0).setData(_ASSESSMENT_ROLE, assessment)
        bitrate_item = self.table.item(row, 7)
        verdict_item = self.table.item(row, 8)
        if assessment is None:
            for item in (bitrate_item, verdict_item):
                item.setText("—")
                item.setData(SORT_ROLE, -1.0)
                item.setBackground(QColor(0, 0, 0, 0))
                item.setToolTip("No estimate: the duration or resolution could not be read.")
            return
        bitrate_item.setText(f"{assessment.total_bps / 1e6:.1f} Mb/s · {assessment.rawness_label}")
        bitrate_item.setData(SORT_ROLE, assessment.total_bps)
        if assessment.verdict == Verdict.SKIP:
            verdict_item.setText(assessment.verdict.label)
        else:
            verdict_item.setText(
                f"{assessment.verdict.label} · −{assessment.saving_fraction * 100:.0f}% "
                f"(≈{self._format_size(assessment.estimated_saving)})"
            )
        verdict_item.setData(SORT_ROLE, float(assessment.estimated_saving))
        tooltip = self._assessment_tooltip(video, assessment, preset)
        for item in (bitrate_item, verdict_item):
            item.setBackground(_VERDICT_TINT[assessment.verdict])
            item.setToolTip(tooltip)

    def _assessment_tooltip(
        self, video: CompressionVideo, assessment: CompressionAssessment, preset: TranscodePreset
    ) -> str:
        reasons = {
            Verdict.WORTH: "The video carries far more data than an efficient encode needs, so a re-encode "
            "frees a lot of space without visible loss.",
            Verdict.MAYBE: "Space can be won, but the file is already encoded sensibly for its codec. "
            "Compressing it again risks banding in gradients and dark scenes — test one episode first.",
            Verdict.SKIP: "Already compact: a re-encode would free little space and only cost quality.",
        }
        lines = [
            f"<b>{assessment.verdict.label}</b> · {assessment.rawness_label} "
            f"({assessment.rawness:.1f}× an efficient {html.escape(video.codec)} encode)",
            reasons[assessment.verdict],
            f"Video now ≈ {assessment.video_bps / 1e6:.1f} Mb/s; {html.escape(preset.name)} typically needs "
            f"≈ {assessment.target_video_bps / 1e6:.1f} Mb/s at {html.escape(video.resolution)}.",
            f"Estimated result ≈ {self._format_size(assessment.estimated_output_size)}, "
            f"saving ≈ {self._format_size(assessment.estimated_saving)}.",
        ]
        if assessment.lossless_audio_bps:
            lines.append(
                f"Lossless audio (≈ {assessment.lossless_audio_bps / 1e3:.0f} kb/s) is copied unchanged and "
                "stays in the output."
            )
        lines.append("<i>An estimate from bitrates, not a measurement.</i>")
        return "<table width=380><tr><td>" + "<br>".join(lines) + "</td></tr></table>"

    def _row_assessment(self, row: int) -> CompressionAssessment | None:
        value = self.table.item(row, 0).data(_ASSESSMENT_ROLE)
        return value if isinstance(value, CompressionAssessment) else None

    def _refresh_assessments(self) -> None:
        preset = self._assessment_preset()
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                video = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                if isinstance(video, CompressionVideo):
                    self._show_assessment(row, video, preset)
        finally:
            self.table.blockSignals(False)
            self.table.setSortingEnabled(sorting)
        self._update_verdict_overview()
        self._update_summary()

    def _update_verdict_overview(self) -> None:
        totals = {verdict: [0, 0, 0] for verdict in Verdict}
        for row in range(self.table.rowCount()):
            video = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            assessment = self._row_assessment(row)
            if assessment is None or not isinstance(video, CompressionVideo):
                continue
            bucket = totals[assessment.verdict]
            bucket[0] += 1
            bucket[1] += video.size
            bucket[2] += assessment.estimated_saving
        if not any(bucket[0] for bucket in totals.values()):
            self.verdict_overview.clear()
            return
        parts = []
        for verdict in Verdict:
            count, size, saving = totals[verdict]
            detail = self._format_size(size)
            if verdict != Verdict.SKIP:
                detail += f", ≈{self._format_size(saving)} to free"
            parts.append(
                f"<span style='color:{_VERDICT_DOT[verdict]}'>●</span> <b>{verdict.label}</b>: {count} ({detail})"
            )
        self.verdict_overview.setText(
            " &nbsp;·&nbsp; ".join(parts) + f" &nbsp;<span style='color:#888888'>— estimated for "
            f"{html.escape(self._assessment_preset().name)}</span>"
        )

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
        self.table.setSortingEnabled(True)
        self._set_loading(False)
        self._update_verdict_overview()
        self._update_summary()
        # Only now are the real source paths known, so the quarantine
        # same-volume verdict can be recomputed against them.
        self._cleanup_changed()

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
                    assessment=self._row_assessment(row),
                    minimum_verdict_rank=int(self.verdict_filter.currentData() or 0),
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
        expected_saving = 0
        for row in range(self.table.rowCount()):
            assessment = self._row_assessment(row)
            if assessment is not None and self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                expected_saving += assessment.estimated_saving
        saving_text = f" · ≈{self._format_size(expected_saving)} expected to free" if selected else ""
        self.summary_label.setText(
            f"{len(self._videos)} video(s) loaded · {len(selected)} checked · "
            f"{self._format_size(selected_size)} selected{saving_text}"
        )
        self.queue_button.setEnabled(not self.is_loading and bool(selected))

    def _preset_changed(self, *_args: object) -> None:
        self.custom_group.setEnabled(self.preset_combo.currentData() == "custom")
        # The estimate depends on the preset's content family and encoder.
        if hasattr(self, "table"):
            self._refresh_assessments()

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
        if hasattr(self, "table") and self.preset_combo.currentData() == "custom":
            self._refresh_assessments()

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
        layout = self.output_layout()
        output_value = self.output_dir.text().strip()
        output_dir = Path(output_value).expanduser() if output_value and layout != OutputLayout.REPLACE else None
        root = Path(self.folder_path.text()).expanduser().resolve()
        self.queue_requested.emit(
            [video.path for video in videos],
            self._selected_preset(),
            output_dir,
            root,
            (self.cleanup_action(), layout),
        )

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
