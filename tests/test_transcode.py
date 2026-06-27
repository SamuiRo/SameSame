from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.events import CancellationToken
from dedupe.transcode.capabilities import check_encoder_capability
from dedupe.transcode.command_builder import PlanError, build_plan
from dedupe.transcode.models import (
    JobStatus,
    MediaInfo,
    StreamInfo,
    TranscodePlan,
    TranscodeRequest,
    TranscodeResult,
)
from dedupe.transcode.presets import PRESETS
from dedupe.transcode.probe import list_encoders, probe_media
from dedupe.transcode.runner import run_transcode
from dedupe.transcode.queue import TranscodeQueue
from dedupe.transcode.validation import validate_output


def media(path: Path, *, size: int = 1000, duration: float = 60.0) -> MediaInfo:
    return MediaInfo(
        path=path,
        size=size,
        duration=duration,
        format_name="matroska",
        streams=(
            StreamInfo(0, "video", "h264"),
            StreamInfo(1, "audio", "aac"),
            StreamInfo(2, "audio", "aac"),
            StreamInfo(3, "subtitle", "ass"),
            StreamInfo(4, "attachment", "ttf"),
        ),
        chapter_count=2,
    )


class PresetAndPlanTests(unittest.TestCase):
    def test_all_four_presets_have_the_documented_encoder_settings(self) -> None:
        self.assertEqual(set(PRESETS), {
            "anime_x265_max",
            "anime_x265_balanced",
            "anime_av1_nvenc",
            "anime_hevc_nvenc",
        })
        self.assertIn(("-crf", "20"), list(zip(PRESETS["anime_x265_max"].video_args[::2], PRESETS["anime_x265_max"].video_args[1::2])))
        self.assertIn(("-crf", "22"), list(zip(PRESETS["anime_x265_balanced"].video_args[::2], PRESETS["anime_x265_balanced"].video_args[1::2])))
        self.assertIn("fullres", PRESETS["anime_av1_nvenc"].video_args)
        self.assertEqual(PRESETS["anime_hevc_nvenc"].encoder, "hevc_nvenc")

    def test_each_preset_is_included_verbatim_in_its_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            for preset in PRESETS.values():
                plan = build_plan(
                    media(source),
                    root / f"{preset.preset_id}.mkv",
                    preset,
                    ffmpeg=sys.executable,
                )
                start = plan.command.index("-c:v")
                self.assertEqual(plan.command[start : start + len(preset.video_args)], preset.video_args)

    def test_plan_preserves_streams_metadata_and_uses_unique_temporary_mkv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "вхідне відео.mkv"
            source.write_bytes(b"source")
            output = root / "результат.mkv"
            plan = build_plan(media(source), output, PRESETS["anime_x265_balanced"], ffmpeg=sys.executable)
            command = plan.command
            self.assertEqual(plan.input_path, source.resolve())
            self.assertEqual(plan.output_path, output.resolve())
            self.assertNotEqual(plan.temporary_path, plan.output_path)
            self.assertTrue(plan.temporary_path.name.endswith(".part.mkv"))
            for mapping in ("0:v?", "0:a?", "0:s?", "0:t?"):
                self.assertIn(mapping, command)
            self.assertIn("-map_metadata", command)
            self.assertIn("-map_chapters", command)
            self.assertIn("copy", command)

    def test_plan_rejects_existing_output_source_overwrite_and_non_mkv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            info = media(source)
            with self.assertRaises(PlanError):
                build_plan(info, source, PRESETS["anime_x265_max"], ffmpeg=sys.executable)
            existing = root / "existing.mkv"
            existing.write_bytes(b"keep")
            with self.assertRaises(PlanError):
                build_plan(info, existing, PRESETS["anime_x265_max"], ffmpeg=sys.executable)
            with self.assertRaises(PlanError):
                build_plan(info, root / "output.mp4", PRESETS["anime_x265_max"], ffmpeg=sys.executable)


