from __future__ import annotations

import unittest

from dedupe.transcode.assessment import AudioTrack, Verdict, assess_compression
from dedupe.transcode.probe import _bit_rate, _frame_rate

EPISODE = 24 * 60


def _episode(total_mbps: float, codec: str, width: int, height: int, *audio: AudioTrack, **kwargs: object):
    size = round(total_mbps * 1e6 * EPISODE / 8)
    return assess_compression(
        size=size, duration=EPISODE, codec=codec, width=width, height=height, audio=audio, **kwargs
    )


class CompressionAssessmentTests(unittest.TestCase):
    # Bitrates below are real episodes from a 1.1 TB anime library.

    def test_blu_ray_rip_with_flac_is_worth_compressing(self) -> None:
        result = _episode(12.6, "h264", 1920, 1080, AudioTrack("flac", 800_000))
        self.assertEqual(result.verdict, Verdict.WORTH)
        self.assertEqual(result.rawness_label, "Heavy")
        self.assertGreater(result.saving_fraction, 0.6)
        self.assertEqual(result.lossless_audio_bps, 800_000)

    def test_near_disc_bitrate_reads_as_raw(self) -> None:
        result = _episode(37.2, "h264", 1920, 1080, AudioTrack("flac"))
        self.assertEqual(result.rawness_label, "Raw")
        self.assertEqual(result.verdict, Verdict.WORTH)

    def test_heavy_web_release_is_worth_compressing(self) -> None:
        result = _episode(8.1, "h264", 1920, 1080, AudioTrack("aac", 192_000))
        self.assertEqual(result.verdict, Verdict.WORTH)

    def test_ordinary_720p_web_rip_is_only_a_maybe(self) -> None:
        # Space can be won, but this is already a sensible H.264 encode.
        result = _episode(2.6, "h264", 1280, 720, AudioTrack("aac", 128_000))
        self.assertEqual(result.verdict, Verdict.MAYBE)
        self.assertEqual(result.rawness_label, "Compact")

    def test_compact_release_is_skipped(self) -> None:
        self.assertEqual(_episode(2.1, "h264", 1920, 1080, AudioTrack("aac", 192_000)).verdict, Verdict.SKIP)
        self.assertEqual(_episode(1.1, "h264", 1280, 720, AudioTrack("aac", 128_000)).verdict, Verdict.SKIP)

    def test_an_efficient_av1_file_is_skipped(self) -> None:
        result = _episode(2.4, "av1", 1920, 1080, AudioTrack("opus", 128_000))
        self.assertEqual(result.verdict, Verdict.SKIP)
        self.assertLess(result.saving_fraction, 0.05)

    def test_estimate_follows_the_preset(self) -> None:
        anime = _episode(4.4, "h264", 1920, 1080, category="anime", encoder="libsvtav1")
        film = _episode(4.4, "h264", 1920, 1080, category="film", encoder="libsvtav1")
        nvenc = _episode(4.4, "h264", 1920, 1080, category="anime", encoder="hevc_nvenc")
        self.assertGreater(anime.estimated_saving, film.estimated_saving)
        self.assertGreater(anime.estimated_saving, nvenc.estimated_saving)

    def test_high_frame_rate_raises_the_target(self) -> None:
        slow = _episode(6.0, "h264", 1920, 1080, frame_rate=24.0)
        fast = _episode(6.0, "h264", 1920, 1080, frame_rate=60.0)
        self.assertGreater(fast.target_video_bps, slow.target_video_bps)

    def test_unknown_inputs_give_no_estimate(self) -> None:
        self.assertIsNone(assess_compression(size=100, duration=0, codec="h264", width=1920, height=1080))
        self.assertIsNone(assess_compression(size=100, duration=60, codec="h264", width=None, height=None))

    def test_probe_reads_frame_rate_and_matroska_bitrate_tags(self) -> None:
        self.assertAlmostEqual(_frame_rate({"avg_frame_rate": "24000/1001"}), 23.976, places=3)
        self.assertEqual(_frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "30/1"}), 30.0)
        self.assertIsNone(_frame_rate({}))
        self.assertEqual(_bit_rate({"bit_rate": "192000"}, {}), 192000)
        self.assertEqual(_bit_rate({}, {"BPS-eng": "669000"}), 669000)
        self.assertIsNone(_bit_rate({}, {"title": "x"}))


if __name__ == "__main__":
    unittest.main()
