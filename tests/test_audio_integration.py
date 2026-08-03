from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dedupe.audio_fingerprint import ITEM_SECONDS, audio_similarity
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

    def _create_source(self, path: Path, left_frequency: int, right_frequency: int, seed: int = 11) -> None:
        # Noise plus two tones, not tones alone (A-14). A pure tone
        # fingerprints to a single repeated Chromaprint item, so two
        # *different* tones used to score 75 against each other - this
        # fixture's positives only passed for the same reason its negative
        # nearly failed. Seeded, so the corpus stays reproducible.
        self._run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=c=pink:seed={seed}:r=44100:d=20",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={left_frequency}:sample_rate=44100:duration=20",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={right_frequency}:sample_rate=44100:duration=20",
            "-filter_complex",
            "[0:a][1:a][2:a]amix=inputs=3:weights=1 0.6 0.35,volume=0.8",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(path),
        )

    def test_default_extensions_include_audio(self) -> None:
        self.assertTrue(AUDIO_EXTENSIONS <= normalize_extensions(None))

    def test_audio_similarity_allows_small_fingerprint_alignment(self) -> None:
        # Varied items, not a repeated one: a constant fingerprint carries no
        # information and is now vetoed outright (A-14).
        source = [0x12345678 ^ (index * 0x9E3779B1) for index in range(40)]
        shifted = [0xFFFFFFFF, 0xAAAAAAAA] + source
        self.assertEqual(audio_similarity(source, shifted), 100.0)

    def test_degenerate_audio_never_matches(self) -> None:
        # A-14: two different pure tones fingerprint to a single repeated item
        # each and scored 75.00 against one another - the audio counterpart of
        # A-06's flat-image problem. Real library audio is 99%+ distinct.
        one_tone = [0x11111111] * 140
        another_tone = [0x22222222] * 140
        self.assertEqual(audio_similarity(one_tone, another_tone), 0.0)
        self.assertEqual(audio_similarity(one_tone, one_tone), 0.0)

    def test_audio_similarity_recovers_a_multi_second_offset(self) -> None:
        # A-14: alignment used to allow 8 chromaprint items, which is almost
        # exactly one second. Two releases of the same content routinely
        # differ by more at the head, and past a second the score collapsed -
        # measured on real library audio, a 2-second lead took a
        # byte-identical recording from 98.6 to 10.9.
        import random

        random.seed(4)
        source = [random.getrandbits(32) for _ in range(2000)]
        lead = [random.getrandbits(32) for _ in range(int(round(5.0 / ITEM_SECONDS)))]
        self.assertGreaterEqual(audio_similarity(source, lead + source), 99.0)

    def test_audio_similarity_stays_cheap_at_the_wide_shift(self) -> None:
        # A-14: the shift range grew 30x, so the search had to stop being
        # exhaustive - a flat scan measured 240 ms per pair, which is 91
        # minutes for a library-sized pass (the A-01 shape).
        import random
        import time

        random.seed(5)
        left = [random.getrandbits(32) for _ in range(7000)]
        right = [random.getrandbits(32) for _ in range(7000)]

        start = time.perf_counter()
        audio_similarity(left, right)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.05)

    def test_audio_similarity_does_not_invent_a_match(self) -> None:
        # The search maximises over ~475 candidate shifts, so it must not
        # lift unrelated audio by picking the luckiest one.
        import random

        random.seed(6)
        left = [random.getrandbits(32) for _ in range(2000)]
        right = [random.getrandbits(32) for _ in range(2000)]
        self.assertLessEqual(audio_similarity(left, right), 10.0)

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
            self._create_source(unrelated, 1200, 1700, seed=77)

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
            matched_names = [{Path(match["left"]).name, Path(match["right"]).name} for match in report["audio_matches"]]
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["scanned_files"], 4)
            self.assertIn({"source.wav", "copy.mp3"}, matched_names)
            self.assertIn({"source.wav", "copy.flac"}, matched_names)
            self.assertFalse(any("unrelated.wav" in pair for pair in matched_names))
            self.assertTrue(report["folder_pairs"][0]["matched"][0]["content_backed"])
            self.assertIn("Similar audio", html_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
