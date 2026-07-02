from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.gui.result_items import CATEGORY_LABELS, build_review_items, category_counts
from dedupe.models import (
    AudioMatch,
    DedupeReport,
    ExactDuplicateGroup,
    FolderPair,
    ImageMatch,
    NameHint,
    VideoMatch,
)

PYSIDE_AVAILABLE = importlib.util.find_spec("PySide6") is not None


class ResultItemTests(unittest.TestCase):
    def test_all_report_categories_become_review_items(self) -> None:
        report = DedupeReport(
            scanned_files=7,
            exact_duplicates=[ExactDuplicateGroup("hash", ["a", "b"], 10)],
            video_matches=[VideoMatch("c", "d", 91.0, 0.5)],
            image_matches=[ImageMatch("e", "f", 95.0)],
            audio_matches=[AudioMatch("g", "h", 97.0, 0.2)],
            folder_pairs=[FolderPair("left", "right", 80.0, 80.0, 90.0, [], [], [])],
            name_hints=[NameHint("name:key", 100.0, ["i", "j"], "Title")],
        )

        items = build_review_items(report)
        counts = category_counts(items)

        self.assertEqual({item.category for item in items}, set(CATEGORY_LABELS) - {"all"})
        self.assertEqual(counts["all"], 6)
        self.assertTrue(all(counts[category] == 1 for category in CATEGORY_LABELS if category != "all"))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 GUI extra is not installed")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])

    def test_main_window_constructs_with_all_filters(self) -> None:
        from dedupe.gui.main_window import MainWindow

        window = MainWindow()
        try:
            self.assertEqual(window.category_filter.count(), len(CATEGORY_LABELS))
            self.assertIn("transcoding", window.windowTitle())
            self.assertFalse(window.cancel_button.isEnabled())
            self.assertEqual(window.centralWidget().count(), 3)
            self.assertEqual(window.centralWidget().tabText(1), "Video compression")
            self.assertEqual(window.centralWidget().tabText(2), "Folder consolidation")
        finally:
            window.close()
            self.application.processEvents()

    def test_folder_consolidation_tab_shows_folder_mapping_and_file_preview(self) -> None:
        from dedupe.gui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "[F0001] Title"
            left = root / "folder1" / "Title"
            right = root / "folder2" / "Translated Title"
            left.mkdir(parents=True)
            right.mkdir(parents=True)
            (left / "01.mkv").write_bytes(b"one")
            (right / "02.mkv").write_bytes(b"two")
            window = MainWindow()
            window._journal_path = lambda: base / "operations.sqlite3"  # type: ignore[method-assign]
            try:
                tab = window.consolidation_tab
                tab.title_path.setText(str(root))
                tab._analyze()

                self.assertEqual(tab.final_name.text(), "Title")
                self.assertEqual(tab.mapping_table.rowCount(), 2)
                self.assertEqual(tab.preview_table.rowCount(), 2)
                self.assertTrue(tab.execute_button.isEnabled())
                destinations = {
                    tab.preview_table.item(row, 2).text() for row in range(tab.preview_table.rowCount())
                }
                self.assertEqual(destinations, {str(base / "Title" / "01.mkv"), str(base / "Title" / "02.mkv")})
            finally:
                window.close()
                self.application.processEvents()

    def test_compression_filters_and_custom_preset(self) -> None:
        from dedupe.gui.compression_tab import (
            CompressionVideo,
            build_custom_preset,
            discover_video_paths,
            matches_filters,
        )

        video = CompressionVideo(Path("episode.mkv"), 800 * 1024 * 1024, 25 * 60, "h264", "1280×720")
        self.assertTrue(
            matches_filters(
                video,
                extensions={".mkv"},
                minimum_size_mb=700,
                minimum_duration_minutes=20,
            )
        )
        self.assertFalse(
            matches_filters(
                video,
                extensions={".mp4"},
                minimum_size_mb=700,
                minimum_duration_minutes=20,
            )
        )
        preset = build_custom_preset(
            "libx265",
            19,
            "slower",
            "yuv420p10le",
            x265_params="no-sao=1:aq-mode=3",
        )
        self.assertEqual(preset.encoder, "libx265")
        self.assertIn("-crf", preset.video_args)
        self.assertIn("19", preset.video_args)
        self.assertTrue(preset.preset_id.startswith("custom_libx265_crf19_slower_"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deepest = root.joinpath(*(f"level-{index}" for index in range(10)))
            deepest.mkdir(parents=True)
            first = deepest / "deep-episode.mkv"
            second = deepest / "legacy-episode.vob"
            ignored = deepest / "notes.txt"
            first.write_bytes(b"video")
            second.write_bytes(b"video")
            ignored.write_text("not video", encoding="utf-8")
            self.assertEqual(discover_video_paths(root), [first.resolve(), second.resolve()])

    def test_compression_tab_loads_folder_metadata_in_background(self) -> None:
        from unittest.mock import patch

        from PySide6.QtCore import QEventLoop, QTimer, Qt

        from dedupe.gui.main_window import MainWindow
        from dedupe.transcode.models import MediaInfo, StreamInfo
        from dedupe.transcode.probe import ProbeError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "episode.mkv"
            nested = root / "season" / "extras" / "deep"
            nested.mkdir(parents=True)
            unreadable_metadata = nested / "legacy.vob"
            source.write_bytes(b"video")
            unreadable_metadata.write_bytes(b"legacy video")
            info = MediaInfo(source, source.stat().st_size, 25 * 60, "matroska", (StreamInfo(0, "video", "h264", 1280, 720),))
            window = MainWindow()
            window.compression_tab.folder_path.setText(str(root))

            def fake_probe(path: Path, _ffprobe: str) -> MediaInfo:
                if path == unreadable_metadata.resolve():
                    raise ProbeError("metadata test failure")
                return info

            with patch("dedupe.gui.compression_tab.probe_media", side_effect=fake_probe):
                window.compression_tab._load_folder()
                event_loop = QEventLoop()
                poll = QTimer()
                poll.timeout.connect(
                    lambda: event_loop.quit() if not window.compression_tab.is_loading else None
                )
                timeout = QTimer()
                timeout.setSingleShot(True)
                timeout.timeout.connect(event_loop.quit)
                poll.start(5)
                timeout.start(5_000)
                event_loop.exec()
                poll.stop()
                self.assertTrue(timeout.isActive(), "compression folder loading timed out")
                timeout.stop()

            try:
                self.assertEqual(window.compression_tab.table.rowCount(), 2)
                self.assertTrue(
                    any(
                        window.compression_tab.table.item(row, 6).toolTip()
                        for row in range(window.compression_tab.table.rowCount())
                    )
                )
                window.compression_tab._select_matching()
                self.assertTrue(
                    all(
                        window.compression_tab.table.item(row, 0).checkState() == Qt.CheckState.Checked
                        for row in range(window.compression_tab.table.rowCount())
                    )
                )
                self.assertTrue(window.compression_tab.queue_button.isEnabled())
            finally:
                window.close()
                self.application.processEvents()

    def test_name_hints_allow_review_decisions_but_disable_file_mutation(self) -> None:
        from dedupe.gui.main_window import MainWindow
        from dedupe.metadata import basic_media_metadata
        from dedupe.models import FileRecord
        from dedupe.service import ScanResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "episode-a.bin"
            right = root / "episode-b.bin"
            left.write_bytes(b"left")
            right.write_bytes(b"right")
            records = []
            for path in (left, right):
                stat = path.stat()
                records.append(FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem))
            report = DedupeReport(2, [], [], [], [], [], [NameHint("name:test", 95.0, [str(left), str(right)], "Episode")])
            result = ScanResult(report, records, {record.path_key: basic_media_metadata(record) for record in records})
            window = MainWindow()
            window._scan_completed(result)
            self.application.processEvents()

            try:
                self.assertTrue(window.comparison.keep_button.isEnabled())
                self.assertTrue(window.comparison.ignore_button.isEnabled())
                self.assertFalse(window.comparison.quarantine_button.isEnabled())
                self.assertFalse(window.comparison.recycle_button.isEnabled())
                self.assertFalse(window.comparison.batch_quarantine_button.isEnabled())
                self.assertFalse(window.comparison.batch_quarantine_checked_button.isEnabled())
                self.assertFalse(window.comparison.batch_recycle_checked_button.isEnabled())
            finally:
                window.close()
                self.application.processEvents()

    def test_video_review_enables_transcode_queue_action(self) -> None:
        from unittest.mock import patch

        from PySide6.QtWidgets import QMessageBox

        from dedupe.actions import FileAction
        from dedupe.gui.main_window import MainWindow
        from dedupe.metadata import basic_media_metadata
        from dedupe.models import FileRecord
        from dedupe.service import ScanResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "episode-a.mkv"
            right = root / "episode-b.mkv"
            left.write_bytes(b"left-video")
            right.write_bytes(b"right-video")
            records = []
            for path in (left, right):
                stat = path.stat()
                records.append(FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem))
            report = DedupeReport(2, [], [VideoMatch(str(left), str(right), 95.0, 0.0)], [], [], [], [])
            result = ScanResult(report, records, {record.path_key: basic_media_metadata(record) for record in records})
            window = MainWindow()
            window._scan_completed(result)
            self.application.processEvents()

            try:
                self.assertTrue(window.comparison.transcode_button.isEnabled())
                self.assertFalse(window.allow_unsafe_recycle.isChecked())
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    self.assertEqual(window._resolve_recycle_action(FileAction.RECYCLE), FileAction.QUARANTINE)
                window.comparison.release_paths([left])
                self.application.processEvents()
                self.assertEqual(window.comparison.left.current_path, "")
            finally:
                window.close()
                self.application.processEvents()

    def test_transcode_dialog_displays_capability_and_before_after_metadata(self) -> None:
        from unittest.mock import patch

        from PySide6.QtCore import QEventLoop, QTimer

        from dedupe.gui.transcode_dialog import TranscodeDialog
        from dedupe.transcode.models import (
            EncoderCapability,
            JobStatus,
            MediaInfo,
            StreamInfo,
            TranscodeResult,
            ValidationResult,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "episode.mkv"
            output = root / "episode.encoded.mkv"
            source.write_bytes(b"source-video")
            output.write_bytes(b"encoded")
            with patch(
                "dedupe.gui.worker.check_encoder_capability",
                return_value=EncoderCapability("libx265", True, True),
            ):
                dialog = TranscodeDialog(
                    [source],
                    ffmpeg="ffmpeg",
                    ffprobe="ffprobe",
                    journal_path=root / "journal.sqlite3",
                    quarantine_root=root / "quarantine",
                )
                event_loop = QEventLoop()
                poll = QTimer()
                poll.timeout.connect(lambda: event_loop.quit() if dialog._capability_thread is None else None)
                timeout = QTimer()
                timeout.setSingleShot(True)
                timeout.timeout.connect(event_loop.quit)
                poll.start(5)
                timeout.start(5_000)
                event_loop.exec()
                poll.stop()
                self.assertTrue(timeout.isActive(), "capability check timed out")
                timeout.stop()

            input_info = MediaInfo(source, 12, 60.0, "matroska", (StreamInfo(0, "video", "h264"),))
            output_info = MediaInfo(output, 7, 60.0, "matroska", (StreamInfo(0, "video", "hevc"),))
            result = TranscodeResult(
                "job",
                JobStatus.COMPLETED,
                source,
                output,
                root / "encode.log",
                "anime_x265_balanced",
                input_size=12,
                output_size=7,
                validation=ValidationResult(True, output_info=output_info),
                input_info=input_info,
            )
            dialog._transcode_result(result)
            dialog.table.selectRow(0)
            self.application.processEvents()

            try:
                self.assertIn("Available: libx265", dialog.capability_label.text())
                self.assertIn("Input:", dialog.details.toPlainText())
                self.assertIn("Output:", dialog.details.toPlainText())
                self.assertTrue(dialog.promote_button.isEnabled())
            finally:
                dialog.close()
                self.application.processEvents()

    def test_background_gui_transcode_queue_completes_without_blocking(self) -> None:
        ffmpeg = shutil.which(os.environ.get("SAMESAME_TEST_FFMPEG", "ffmpeg"))
        ffprobe = shutil.which(os.environ.get("SAMESAME_TEST_FFPROBE", "ffprobe"))
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg and ffprobe are required")

        from PySide6.QtCore import QEventLoop, QTimer

        from dedupe.gui.transcode_dialog import TranscodeDialog
        from dedupe.transcode.models import JobStatus

        def wait_until(predicate: object, timeout_ms: int = 15_000) -> None:
            event_loop = QEventLoop()
            poll = QTimer()
            poll.timeout.connect(lambda: event_loop.quit() if predicate() else None)  # type: ignore[operator]
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(event_loop.quit)
            poll.start(10)
            timeout.start(timeout_ms)
            event_loop.exec()
            poll.stop()
            self.assertTrue(timeout.isActive(), "background GUI operation timed out")
            timeout.stop()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "gui-source.mkv"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=96x64:rate=8:duration=0.5",
                    "-c:v",
                    "mpeg4",
                    str(source),
                ],
                check=True,
                timeout=30,
            )
            dialog = TranscodeDialog(
                [source],
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                journal_path=root / "journal.sqlite3",
                quarantine_root=root / "quarantine",
            )
            wait_until(lambda: dialog._capability_thread is None)
            self.assertTrue(dialog.start_button.isEnabled())
            dialog._start_queue()
            self.assertIsNotNone(dialog._queue_thread)
            wait_until(lambda: dialog._queue_thread is None)

            try:
                result = dialog._results[str(source.resolve())]
                self.assertEqual(result.status, JobStatus.COMPLETED, result.message)
                self.assertTrue(source.exists())
                self.assertTrue(result.output_path.exists())
                self.assertEqual(dialog.table.item(0, 2).text(), "Completed")
            finally:
                dialog.close()
                self.application.processEvents()

    def test_background_scan_populates_results_without_blocking_ui(self) -> None:
        from PIL import Image
        from PySide6.QtCore import QEventLoop, QTimer

        from dedupe.gui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "copy-a.png"
            copy = root / "copy-b.png"
            Image.new("RGB", (32, 24), (30, 60, 90)).save(source)
            copy.write_bytes(source.read_bytes())
            window = MainWindow()
            window.folder_list.addItem(str(root))
            window.skip_video.setChecked(True)
            window.skip_images.setChecked(True)
            window.skip_audio.setChecked(True)
            window._cache_path = lambda: root / "cache.sqlite3"  # type: ignore[method-assign]

            window._start_scan()
            self.assertIsNotNone(window._scan_thread)
            event_loop = QEventLoop()
            if window._scan_thread is not None:
                window._scan_thread.finished.connect(event_loop.quit)
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(event_loop.quit)
            timeout.start(10_000)
            event_loop.exec()
            self.application.processEvents()

            try:
                self.assertTrue(timeout.isActive(), "GUI scan timed out")
                self.assertIsNotNone(window._result)
                self.assertEqual(window._result.report.scanned_files, 2)  # type: ignore[union-attr]
                self.assertEqual(len(window._result.report.exact_duplicates), 1)  # type: ignore[union-attr]
                self.assertEqual(window.result_list.count(), 1)
                self.assertTrue(window.export_button.isEnabled())
            finally:
                timeout.stop()
                window.close()
                self.application.processEvents()

    def test_background_quarantine_and_restore_update_the_journal(self) -> None:
        from PIL import Image
        from PySide6.QtCore import QEventLoop, QTimer, Qt

        from dedupe.actions import FileAction, FileActionService, OperationStatus
        from dedupe.gui.journal_dialog import JournalDialog
        from dedupe.gui.main_window import MainWindow
        from dedupe.metadata import basic_media_metadata
        from dedupe.models import FileRecord
        from dedupe.service import ScanResult

        def wait_for_action(window: MainWindow) -> None:
            event_loop = QEventLoop()
            poll = QTimer()
            poll.timeout.connect(lambda: event_loop.quit() if window._action_thread is None else None)
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(event_loop.quit)
            poll.start(10)
            timeout.start(10_000)
            event_loop.exec()
            poll.stop()
            self.assertTrue(timeout.isActive(), "GUI file action timed out")
            timeout.stop()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "copy-a.png"
            right = root / "copy-b.png"
            third = root / "copy-c.png"
            Image.new("RGB", (24, 24), (80, 40, 20)).save(left)
            right.write_bytes(left.read_bytes())
            third.write_bytes(left.read_bytes())
            records = []
            for path in (left, right, third):
                stat = path.stat()
                records.append(FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem))
            paths = (str(left), str(right), str(third))
            report = DedupeReport(3, [ExactDuplicateGroup("hash", list(paths), records[0].size)], [], [], [], [], [])
            result = ScanResult(report, records, {record.path_key: basic_media_metadata(record) for record in records})
            journal_path = root / "operations.sqlite3"
            quarantine_root = root / "quarantine"
            window = MainWindow()
            window._journal_path = lambda: journal_path  # type: ignore[method-assign]
            window.quarantine_path.setText(str(quarantine_root))
            window._scan_completed(result)

            window._confirm_action = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
            self.assertEqual(window.comparison.batch_file_list.count(), 3)
            for index in range(window.comparison.batch_file_list.count()):
                checkbox = window.comparison.batch_file_list.item(index)
                if checkbox.data(Qt.ItemDataRole.UserRole) in {str(right), str(third)}:
                    checkbox.setCheckState(Qt.CheckState.Checked)
            self.assertTrue(window.comparison.batch_quarantine_checked_button.isEnabled())
            window._request_batch_action((str(right), str(third)), FileAction.QUARANTINE)
            wait_for_action(window)

            self.assertFalse(right.exists())
            self.assertFalse(third.exists())
            self.assertTrue(left.exists())
            operations = FileActionService(journal_path, quarantine_root).recent_operations()
            self.assertEqual(len(operations), 2)
            self.assertEqual(operations[0].status, OperationStatus.COMPLETED)
            dialog = JournalDialog(journal_path, quarantine_root)
            dialog.table.selectRow(0)
            self.application.processEvents()
            self.assertTrue(dialog.restore_button.isEnabled())

            window._start_action_worker(restore_operation_id=operations[0].operation_id)
            wait_for_action(window)

            try:
                self.assertTrue(operations[0].source.exists())
                self.assertEqual(operations[0].source.read_bytes(), left.read_bytes())
                self.assertEqual(len(FileActionService(journal_path, quarantine_root).recent_operations()), 3)
            finally:
                dialog.close()
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
