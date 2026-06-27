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


if __name__ == "__main__":
    unittest.main()
