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
        # "k"/"l" are matched by both video and audio evidence, so they form a
        # mixed-evidence ("content") cluster distinct from the pure video and
        # audio clusters below (F-23).
        report = DedupeReport(
            scanned_files=9,
            exact_duplicates=[ExactDuplicateGroup("hash", ["a", "b"], 10)],
            video_matches=[VideoMatch("c", "d", 91.0, 0.5), VideoMatch("k", "l", 92.0, 0.1)],
            image_matches=[ImageMatch("e", "f", 95.0)],
            audio_matches=[AudioMatch("g", "h", 97.0, 0.2), AudioMatch("k", "l", 96.0, 0.1)],
            folder_pairs=[FolderPair("left", "right", 80.0, 80.0, 90.0, [], [], [])],
            name_hints=[NameHint("name:key", 100.0, ["i", "j"], "Title")],
        )

        items = build_review_items(report)
        counts = category_counts(items)

        self.assertEqual({item.category for item in items}, set(CATEGORY_LABELS) - {"all"})
        self.assertEqual(counts["all"], 7)
        self.assertTrue(all(counts[category] == 1 for category in CATEGORY_LABELS if category != "all"))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 GUI extra is not installed")
class GuiSmokeTests(unittest.TestCase):
    # A-28, unresolved: name a new MainWindow-constructing test so that it
    # sorts LATE in this class (unittest runs methods alphabetically).
    #
    # One extra window placed immediately after
    # `test_similar_video_chain_is_one_review_case_with_working_batch_cleanup`
    # makes the *whole suite* abort with a native access violation, inside
    # Pillow worker threads, over in tests/test_images.py. The same test
    # renamed to sort last is clean over repeated full runs, and two plain
    # extra windows appended at the end are clean too - so this is about
    # position in the accumulated Qt object graph, not about how many windows
    # exist. `close()` does not destroy a window, and forcing the collection
    # onto the GUI thread in tearDown makes the abort deterministic rather
    # than fixing it.
    #
    # This is a workaround and it is fragile: it depends on alphabetical
    # ordering. See docs/AUDIT-1.10.1.md (A-28) for the bisect, the four
    # standalone reproductions that did NOT trigger it, and where to start.
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])
        QCoreApplication.setOrganizationName("SameSame")
        QCoreApplication.setApplicationName("SameSame")

    def setUp(self) -> None:
        from PySide6.QtCore import QSettings

        # F-18's QSettings persistence must not touch the real machine
        # registry/config from a test run, and must not leak between test
        # methods either (MainWindow() is constructed many times below) -
        # redirect every QSettings() constructed with no explicit path to a
        # fresh throwaway ini file per test.
        self._settings_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._settings_dir.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        from PySide6.QtCore import QSettings

        # Both of these are process-global Qt state. Leaving them pointing at
        # a deleted temporary directory would silently affect anything that
        # constructs QSettings later in the same process (A-12).
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "")
        self._settings_dir.cleanup()

    def test_shipped_defaults_are_pinned_to_the_settings_schema_version(self) -> None:
        # A-17: the schema guard failed twice not because it was wrong but
        # because nothing connected it to the thing it guards - 1.9.2 and
        # 1.9.3 re-derived the image and audio thresholds and neither bumped
        # it, so a 1.9.0/1.9.1 profile restored image=80 and audio=88.
        #
        # This asserts the *relationship*: the defaults recorded here must
        # match what the package actually ships. Changing a default without
        # bumping SETTINGS_SCHEMA_VERSION (and adding a row here) fails, which
        # is the only cheap way to make the connection impossible to forget.
        from dedupe.cli import DEFAULT_CONFIG
        from dedupe.gui.main_window import SETTINGS_SCHEMA_VERSION

        defaults_by_schema = {
            3: {
                "video_threshold": 20.0,
                "image_threshold": 60.0,
                "audio_threshold": 60.0,
                "folder_threshold": 50.0,
                "name_threshold": 92.0,
            },
        }
        self.assertIn(
            SETTINGS_SCHEMA_VERSION,
            defaults_by_schema,
            "SETTINGS_SCHEMA_VERSION changed without recording the defaults it ships with.",
        )
        for key, expected in defaults_by_schema[SETTINGS_SCHEMA_VERSION].items():
            self.assertEqual(
                DEFAULT_CONFIG[key],
                expected,
                f"{key} changed without bumping SETTINGS_SCHEMA_VERSION - a stored "
                f"value from the previous release would be restored on the new scale (A-17).",
            )

    def test_thresholds_from_an_older_schema_are_not_restored(self) -> None:
        # A-03: 1.7.8/1.7.9 stored thresholds on the pre-F-24 raw scale, and
        # restoring 85 on today's calibrated scale made video matching find
        # nothing without ever saying why.
        #
        # A-17: this now stores schema *2* rather than nothing. Schema 2 is not
        # an ancient profile - it is 1.9.0/1.9.1, the immediately preceding
        # releases, whose image=80 and audio=88 were correct when written and
        # are wrong now (A-06 replaced the image formula; A-14 measured 88 as
        # missing 16 of 20 confirmed duplicate pairs). It is deliberately the
        # newest rejected schema, since the old schema-0 case is covered by the
        # same `>=` comparison. Everything else still restores.
        from PySide6.QtCore import QSettings

        from dedupe.cli import DEFAULT_CONFIG
        from dedupe.gui.main_window import MainWindow

        settings = QSettings()
        settings.setValue("schema_version", 2)
        settings.beginGroup("scan")
        settings.setValue("video_threshold", 85.0)
        settings.setValue("image_threshold", 80.0)
        settings.setValue("audio_threshold", 88.0)
        settings.setValue("workers", 7)
        settings.endGroup()
        settings.sync()

        window = MainWindow()
        try:
            self.assertEqual(window.video_threshold.value(), DEFAULT_CONFIG["video_threshold"])
            self.assertEqual(window.image_threshold.value(), DEFAULT_CONFIG["image_threshold"])
            self.assertEqual(window.audio_threshold.value(), DEFAULT_CONFIG["audio_threshold"])
            self.assertEqual(window.workers.value(), 7)
        finally:
            window.close()
            self.application.processEvents()

        # Saved by the current version, the same values now survive a reopen.
        reopened = MainWindow()
        try:
            reopened.video_threshold.setValue(42.0)
        finally:
            reopened.close()
            self.application.processEvents()

        final = MainWindow()
        try:
            self.assertEqual(final.video_threshold.value(), 42.0)
        finally:
            final.close()
            self.application.processEvents()

    def test_missing_collection_roots_are_kept_but_reported(self) -> None:
        # A-12: a root on a removable drive is missing today and back
        # tomorrow, so it must survive a restart - but the user should be told
        # rather than left wondering why the scan found nothing.
        from dedupe.gui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            present = str(Path(tmp).resolve())
            absent = str(Path(tmp).resolve() / "gone")

            first = MainWindow()
            try:
                first.folder_list.addItem(present)
                first.folder_list.addItem(absent)
            finally:
                first.close()
                self.application.processEvents()

            second = MainWindow()
            try:
                restored = [second.folder_list.item(i).text() for i in range(second.folder_list.count())]
                self.assertEqual(restored, [present, absent])
                # Compare the listed roots exactly: `absent` is a child path of
                # `present`, so a substring check cannot tell them apart.
                listed = [line.strip() for line in second.log_view.toPlainText().splitlines()[1:]]
                self.assertEqual(listed, [absent])
            finally:
                second.close()
                self.application.processEvents()

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

    def test_scan_settings_persist_across_windows_except_the_unsafe_recycle_flag(self) -> None:
        # F-18: collection roots, thresholds, worker count, ffmpeg/ffprobe
        # paths, and skip flags survive a close/reopen. allow_unsafe_recycle
        # must NOT survive it - it always starts unchecked, on purpose.
        from dedupe.gui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            folder = str(Path(tmp).resolve())
            first = MainWindow()
            try:
                first.folder_list.addItem(folder)
                first.workers.setValue(9)
                first.video_threshold.setValue(77.0)
                first.skip_audio.setChecked(True)
                first.ffmpeg_path.setText("C:/tools/ffmpeg.exe")
                first.allow_unsafe_recycle.setChecked(True)
            finally:
                first.close()
                self.application.processEvents()

            second = MainWindow()
            try:
                self.assertEqual(
                    [second.folder_list.item(i).text() for i in range(second.folder_list.count())],
                    [folder],
                )
                self.assertEqual(second.workers.value(), 9)
                self.assertEqual(second.video_threshold.value(), 77.0)
                self.assertTrue(second.skip_audio.isChecked())
                self.assertEqual(second.ffmpeg_path.text(), "C:/tools/ffmpeg.exe")
                self.assertFalse(second.allow_unsafe_recycle.isChecked())
            finally:
                second.close()
                self.application.processEvents()

    def test_scan_progress_shows_stage_counts_activity_and_elapsed_time(self) -> None:
        import time

        from dedupe.events import ScanEvent, ScanEventType, ScanStage
        from dedupe.gui.main_window import MainWindow

        window = MainWindow()
        try:
            window._scan_started_at = time.monotonic() - 65
            window._handle_event(
                ScanEvent(
                    ScanEventType.STAGE_STARTED,
                    stage=ScanStage.VIDEO_MATCHING,
                    message="Finding similar videos",
                    current=0,
                    total=10,
                )
            )
            window._handle_event(
                ScanEvent(
                    ScanEventType.PROGRESS,
                    stage=ScanStage.VIDEO_MATCHING,
                    message="Video fingerprints",
                    current=2,
                    total=10,
                    unit="file",
                )
            )

            status = window.status_label.text()
            self.assertIn("Step 3/7", status)
            self.assertIn("Finding similar videos", status)
            self.assertIn("Video fingerprints", status)
            self.assertIn("2/10 files", status)
            self.assertIn("elapsed 1m 05s", status)
            self.assertEqual(window.progress_bar.value(), 2)
            self.assertIn("2 / 10 files", window.progress_bar.text())
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
                destinations = {tab.preview_table.item(row, 2).text() for row in range(tab.preview_table.rowCount())}
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
            info = MediaInfo(
                source, source.stat().st_size, 25 * 60, "matroska", (StreamInfo(0, "video", "h264", 1280, 720),)
            )
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
                poll.timeout.connect(lambda: event_loop.quit() if not window.compression_tab.is_loading else None)
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

    def test_compression_cleanup_modes_reach_the_queue_and_flag_a_useless_quarantine(self) -> None:
        from PySide6.QtCore import Qt

        from dedupe.actions import FileAction
        from dedupe.gui.compression_tab import CompressionTab, CompressionVideo
        from dedupe.transcode.models import OutputLayout

        tab = CompressionTab()
        try:
            # A tab with no parent window must not recurse looking for one.
            self.assertIsInstance(tab._quarantine_root(), Path)

            # Replacing in place is the default, and it owns the cleanup switch:
            # there is nowhere for the encode to go while the source is there.
            self.assertEqual(tab.output_layout(), OutputLayout.REPLACE)
            self.assertTrue(tab.recycle_originals.isChecked())
            self.assertFalse(tab.recycle_originals.isEnabled())
            self.assertFalse(tab.output_dir.isEnabled())

            tab.output_layout_combo.setCurrentIndex(tab.output_layout_combo.findData(OutputLayout.BESIDE.value))
            self.assertTrue(tab.recycle_originals.isEnabled())
            tab.recycle_originals.setChecked(False)
            self.assertIsNone(tab.cleanup_action(), "originals are kept when the box is cleared")
            self.assertFalse(tab.cleanup_mode.isEnabled())
            self.assertIn("kept", tab.cleanup_note.text())

            payloads: list[tuple[object, ...]] = []
            tab.queue_requested.connect(lambda *args: payloads.append(args))
            with tempfile.TemporaryDirectory() as tmp:
                episode = Path(tmp) / "episode.mkv"
                episode.write_bytes(b"video")
                tab.folder_path.setText(tmp)
                tab._add_video(CompressionVideo(episode, episode.stat().st_size, 60.0, "h264", "1280×720"))
                tab.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                # Pin the quarantine root beside the sources so the same-volume
                # verdict is decided by the test rather than by the host's drives.
                tab._quarantine_root = lambda: Path(tmp) / "quarantine"  # type: ignore[method-assign]

                tab.recycle_originals.setChecked(True)
                self.assertTrue(tab.cleanup_mode.isEnabled())
                self.assertEqual(tab.cleanup_action(), FileAction.QUARANTINE)
                self.assertIn("frees no space", tab.cleanup_note.text())

                tab.cleanup_mode.setCurrentIndex(tab.cleanup_mode.findData(FileAction.DELETE.value))
                self.assertEqual(tab.cleanup_action(), FileAction.DELETE)
                self.assertIn("no restore", tab.cleanup_note.text())

                tab._request_queue()

            self.assertEqual(len(payloads), 1)
            self.assertEqual(
                payloads[0][4],
                (FileAction.DELETE, OutputLayout.BESIDE),
                "the chosen cleanup mode and layout must both reach the queue",
            )
        finally:
            tab.close()
            self.application.processEvents()

    def test_journal_dialog_empties_quarantine_after_an_explicit_confirmation(self) -> None:
        from unittest.mock import patch

        from PySide6.QtWidgets import QMessageBox

        from dedupe.actions import FileAction, FileActionService
        from dedupe.gui.journal_dialog import JournalDialog
        from dedupe.models import FileRecord

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            collection = base / "collection"
            collection.mkdir()
            source = collection / "episode.mkv"
            source.write_bytes(b"episode payload")
            stat = source.stat()
            service = FileActionService(base / "journal.sqlite3", base / "quarantine")
            outcome = service.perform(
                FileRecord(source.resolve(), collection.resolve(), stat.st_size, stat.st_mtime, source.stem),
                FileAction.QUARANTINE,
            )
            quarantined = outcome.destination
            self.assertTrue(quarantined and quarantined.is_file())

            dialog = JournalDialog(base / "journal.sqlite3", base / "quarantine")
            try:
                self.assertTrue(dialog.empty_quarantine_button.isEnabled())

                # Declining must change nothing at all.
                with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No):
                    dialog._empty_quarantine()
                self.assertTrue(quarantined and quarantined.is_file(), "a declined purge must not delete")

                purged: list[bool] = []
                dialog.journal_purged.connect(lambda: purged.append(True))
                with (
                    patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes),
                    patch.object(QMessageBox, "information"),
                ):
                    dialog._empty_quarantine()

                self.assertFalse(quarantined and quarantined.exists())
                self.assertEqual(purged, [True])
                self.assertFalse(dialog.empty_quarantine_button.isEnabled(), "nothing left to empty")
                self.assertEqual(service.quarantine_contents(), (0, 0))
            finally:
                dialog.close()
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
            report = DedupeReport(
                2, [], [], [], [], [], [NameHint("name:test", 95.0, [str(left), str(right)], "Episode")]
            )
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
                # Explicit teardown (F-06): closing the window releases both
                # preview panes' media handles (see ComparisonWidget.stop()),
                # but Qt Multimedia's backend can finish that release a
                # cycle or two after setSource(QUrl()) returns, so pump the
                # event loop a few times before the tempdir cleanup runs -
                # this is what made the suite intermittently hit WinError 32
                # on `right`, which nothing in this test used to release.
                window.close()
                for _ in range(5):
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

    def test_finishing_transcode_does_not_reenable_actions_while_consolidation_is_busy(self) -> None:
        # F-07: _transcode_busy_changed used to re-enable comparison actions
        # and the scan button without checking consolidation_tab.is_busy, so
        # starting a consolidation batch and then finishing an unrelated
        # transcode queue would silently re-enable destructive quarantine
        # buttons while a journaled consolidation batch was still moving
        # files.
        from dedupe.gui.main_window import MainWindow

        window = MainWindow()
        try:
            window.consolidation_tab._thread = object()  # simulate a busy consolidation batch
            window._transcode_busy_changed(False)  # an unrelated transcode queue just finished

            self.assertFalse(window.comparison._actions_enabled)
            self.assertFalse(window.scan_button.isEnabled())
            self.assertFalse(window.folder_list.isEnabled())
        finally:
            window.consolidation_tab._thread = None
            window.close()
            self.application.processEvents()

    def test_similar_video_chain_is_one_review_case_with_working_batch_cleanup(self) -> None:
        """Stage B acceptance test (F-23): three copies of one episode linked by a
        chained A~B, B~C video match must produce ONE review case listing all
        three files, with a working keeper-based batch action, and a decision
        recorded once must cover the whole cluster - not three separate pairwise
        cases each needing its own decision."""
        from PySide6.QtCore import QEventLoop, QTimer, Qt

        from dedupe.actions import FileAction, FileActionService
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
            episode_a = root / "episode-a.mkv"
            episode_b = root / "episode-b.mkv"
            episode_c = root / "episode-c.mkv"
            for path in (episode_a, episode_b, episode_c):
                path.write_bytes(b"same-episode-different-intro")
            records = []
            for path in (episode_a, episode_b, episode_c):
                stat = path.stat()
                records.append(FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem))
            report = DedupeReport(
                3,
                [],
                [
                    VideoMatch(str(episode_a), str(episode_b), 91.0, 0.2),
                    VideoMatch(str(episode_b), str(episode_c), 89.0, 0.3),
                ],
                [],
                [],
                [],
                [],
            )
            result = ScanResult(report, records, {record.path_key: basic_media_metadata(record) for record in records})
            journal_path = root / "operations.sqlite3"
            quarantine_root = root / "quarantine"
            window = MainWindow()
            window._journal_path = lambda: journal_path  # type: ignore[method-assign]
            window.quarantine_path.setText(str(quarantine_root))
            window._scan_completed(result)
            self.application.processEvents()

            try:
                self.assertEqual(len(window._items), 1)
                item = window._items[0]
                self.assertEqual(item.category, "video")
                self.assertEqual(set(item.paths), {str(episode_a), str(episode_b), str(episode_c)})

                window.result_list.setCurrentRow(0)
                self.application.processEvents()
                self.assertEqual(window.comparison.batch_file_list.count(), 3)
                for index in range(window.comparison.batch_file_list.count()):
                    checkbox = window.comparison.batch_file_list.item(index)
                    if checkbox.data(Qt.ItemDataRole.UserRole) != str(episode_a):
                        checkbox.setCheckState(Qt.CheckState.Checked)
                self.assertTrue(window.comparison.batch_quarantine_checked_button.isEnabled())

                window._confirm_action = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
                window._request_batch_action((str(episode_b), str(episode_c)), FileAction.QUARANTINE)
                wait_for_action(window)

                self.assertTrue(episode_a.exists())
                self.assertFalse(episode_b.exists())
                self.assertFalse(episode_c.exists())

                operations = FileActionService(journal_path, quarantine_root).recent_operations()
                group_ids = {operation.group_id for operation in operations}
                self.assertEqual(group_ids, {window._group_id(item)})

                window.result_list.setCurrentRow(0)
                self.application.processEvents()
                self.assertIsNotNone(window._review_decisions.get(window._group_id(item)))
            finally:
                window.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
