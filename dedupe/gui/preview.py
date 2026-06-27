from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..actions import FileAction
from ..metadata import MediaMetadata, basic_media_metadata
from ..models import FileRecord
from ..scanner import is_audio_path, is_image_path, is_video_path
from ..service import ScanResult
from .result_items import ReviewItem
from .worker import MetadataTask


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _duration(value: float | None) -> str:
    if value is None:
        return "unknown"
    hours, remainder = divmod(int(round(value)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_metadata(metadata: MediaMetadata) -> str:
    lines = [
        f"Type: {metadata.media_type}",
        f"Size: {_human_size(metadata.size)}",
        f"Modified: {datetime.fromtimestamp(metadata.modified_at).isoformat(sep=' ', timespec='seconds')}",
    ]
    if metadata.duration is not None:
        lines.append(f"Duration: {_duration(metadata.duration)}")
    if metadata.container:
        lines.append(f"Container: {metadata.container}")
    if metadata.bit_rate is not None:
        lines.append(f"Bit rate: {metadata.bit_rate / 1000:.0f} kb/s")
    if metadata.chapter_count:
        lines.append(f"Chapters: {metadata.chapter_count}")
    if metadata.attachment_count:
        lines.append(f"Attachments: {metadata.attachment_count}")
    for stream in metadata.streams:
        details = [stream.codec or "unknown codec"]
        if stream.width and stream.height:
            details.append(f"{stream.width}×{stream.height}")
        if stream.frame_rate:
            details.append(f"{stream.frame_rate:.3f} fps")
        if stream.pixel_format:
            details.append(stream.pixel_format)
        if stream.channels:
            details.append(f"{stream.channels} ch")
        if stream.sample_rate:
            details.append(f"{stream.sample_rate} Hz")
        if stream.language:
            details.append(stream.language)
        if stream.title:
            details.append(stream.title)
        if stream.is_default:
            details.append("default")
        if stream.is_forced:
            details.append("forced")
        lines.append(f"Stream {stream.index} [{stream.kind}]: " + " · ".join(details))
    if metadata.error:
        lines.append(f"Metadata warning: {metadata.error}")
    return "\n".join(lines)


class MediaPane(QWidget):
    position_changed = Signal(int)
    path_changed = Signal(str)

    def __init__(self, title: str, parent: QWidget | None = None, *, muted: bool = False) -> None:
        super().__init__(parent)
        self._records: dict[str, FileRecord] = {}
        self._ffprobe = "ffprobe"
        self._thread_pool: QThreadPool | None = None
        self._current_path = ""
        self._metadata_tasks: dict[str, MetadataTask] = {}

        self.title_label = QLabel(f"<b>{title}</b>")
        self.path_combo = QComboBox()
        self.path_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.path_combo.setMinimumContentsLength(24)
        self.path_combo.currentTextChanged.connect(self._load_path)

        self.placeholder = QLabel("Select a result to preview")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(240, 180)
        self.image_label.setScaledContents(False)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(240, 180)
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.placeholder)
        self.preview_stack.addWidget(self.image_label)
        self.preview_stack.addWidget(self.video_widget)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.5)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._position_updated)
        self.player.durationChanged.connect(self._duration_updated)
        self.player.playbackStateChanged.connect(self._playback_state_changed)

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)
        self.mute_checkbox = QCheckBox("Mute")
        self.mute_checkbox.setChecked(muted)
        self.audio_output.setMuted(muted)
        self.mute_checkbox.toggled.connect(self.audio_output.setMuted)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00 / 00:00")
        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.mute_checkbox)
        controls.addWidget(self.timeline, 1)
        controls.addWidget(self.time_label)

        self.metadata_view = QPlainTextEdit()
        self.metadata_view.setReadOnly(True)
        self.metadata_view.setMinimumHeight(145)
        self.open_file_button = QPushButton("Open file")
        self.open_folder_button = QPushButton("Open folder")
        self.open_file_button.clicked.connect(self.open_file)
        self.open_folder_button.clicked.connect(self.open_folder)
        open_controls = QHBoxLayout()
        open_controls.addWidget(self.open_file_button)
        open_controls.addWidget(self.open_folder_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.path_combo)
        layout.addWidget(self.preview_stack, 1)
        layout.addLayout(controls)
        layout.addWidget(self.metadata_view)
        layout.addLayout(open_controls)
        self.clear()

    def configure(self, result: ScanResult, ffprobe: str, thread_pool: QThreadPool) -> None:
        self._records = {record.path_key: record for record in result.records}
        self._ffprobe = ffprobe
        self._thread_pool = thread_pool

    def set_candidates(self, paths: tuple[str, ...], selected: int) -> None:
        self.path_combo.blockSignals(True)
        self.path_combo.clear()
        self.path_combo.addItems(paths)
        if paths:
            self.path_combo.setCurrentIndex(min(selected, len(paths) - 1))
        self.path_combo.blockSignals(False)
        self._load_path(self.path_combo.currentText())

    def clear(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self._current_path = ""
        self.path_combo.clear()
        self.placeholder.setText("Select a result to preview")
        self.preview_stack.setCurrentWidget(self.placeholder)
        self.metadata_view.clear()
        self.play_button.setEnabled(False)
        self.timeline.setEnabled(False)
        self.open_file_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)

    @property
    def current_path(self) -> str:
        return self._current_path

    def _load_path(self, path_text: str) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self._current_path = path_text
        self.path_changed.emit(path_text)
        path = Path(path_text) if path_text else None
        exists = bool(path and path.exists())
        self.open_file_button.setEnabled(bool(exists and path and path.is_file()))
        self.open_folder_button.setEnabled(exists)
        self.play_button.setEnabled(False)
        self.timeline.setEnabled(False)
        if not path:
            self.preview_stack.setCurrentWidget(self.placeholder)
            self.metadata_view.clear()
            return
        if path.is_dir():
            self.placeholder.setText("Folder comparison\nNo media preview available")
            self.preview_stack.setCurrentWidget(self.placeholder)
            self.metadata_view.setPlainText(str(path))
            return
        if not path.exists():
            self.placeholder.setText("File no longer exists")
            self.preview_stack.setCurrentWidget(self.placeholder)
            self.metadata_view.setPlainText(str(path))
            return

        record = self._records.get(path_text)
        if record is not None:
            self.metadata_view.setPlainText(format_metadata(basic_media_metadata(record)))
            self._request_metadata(record)
        else:
            stat = path.stat()
            record = FileRecord(path, path.parent, stat.st_size, stat.st_mtime, path.stem)
            self.metadata_view.setPlainText(format_metadata(basic_media_metadata(record)))

        if is_image_path(path):
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                self.placeholder.setText(f"Cannot preview image\n{reader.errorString()}")
                self.preview_stack.setCurrentWidget(self.placeholder)
            else:
                pixmap = QPixmap.fromImage(image)
                self.image_label.setPixmap(
                    pixmap.scaled(800, 500, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
                self.preview_stack.setCurrentWidget(self.image_label)
        elif is_video_path(path) or is_audio_path(path):
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.play_button.setEnabled(True)
            self.timeline.setEnabled(True)
            if is_video_path(path):
                self.preview_stack.setCurrentWidget(self.video_widget)
            else:
                self.placeholder.setText("Audio preview")
                self.preview_stack.setCurrentWidget(self.placeholder)
        else:
            self.placeholder.setText("Preview is not available for this file type")
            self.preview_stack.setCurrentWidget(self.placeholder)

    def _request_metadata(self, record: FileRecord) -> None:
        if self._thread_pool is None:
            return
        task = MetadataTask(record, self._ffprobe)
        self._metadata_tasks[record.path_key] = task
        task.signals.completed.connect(self._metadata_ready)
        self._thread_pool.start(task)

    def _metadata_ready(self, path: str, metadata: MediaMetadata) -> None:
        self._metadata_tasks.pop(path, None)
        if path == self._current_path:
            self.metadata_view.setPlainText(format_metadata(metadata))

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _position_updated(self, position: int) -> None:
        if not self.timeline.isSliderDown():
            self.timeline.setValue(position)
        self.time_label.setText(f"{_duration(position / 1000)} / {_duration(self.player.duration() / 1000)}")
        self.position_changed.emit(position)

    def _duration_updated(self, duration: int) -> None:
        self.timeline.setRange(0, max(0, duration))
        self.time_label.setText(f"{_duration(self.player.position() / 1000)} / {_duration(duration / 1000)}")

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def open_file(self) -> None:
        if self._current_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._current_path))

    def open_folder(self) -> None:
        if not self._current_path:
            return
        path = Path(self._current_path)
        target = path if path.is_dir() else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


