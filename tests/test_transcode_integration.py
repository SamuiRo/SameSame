from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.transcode.models import JobStatus, TranscodeRequest
from dedupe.transcode.presets import TranscodePreset
from dedupe.transcode.probe import probe_media
from dedupe.transcode.queue import TranscodeQueue


FFMPEG = shutil.which(os.environ.get("SAMESAME_TEST_FFMPEG", "ffmpeg"))
FFPROBE = shutil.which(os.environ.get("SAMESAME_TEST_FFPROBE", "ffprobe"))


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required")
class TranscodeIntegrationTests(unittest.TestCase):
    def test_x265_encode_keeps_source_tracks_chapters_metadata_and_attachment(self) -> None:
        assert FFMPEG is not None
        assert FFPROBE is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "subtitles.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            attachment = root / "font.txt"
            attachment.write_text("synthetic attachment", encoding="utf-8")
            metadata = root / "chapters.ffmeta"
            metadata.write_text(
                ";FFMETADATA1\ntitle=Synthetic episode\n\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Opening\n",
                encoding="utf-8",
            )
            source = root / "джерело.mkv"
            subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x72:rate=10:duration=1.5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1.5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=880:duration=1.5",
                    "-i",
                    str(subtitle),
                    "-f",
                    "ffmetadata",
                    "-i",
                    str(metadata),
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-map",
                    "2:a",
                    "-map",
                    "3:s",
                    "-map_metadata",
                    "4",
                    "-map_chapters",
                    "4",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    "-c:a",
                    "aac",
                    "-c:s",
                    "ass",
                    "-attach",
                    str(attachment),
                    "-metadata:s:t",
                    "mimetype=text/plain",
                    "-metadata:s:t",
                    "filename=font.txt",
                    str(source),
                ],
                check=True,
                timeout=60,
            )
            output = root / "encoded.mkv"
            queue = TranscodeQueue(ffmpeg=FFMPEG, ffprobe=FFPROBE)
            results = queue.run([TranscodeRequest(source, output, "anime_x265_balanced")])

            self.assertEqual(results[0].status, JobStatus.COMPLETED, results[0].message)
            self.assertTrue(source.is_file(), "the source must always be kept")
            self.assertTrue(output.is_file())
            self.assertFalse(any(root.glob("*.part.mkv")))
            before = probe_media(source, FFPROBE)
            after = probe_media(output, FFPROBE)
            for kind in ("video", "audio", "subtitle", "attachment"):
                self.assertEqual(after.stream_count(kind), before.stream_count(kind))
            self.assertEqual(after.chapter_count, before.chapter_count)
            self.assertTrue(results[0].log_path and results[0].log_path.is_file())

    def test_odd_sized_cover_art_is_kept_as_an_attachment_and_does_not_break_the_encode(self) -> None:
        """Regression: a 423x600 cover.jpg made libx265 fail with "Invalid argument"."""
        assert FFMPEG is not None
        assert FFPROBE is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cover = root / "cover.jpg"
            subprocess.run(
                [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "color=c=red:size=423x601", "-frames:v", "1", str(cover)],
                check=True,
                timeout=60,
            )
            font = root / "font.ttf"
            font.write_bytes(b"synthetic font")
            source = root / "episode.mkv"
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=10:duration=1.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                    "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac",
                    "-attach", str(font), "-metadata:s:t:0", "mimetype=application/x-truetype-font",
                    "-attach", str(cover), "-metadata:s:t:1", "mimetype=image/jpeg",
                    "-metadata:s:t:1", "filename=cover.jpg",
                    str(source),
                ],
                check=True,
                timeout=60,
            )
            before = probe_media(source, FFPROBE)
            self.assertTrue(any(stream.attached_pic for stream in before.streams), "fixture must carry cover art")

            output = root / "encoded.mkv"
            results = TranscodeQueue(ffmpeg=FFMPEG, ffprobe=FFPROBE).run(
                [TranscodeRequest(source, output, "anime_x265_balanced")]
            )

            self.assertEqual(results[0].status, JobStatus.COMPLETED, results[0].message)
            after = probe_media(output, FFPROBE)
            for kind in ("video", "audio", "subtitle", "attachment"):
                self.assertEqual(after.stream_count(kind), before.stream_count(kind), kind)
            covers = [stream for stream in after.streams if stream.attached_pic]
            self.assertEqual(len(covers), 1, "the cover must stay an attachment, not become a video track")
            self.assertEqual(covers[0].filename, "cover.jpg")
            self.assertEqual(covers[0].codec, "mjpeg")
            main = [stream for stream in after.streams if stream.kind == "video" and not stream.attached_pic]
            self.assertEqual(main[0].codec, "hevc")
            self.assertEqual([path.name for path in root.iterdir() if ".cover" in path.name], [])

    def test_an_already_efficient_source_is_skipped_instead_of_growing_on_disk(self) -> None:
        """Re-encoding an x265 source at CRF 0 can only inflate it; nothing may be committed."""
        assert FFMPEG is not None
        assert FFPROBE is not None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "already-x265.mkv"
            subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=12:duration=2",
                    "-c:v",
                    "libx265",
                    "-crf",
                    "40",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                check=True,
                timeout=120,
            )
            source_size = source.stat().st_size
            inflating = TranscodePreset(
                "test_inflate",
                "Inflating",
                "libx265",
                ("-c:v", "libx265", "-crf", "0", "-preset", "medium", "-pix_fmt", "yuv420p10le"),
            )
            output = root / "encoded.mkv"
            queue = TranscodeQueue(ffmpeg=FFMPEG, ffprobe=FFPROBE)
            results = queue.run([TranscodeRequest(source, output, inflating.preset_id, inflating)])

            self.assertEqual(results[0].status, JobStatus.SKIPPED, results[0].message)
            self.assertGreater(results[0].output_size, source_size)
            self.assertTrue(source.is_file(), "the source must be kept")
            self.assertEqual(source.stat().st_size, source_size, "the source must be untouched")
            self.assertFalse(output.exists(), "the inflated encode must not be committed")
            self.assertFalse(any(root.glob("*.part.mkv")))


if __name__ == "__main__":
    unittest.main()
