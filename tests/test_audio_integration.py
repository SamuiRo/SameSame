from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.audio_fingerprint import audio_similarity
from dedupe.cli import main
from dedupe.scanner import AUDIO_EXTENSIONS, normalize_extensions
from dedupe.video_fingerprint import check_video_tools

FFMPEG, FFPROBE = check_video_tools(
    os.environ.get("SAMESAME_TEST_FFMPEG", "ffmpeg"),
    os.environ.get("SAMESAME_TEST_FFPROBE", "ffprobe"),
)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required")
class AudioIntegrationTests(unittest.TestCase):
    def _run_ffmpeg(self, *arguments: str) -> None:
        subprocess.run(
            [str(FFMPEG), "-y", "-v", "error", *arguments],
            check=True,
            capture_output=True,
            timeout=60,
        )

    def _create_source(self, path: Path, left_frequency: int, right_frequency: int) -> None:
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={left_frequency}:sample_rate=44100:duration=20",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={right_frequency}:sample_rate=44100:duration=20",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:weights=1 0.45,volume=0.7",
            "-c:a",
            "pcm_s16le",
            str(path),
        )

    def test_default_extensions_include_audio(self) -> None:
        self.assertTrue(AUDIO_EXTENSIONS <= normalize_extensions(None))

    def test_audio_similarity_allows_small_fingerprint_alignment(self) -> None:
        source = [0x12345678] * 20
        shifted = [0xFFFFFFFF, 0xFFFFFFFF] + source
        self.assertEqual(audio_similarity(source, shifted), 100.0)

    def test_cli_matches_wav_mp3_flac_and_rejects_unrelated_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            left = base / "left"
            right = base / "right"
            left.mkdir()
            right.mkdir()
            source = left / "source.wav"
            mp3_copy = right / "copy.mp3"
            flac_copy = right / "copy.flac"
            unrelated = right / "unrelated.wav"

            self._create_source(source, 440, 660)
            self._run_ffmpeg("-i", str(source), "-c:a", "libmp3lame", "-b:a", "96k", str(mp3_copy))
            self._run_ffmpeg("-i", str(source), "-af", "volume=0.55", "-c:a", "flac", str(flac_copy))
            self._create_source(unrelated, 1200, 1700)

            json_report = base / "report.json"
            html_report = base / "report.html"
            exit_code = main(
                [
                    "--folders",
                    str(left),
                    str(right),
                    "--name-provider",
                    "none",
                    "--skip-video",
                    "--skip-images",
                    "--ffmpeg",
                    str(FFMPEG),
                    "--ffprobe",
                    str(FFPROBE),
                    "--cache",
                    str(base / "cache.sqlite3"),
                    "--json-output",
                    str(json_report),
                    "--output",
                    str(html_report),
                ]
            )

            report = json.loads(json_report.read_text(encoding="utf-8"))
            matched_names = [
                {Path(match["left"]).name, Path(match["right"]).name}
                for match in report["audio_matches"]
            ]
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["scanned_files"], 4)
            self.assertIn({"source.wav", "copy.mp3"}, matched_names)
            self.assertIn({"source.wav", "copy.flac"}, matched_names)
            self.assertFalse(any("unrelated.wav" in pair for pair in matched_names))
            self.assertTrue(report["folder_pairs"][0]["matched"][0]["content_backed"])
            self.assertIn("Схожі аудіозаписи", html_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