class ComparisonWidget(QWidget):
    action_requested = Signal(object, str)
    batch_quarantine_requested = Signal(object, str)
    batch_action_requested = Signal(object, object)
    transcode_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)
        self.left = MediaPane("Left", self)
        self.right = MediaPane("Right", self, muted=True)
        self.left.position_changed.connect(lambda position: self._sync(self.left, self.right, position))
        self.right.position_changed.connect(lambda position: self._sync(self.right, self.left, position))
        self.left.path_changed.connect(self._update_action_controls)
        self.right.path_changed.connect(self._update_action_controls)
        self.sync_checkbox = QCheckBox("Synchronize seeking")
        self.sync_checkbox.setChecked(True)
        self.play_both_button = QPushButton("Play/Pause both")
        self.play_both_button.clicked.connect(self._play_both)
        self.evidence_label = QLabel("Select a result")
        self.evidence_label.setWordWrap(True)
        header = QHBoxLayout()
        header.addWidget(self.evidence_label, 1)
        header.addWidget(self.sync_checkbox)
        header.addWidget(self.play_both_button)

        self.action_target = QComboBox()
        self.action_target.addItem("Left selected file", "left")
        self.action_target.addItem("Right selected file", "right")
        self.action_target.currentIndexChanged.connect(self._update_action_controls)
        self.keep_button = QPushButton("Keep")
        self.ignore_button = QPushButton("Ignore")
        self.quarantine_button = QPushButton("Quarantine…")
        self.recycle_button = QPushButton("Recycle…")
        self.batch_quarantine_button = QPushButton("Quarantine other copies…")
        self.transcode_button = QPushButton("Transcode videos…")
        self.keep_button.clicked.connect(lambda: self._emit_action(FileAction.KEEP))
        self.ignore_button.clicked.connect(lambda: self._emit_action(FileAction.IGNORE))
        self.quarantine_button.clicked.connect(lambda: self._emit_action(FileAction.QUARANTINE))
        self.recycle_button.clicked.connect(lambda: self._emit_action(FileAction.RECYCLE))
        self.batch_quarantine_button.clicked.connect(self._emit_batch_quarantine)
        self.transcode_button.clicked.connect(self._emit_transcode)
        self.decision_label = QLabel("No review decision")
        review_actions = QHBoxLayout()
        review_actions.addWidget(self.action_target)
        review_actions.addWidget(self.keep_button)
        review_actions.addWidget(self.ignore_button)
        review_actions.addWidget(self.decision_label, 1)
        file_actions = QHBoxLayout()
        file_actions.addWidget(self.quarantine_button)
        file_actions.addWidget(self.recycle_button)
        file_actions.addWidget(self.batch_quarantine_button)
        file_actions.addWidget(self.transcode_button)
        file_actions.addStretch(1)

        batch_group = QGroupBox("Batch cleanup — check files you do not want")
        batch_layout = QVBoxLayout(batch_group)
        self.batch_file_list = QListWidget()
        self.batch_file_list.setMaximumHeight(105)
        self.batch_file_list.itemChanged.connect(self._update_action_controls)
        batch_layout.addWidget(self.batch_file_list)
        batch_controls = QHBoxLayout()
        self.check_others_button = QPushButton("Check all except current")
        self.clear_checked_button = QPushButton("Clear checks")
        self.batch_quarantine_checked_button = QPushButton("Quarantine checked…")
        self.batch_recycle_checked_button = QPushButton("Recycle checked…")
        self.batch_recycle_checked_button.setStyleSheet("color: #c62828; font-weight: 600")
        self.check_others_button.clicked.connect(self._check_all_except_current)
        self.clear_checked_button.clicked.connect(self._clear_checked)
        self.batch_quarantine_checked_button.clicked.connect(
            lambda: self._emit_batch_action(FileAction.QUARANTINE)
        )
        self.batch_recycle_checked_button.clicked.connect(lambda: self._emit_batch_action(FileAction.RECYCLE))
        batch_controls.addWidget(self.check_others_button)
        batch_controls.addWidget(self.clear_checked_button)
        batch_controls.addStretch(1)
        batch_controls.addWidget(self.batch_quarantine_checked_button)
        batch_controls.addWidget(self.batch_recycle_checked_button)
        batch_layout.addLayout(batch_controls)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setSizes([500, 500])
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(review_actions)
        layout.addLayout(file_actions)
        layout.addWidget(batch_group)
        layout.addWidget(splitter, 1)
        self._syncing = False
        self._item: ReviewItem | None = None
        self._actions_enabled = True
        self._update_action_controls()

    def configure(self, result: ScanResult, ffprobe: str) -> None:
        self.left.configure(result, ffprobe, self.thread_pool)
        self.right.configure(result, ffprobe, self.thread_pool)

    def set_item(self, item: ReviewItem) -> None:
        self._item = item
        self.evidence_label.setText(f"<b>{item.title}</b><br>{item.evidence}")
        self.decision_label.setText("No review decision")
        self.left.set_candidates(item.paths, 0)
        self.right.set_candidates(item.paths, 1)
        self._populate_batch_files(item.paths)
        self._update_action_controls()

    def clear(self) -> None:
        self._item = None
        self.evidence_label.setText("Select a result")
        self.decision_label.setText("No review decision")
        self.left.clear()
        self.right.clear()
        self.batch_file_list.clear()
        self._update_action_controls()

    def set_decision(self, message: str) -> None:
        self.decision_label.setText(message)

    def set_actions_enabled(self, enabled: bool) -> None:
        self._actions_enabled = enabled
        self._update_action_controls()

    def stop(self) -> None:
        self.left.player.stop()
        self.right.player.stop()
        self.thread_pool.waitForDone(2000)

    def _sync(self, source: MediaPane, target: MediaPane, position: int) -> None:
        if self._syncing or not self.sync_checkbox.isChecked() or target.player.duration() <= 0:
            return
        if abs(target.player.position() - position) < 400:
            return
        self._syncing = True
        target.player.setPosition(min(position, target.player.duration()))
        self._syncing = False

    def _play_both(self) -> None:
        players = (self.left.player, self.right.player)
        if all(player.playbackState() == QMediaPlayer.PlaybackState.PlayingState for player in players):
            for player in players:
                player.pause()
        else:
            target_position = min(player.position() for player in players)
            for player in players:
                if player.source().isValid():
                    player.setPosition(target_position)
                    player.play()

    def _selected_path(self) -> str:
        return self.left.current_path if self.action_target.currentData() == "left" else self.right.current_path

    def _update_action_controls(self, *_args: object) -> None:
        has_item = self._item is not None and self._actions_enabled
        selected_path = Path(self._selected_path()) if self._selected_path() else None
        content_backed = bool(self._item and self._item.category in {"exact", "video", "image", "audio"})
        can_mutate = bool(content_backed and selected_path and selected_path.is_file())
        self.keep_button.setEnabled(has_item)
        self.ignore_button.setEnabled(has_item)
        self.quarantine_button.setEnabled(can_mutate)
        self.recycle_button.setEnabled(can_mutate)
        self.batch_quarantine_button.setEnabled(
            bool(self._item and self._item.category == "exact" and len(self._item.paths) > 1 and can_mutate)
        )
        self.transcode_button.setEnabled(
            bool(
                has_item
                and self._item
                and any(is_video_path(Path(path)) and Path(path).is_file() for path in self._item.paths)
            )
        )
        checked_paths = self._checked_batch_paths()
        can_batch_mutate = bool(
            content_backed and self._actions_enabled and any(Path(path).is_file() for path in checked_paths)
        )
        self.batch_file_list.setEnabled(bool(content_backed and self._actions_enabled))
        self.check_others_button.setEnabled(bool(content_backed and self._actions_enabled))
        self.clear_checked_button.setEnabled(bool(checked_paths and self._actions_enabled))
        self.batch_quarantine_checked_button.setEnabled(can_batch_mutate)
        self.batch_recycle_checked_button.setEnabled(can_batch_mutate)

    def _populate_batch_files(self, paths: tuple[str, ...]) -> None:
        self.batch_file_list.blockSignals(True)
        self.batch_file_list.clear()
        for path_text in paths:
            path = Path(path_text)
            item = QListWidgetItem(path.name or str(path))
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.batch_file_list.addItem(item)
        self.batch_file_list.blockSignals(False)

    def _checked_batch_paths(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.batch_file_list.count())
            for item in [self.batch_file_list.item(index)]
            if item.checkState() == Qt.CheckState.Checked
        )

    def _check_all_except_current(self) -> None:
        current = self._selected_path()
        self.batch_file_list.blockSignals(True)
        try:
            for index in range(self.batch_file_list.count()):
                item = self.batch_file_list.item(index)
                path = str(item.data(Qt.ItemDataRole.UserRole))
                item.setCheckState(Qt.CheckState.Unchecked if path == current else Qt.CheckState.Checked)
        finally:
            self.batch_file_list.blockSignals(False)
        self._update_action_controls()

    def _clear_checked(self) -> None:
        self.batch_file_list.blockSignals(True)
        try:
            for index in range(self.batch_file_list.count()):
                self.batch_file_list.item(index).setCheckState(Qt.CheckState.Unchecked)
        finally:
            self.batch_file_list.blockSignals(False)
        self._update_action_controls()

    def _emit_action(self, action: FileAction) -> None:
        path = self._selected_path()
        if self._item is not None and path:
            self.action_requested.emit(action, path)

    def _emit_batch_quarantine(self) -> None:
        if self._item is not None:
            self.batch_quarantine_requested.emit(self._item.paths, self._selected_path())

    def _emit_batch_action(self, action: FileAction) -> None:
        paths = self._checked_batch_paths()
        if self._item is not None and paths:
            self.batch_action_requested.emit(paths, action)

    def _emit_transcode(self) -> None:
        if self._item is not None:
            paths = tuple(path for path in self._item.paths if is_video_path(Path(path)) and Path(path).is_file())
            if paths:
                self.transcode_requested.emit(paths)
