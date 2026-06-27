from __future__ import annotations

import importlib.util
import os
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
            self.assertIn("Read-only", window.windowTitle())
            self.assertFalse(window.cancel_button.isEnabled())
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
            finally:
                window.close()
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
        from PySide6.QtCore import QEventLoop, QTimer

        from dedupe.actions import FileActionService, OperationStatus
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
            window._request_batch_quarantine(paths, str(left))
            wait_for_action(window)

            self.assertFalse(right.exists())
            self.assertFalse(third.exists())
            self.assertTrue(left.exists())
            operations = FileActionService(journal_path, quarantine_root).recent_operations()
            self.assertEqual(len(operations), 3)
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
                self.assertEqual(len(FileActionService(journal_path, quarantine_root).recent_operations()), 4)
            finally:
                dialog.close()
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
