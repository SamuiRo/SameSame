from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dedupe.benchmark import main
from dedupe.cli import DEFAULT_CONFIG
from dedupe.video_fingerprint import check_video_tools

FFMPEG, FFPROBE = check_video_tools(
    os.environ.get("SAMESAME_TEST_FFMPEG", "ffmpeg"),
    os.environ.get("SAMESAME_TEST_FFPROBE", "ffprobe"),
)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required")
class BenchmarkIntegrationTests(unittest.TestCase):
    """`samesame-benchmark` is a shipped entry point that had no coverage at
    all, and it silently rotted twice while the matchers were being reworked:
    A-14's degenerate-fingerprint veto turned every one of its synthetic audio
    positives into a miss (its corpus was built from pure tones), and A-15's
    dihedral views pushed one of its flat synthetic image negatives to 62.50
    against a 60.0 bar. Both were fixture faults rather than matcher faults,
    and both were invisible because nothing ran this tool."""

    def test_synthetic_corpus_is_clean_at_the_shipped_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = base / "results.json"
            exit_code = main(
                [
                    "--work-dir",
                    str(base / "corpus"),
                    "--output",
                    str(output),
                    "--ffmpeg",
                    str(FFMPEG),
                    "--ffprobe",
                    str(FFPROBE),
                ]
            )
            self.assertEqual(exit_code, 0)
            report = json.loads(output.read_text(encoding="utf-8"))

        media = report["media"] if "media" in report else report
        for name, default_key in (
            ("image", "image_threshold"),
            ("video", "video_threshold"),
            ("audio", "audio_threshold"),
        ):
            with self.subTest(media=name):
                summary = media[name]
                current = summary["current"]
                self.assertEqual(current["threshold"], DEFAULT_CONFIG[default_key])
                self.assertEqual(current["false_positives"], 0)
                self.assertEqual(current["false_negatives"], 0)
                # A corpus that only just passes is not evidence of anything;
                # keep real headroom on both sides.
                self.assertGreater(summary["min_positive"], summary["max_negative"] + 20.0)


if __name__ == "__main__":
    unittest.main()
