from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.cache import Cache
from dedupe.models import FileRecord
from dedupe.video_fingerprint import PIL_AVAILABLE, check_video_tools, find_video_matches

FFMPEG, FFPROBE = check_video_tools(
    os.environ.get("SAMESAME_TEST_FFMPEG", "ffmpeg"),
    os.environ.get("SAMESAME_TEST_FFPROBE", "ffprobe"),
)


@unittest.skipUnless(PIL_AVAILABLE and FFMPEG and FFPROBE, "ffmpeg, ffprobe, and Pillow are required")
class VideoIntegrationTests(unittest.TestCase):
    def _run_ffmpeg(self, *arguments: str) -> None:
        subprocess.run(
            [str(FFMPEG), "-y", "-v", "error", *arguments],
            check=True,
            capture_output=True,
            timeout=60,
        )

    def _record(self, path: Path, root: Path) -> FileRecord:
        stat = path.stat()
        return FileRecord(
            path=path.resolve(),
            root=root.resolve(),
            size=stat.st_size,
            mtime=stat.st_mtime,
            raw_name=path.stem,
        )

    def test_real_decoding_matches_reencodes_and_small_duration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source.mp4"
            reencoded = base / "reencoded.avi"
            extended = base / "extended.mkv"
            unrelated = base / "unrelated.mp4"

            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=12:duration=6",
                "-c:v",
                "mpeg4",
                "-q:v",
                "3",
                "-pix_fmt",
                "yuv420p",
                str(source),
            )
            self._run_ffmpeg(
                "-i",
                str(source),
                "-vf",
                "scale=240:136",
                "-c:v",
                "mpeg4",
                "-q:v",
                "12",
                str(reencoded),
            )
            self._run_ffmpeg(
                "-i",
                str(source),
                "-vf",
                "tpad=stop_mode=clone:stop_duration=1",
                "-c:v",
                "mpeg4",
                "-q:v",
                "8",
                str(extended),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "smptebars=size=320x180:rate=12:duration=6",
                "-c:v",
                "mpeg4",
                "-q:v",
                "3",
                "-pix_fmt",
                "yuv420p",
                str(unrelated),
            )

            records = [self._record(path, base) for path in (source, reencoded, extended, unrelated)]
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_video_matches(
                    records,
                    cache,
                    threshold=90,
                    ffmpeg=str(FFMPEG),
                    ffprobe=str(FFPROBE),
                    workers=2,
                )

            matched_names = [
                {Path(match.left).name, Path(match.right).name}
                for match in matches
            ]
            self.assertIn({"source.mp4", "reencoded.avi"}, matched_names)
            self.assertIn({"source.mp4", "extended.mkv"}, matched_names)
            self.assertFalse(any("unrelated.mp4" in pair for pair in matched_names))


if __name__ == "__main__":
    unittest.main()
