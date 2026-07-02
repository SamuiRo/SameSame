from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from dedupe.cache import Cache
from dedupe.events import CancellationToken, ScanCancelled, ScanEvent, ScanEventType, ScanStage
from dedupe.metadata import probe_media_metadata
from dedupe.models import FileRecord
from dedupe.progress import progress_scope, tqdm
from dedupe.service import ScanOptions, ScanService


class ScanServiceTests(unittest.TestCase):
    def _options(self, folder: Path, *, extensions: set[str] | None = None) -> ScanOptions:
        return ScanOptions(
            folders=[folder],
            cache=folder / "scan.sqlite3",
            extensions=extensions or {".dat"},
            name_provider="none",
            workers=1,
            skip_video=True,
            skip_images=True,
            skip_audio=True,
        )

    def test_service_returns_report_metadata_and_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "copy-a.dat").write_bytes(b"same content")
            (root / "copy-b.dat").write_bytes(b"same content")
            events: list[ScanEvent] = []

            result = ScanService().run(self._options(root), on_event=events.append)

            self.assertEqual(result.report.scanned_files, 2)
            self.assertEqual(len(result.report.exact_duplicates), 1)
            self.assertEqual(set(result.metadata), {record.path_key for record in result.records})
            started = {event.stage for event in events if event.event_type == ScanEventType.STAGE_STARTED}
            completed = {event.stage for event in events if event.event_type == ScanEventType.STAGE_COMPLETED}
            self.assertEqual(started, set(ScanStage))
            self.assertEqual(completed, set(ScanStage))
            self.assertTrue(any(event.event_type == ScanEventType.PROGRESS for event in events))
            self.assertEqual(events[-1].event_type, ScanEventType.COMPLETED)
            self.assertEqual(events[-1].total, 2)

    def test_nonterminal_progress_does_not_construct_tqdm_in_background_clients(self) -> None:
        events: list[ScanEvent] = []
        with patch("dedupe.progress._tqdm") as terminal_progress:
            with progress_scope(
                stage=ScanStage.SCANNING,
                callback=events.append,
                cancellation=CancellationToken(),
                show_terminal=False,
            ):
                self.assertEqual(list(tqdm(["a", "b"], total=2, desc="Files")), ["a", "b"])

        terminal_progress.assert_not_called()
        self.assertEqual([event.current for event in events], [1, 2])

    def test_cancellation_emits_event_and_preserves_completed_scan_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "copy-a.dat").write_bytes(b"same content")
            (root / "copy-b.dat").write_bytes(b"same content")
            options = self._options(root)
            cancellation = CancellationToken()
            events: list[ScanEvent] = []

            def receive(event: ScanEvent) -> None:
                events.append(event)
                if event.event_type == ScanEventType.STAGE_STARTED and event.stage == ScanStage.EXACT_MATCHING:
                    cancellation.cancel()

            with self.assertRaises(ScanCancelled):
                ScanService().run(options, on_event=receive, cancellation=cancellation)

            self.assertEqual(events[-1].event_type, ScanEventType.CANCELLED)
            with Cache(options.cache) as cache:
                self.assertEqual(cache.stats()["files"]["total"], 2)  # type: ignore[index]

    def test_failure_emits_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[ScanEvent] = []
            with patch("dedupe.service.scan_folders", side_effect=RuntimeError("scan failed")):
                with self.assertRaisesRegex(RuntimeError, "scan failed"):
                    ScanService().run(self._options(root), on_event=events.append)

            self.assertEqual(events[-1].event_type, ScanEventType.FAILED)
            self.assertEqual(events[-1].message, "scan failed")

    def test_missing_ffmpeg_is_a_structured_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.mp4").write_bytes(b"not a real video")
            options = self._options(root, extensions={".mp4"})
            options.skip_video = False
            options.ffmpeg = "definitely-missing-ffmpeg"
            options.ffprobe = "definitely-missing-ffprobe"
            events: list[ScanEvent] = []

            result = ScanService().run(options, on_event=events.append)

            warning_events = [event for event in events if event.event_type == ScanEventType.WARNING]
            self.assertEqual(len(warning_events), 1)
            self.assertEqual(warning_events[0].stage, ScanStage.VIDEO_MATCHING)
            self.assertEqual(result.report.warnings, [warning_events[0].message])

    def test_scanner_warnings_are_forwarded_as_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            events: list[ScanEvent] = []
            options = self._options(missing)
            options.cache = root / "scan.sqlite3"

            result = ScanService().run(options, on_event=events.append)

            warning_events = [event for event in events if event.event_type == ScanEventType.WARNING]
            self.assertEqual(len(warning_events), 1)
            self.assertEqual(warning_events[0].stage, ScanStage.SCANNING)
            self.assertIn("missing or non-directory", warning_events[0].message)
            self.assertEqual(result.report.warnings, [warning_events[0].message])

    def test_image_metadata_contains_review_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "poster.png"
            Image.new("RGB", (48, 32), (20, 40, 60)).save(path)
            stat = path.stat()
            record = FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem)

            metadata = probe_media_metadata(record)

            self.assertIsNone(metadata.error)
            self.assertEqual(metadata.media_type, "image")
            self.assertEqual(metadata.container, "PNG")
            self.assertEqual((metadata.image_streams[0].width, metadata.image_streams[0].height), (48, 32))
            self.assertEqual(metadata.image_streams[0].pixel_format, "RGB")

    def test_ffprobe_metadata_contains_streams_chapters_and_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "episode.mkv"
            path.write_bytes(b"fixture")
            stat = path.stat()
            record = FileRecord(path, root, stat.st_size, stat.st_mtime, path.stem)
            payload = {
                "format": {"format_name": "matroska,webm", "duration": "120.5", "bit_rate": "750000"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                        "pix_fmt": "yuv420p10le",
                        "avg_frame_rate": "24000/1001",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "flac",
                        "sample_rate": "48000",
                        "channels": 2,
                        "tags": {"language": "jpn", "title": "Main"},
                        "disposition": {"default": 1},
                    },
                    {
                        "index": 2,
                        "codec_type": "subtitle",
                        "codec_name": "ass",
                        "tags": {"language": "eng"},
                        "disposition": {"forced": 1},
                    },
                    {"index": 3, "codec_type": "attachment", "codec_name": "ttf"},
                ],
                "chapters": [{"id": 0}, {"id": 1}],
            }
            completed = SimpleNamespace(stdout=json.dumps(payload))

            with patch("dedupe.metadata.subprocess.run", return_value=completed):
                metadata = probe_media_metadata(record, ffprobe=sys.executable)

            self.assertEqual(metadata.container, "matroska,webm")
            self.assertEqual(metadata.duration, 120.5)
            self.assertEqual(metadata.bit_rate, 750000)
            self.assertEqual(metadata.chapter_count, 2)
            self.assertEqual(metadata.attachment_count, 1)
            self.assertEqual(metadata.video_streams[0].codec, "hevc")
            self.assertAlmostEqual(metadata.video_streams[0].frame_rate or 0, 23.976, places=3)
            self.assertEqual(metadata.audio_streams[0].language, "jpn")
            self.assertTrue(metadata.audio_streams[0].is_default)
            self.assertEqual(metadata.subtitle_streams[0].codec, "ass")
            self.assertTrue(metadata.subtitle_streams[0].is_forced)


if __name__ == "__main__":
    unittest.main()
