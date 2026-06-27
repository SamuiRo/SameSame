from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.transcode.models import JobStatus, TranscodeRequest
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


if __name__ == "__main__":
    unittest.main()