class ProbeAndCapabilityTests(unittest.TestCase):
    @patch("dedupe.transcode.probe.resolve_binary", return_value="ffmpeg")
    @patch("dedupe.transcode.probe.subprocess.run")
    def test_encoder_listing(self, run_mock, _resolve_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess([], 0, " V....D libx265 H.265\n V..... hevc_nvenc NVIDIA\n", "")
        self.assertEqual(list_encoders(), {"libx265", "hevc_nvenc"})

    @patch("dedupe.transcode.capabilities.list_encoders", return_value={"libx265"})
    def test_missing_encoder_is_reported_before_running(self, _encoders_mock) -> None:
        capability = check_encoder_capability(PRESETS["anime_av1_nvenc"], sys.executable)
        self.assertFalse(capability.available)
        self.assertFalse(capability.listed)

    @patch("dedupe.transcode.probe.resolve_binary", return_value="ffprobe")
    @patch("dedupe.transcode.probe.subprocess.run")
    def test_probe_collects_streams_chapters_and_metadata(self, run_mock, _resolve_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "show.mkv"
            source.write_bytes(b"media")
            payload = {
                "format": {"duration": "24.5", "format_name": "matroska"},
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                    {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
                    {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}},
                    {"index": 3, "codec_type": "attachment", "codec_name": "ttf"},
                ],
                "chapters": [{}, {}],
            }
            run_mock.return_value = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            info = probe_media(source)
            self.assertEqual(info.duration, 24.5)
            self.assertEqual(info.stream_count("audio"), 1)
            self.assertEqual(info.stream_count("subtitle"), 1)
            self.assertEqual(info.stream_count("attachment"), 1)
            self.assertEqual(info.chapter_count, 2)


class RunnerAndValidationTests(unittest.TestCase):
    def _plan(self, root: Path, command: tuple[str, ...]) -> TranscodePlan:
        source = root / "source.mkv"
        source.write_bytes(b"source")
        return TranscodePlan(
            job_id="job",
            input_path=source,
            output_path=root / "output.mkv",
            temporary_path=root / ".output.part.mkv",
            log_path=root / "output.log",
            preset_id="anime_x265_balanced",
            input_info=media(source, size=100, duration=10),
            command=command,
        )

    def test_runner_parses_machine_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "print('out_time=00:00:05.000000');print('speed=2x');print('progress=end')"
            plan = self._plan(root, (sys.executable, "-c", script))
            events = []
            result = run_transcode(plan, progress_callback=events.append)
            self.assertEqual(result.status, JobStatus.COMPLETED)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].percent, 50.0)

    def test_runner_honours_pre_cancelled_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = CancellationToken()
            token.cancel()
            root = Path(directory)
            plan = self._plan(root, (sys.executable, "-c", "import time; time.sleep(10)"))
            result = run_transcode(plan, cancellation=token)
            self.assertEqual(result.status, JobStatus.CANCELLED)
            self.assertLess(result.elapsed_seconds, 6)

    def test_runner_reports_ffmpeg_failure_and_keeps_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "import sys; print('encoder failed', file=sys.stderr); raise SystemExit(3)"
            plan = self._plan(root, (sys.executable, "-c", script))
            result = run_transcode(plan)
            self.assertEqual(result.status, JobStatus.FAILED)
            self.assertEqual(result.return_code, 3)
            self.assertIn("encoder failed", plan.log_path.read_text(encoding="utf-8"))

    @patch("dedupe.transcode.validation.resolve_binary", return_value="ffmpeg")
    @patch("dedupe.transcode.validation.subprocess.run")
    @patch("dedupe.transcode.validation.probe_media")
    def test_validation_accepts_preserved_tracks_and_warns_when_larger(self, probe_mock, run_mock, _resolve_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._plan(root, (sys.executable, "-c", ""))
            plan.temporary_path.write_bytes(b"x" * 200)
            probe_mock.return_value = MediaInfo(
                path=plan.temporary_path,
                size=200,
                duration=10.2,
                format_name="matroska",
                streams=plan.input_info.streams,
                chapter_count=2,
            )
            run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = validate_output(plan)
            self.assertTrue(result.valid)
            self.assertTrue(result.warnings)

    @patch("dedupe.transcode.validation.resolve_binary", return_value="ffmpeg")
    @patch("dedupe.transcode.validation.subprocess.run")
    @patch("dedupe.transcode.validation.probe_media")
    def test_validation_rejects_missing_tracks_bad_duration_and_decode(self, probe_mock, run_mock, _resolve_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._plan(root, (sys.executable, "-c", ""))
            plan.temporary_path.write_bytes(b"broken")
            probe_mock.return_value = MediaInfo(
                path=plan.temporary_path,
                size=10,
                duration=20,
                format_name="matroska",
                streams=(StreamInfo(0, "video"),),
            )
            run_mock.return_value = subprocess.CompletedProcess([], 1, "", "decode failed")
            result = validate_output(plan)
            self.assertFalse(result.valid)
            self.assertGreaterEqual(len(result.errors), 5)


class QueueTests(unittest.TestCase):
    def test_queue_accepts_request_scoped_custom_preset(self) -> None:
        from dedupe.events import CancellationToken
        from dedupe.transcode.models import EncoderCapability
        from dedupe.transcode.presets import TranscodePreset

        custom = TranscodePreset(
            "custom_test",
            "Custom test",
            "libx265",
            ("-c:v", "libx265", "-crf", "24", "-preset", "medium", "-pix_fmt", "yuv420p10le"),
        )
        request = TranscodeRequest(Path("source.mkv"), Path("output.mkv"), custom.preset_id, custom)
        queue = TranscodeQueue()
        capability = EncoderCapability(custom.encoder, False, False, "custom encoder unavailable")
        with patch.object(queue, "_capability_for_preset", return_value=capability) as check_mock:
            result = queue._run_one(request, CancellationToken())
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertIn("custom encoder unavailable", result.message)
        check_mock.assert_called_once_with(custom)

    def test_queue_runs_jobs_in_order_and_continues_after_a_failure(self) -> None:
        requests = [
            TranscodeRequest(Path("first.mkv"), Path("first.out.mkv"), "anime_x265_balanced"),
            TranscodeRequest(Path("second.mkv"), Path("second.out.mkv"), "anime_x265_balanced"),
        ]
        produced = [
            TranscodeResult(
                "one",
                JobStatus.FAILED,
                requests[0].input_path,
                requests[0].output_path,
                None,
                requests[0].preset_id,
            ),
            TranscodeResult(
                "two",
                JobStatus.COMPLETED,
                requests[1].input_path,
                requests[1].output_path,
                None,
                requests[1].preset_id,
            ),
        ]
        queue = TranscodeQueue()
        with patch.object(queue, "_run_one", side_effect=produced) as run_mock:
            results = queue.run(requests)
        self.assertEqual(results, produced)
        self.assertEqual([call.args[0] for call in run_mock.call_args_list], requests)

    def test_queue_reports_each_result_to_callback_in_order(self) -> None:
        requests = [
            TranscodeRequest(Path("first.mkv"), Path("first.out.mkv"), "anime_x265_balanced"),
            TranscodeRequest(Path("second.mkv"), Path("second.out.mkv"), "anime_x265_balanced"),
        ]
        produced = [
            TranscodeResult("one", JobStatus.FAILED, requests[0].input_path, requests[0].output_path, None, requests[0].preset_id),
            TranscodeResult("two", JobStatus.COMPLETED, requests[1].input_path, requests[1].output_path, None, requests[1].preset_id),
        ]
        callbacks: list[TranscodeResult] = []
        queue = TranscodeQueue(result_callback=callbacks.append)
        with patch.object(queue, "_run_one", side_effect=produced):
            queue.run(requests)
        self.assertEqual(callbacks, produced)


if __name__ == "__main__":
    unittest.main()
