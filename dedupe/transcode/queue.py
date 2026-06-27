from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from ..events import CancellationToken
from .capabilities import check_encoder_capability
from .command_builder import PlanError, build_plan
from .models import (
    EncoderCapability,
    JobStatus,
    TranscodeProgress,
    TranscodeRequest,
    TranscodeResult,
)
from .presets import get_preset
from .probe import ProbeError, probe_media
from .runner import run_transcode
from .validation import validate_output


ProgressCallback = Callable[[TranscodeProgress], None]


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _commit_output(temporary: Path, output: Path) -> None:
    """Reserve the final name, then atomically replace our own empty reservation."""
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        os.replace(temporary, output)
    except BaseException:
        _cleanup(output)
        raise


class TranscodeQueue:
    """Run transcode requests sequentially while preserving every source file."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        progress_callback: ProgressCallback | None = None,
        keep_temporary_on_failure: bool = False,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.progress_callback = progress_callback
        self.keep_temporary_on_failure = keep_temporary_on_failure
        self._capabilities: dict[str, EncoderCapability] = {}

    def capability(self, preset_id: str) -> EncoderCapability:
        if preset_id not in self._capabilities:
            preset = get_preset(preset_id)
            self._capabilities[preset_id] = check_encoder_capability(preset, self.ffmpeg)
        return self._capabilities[preset_id]

    def run(
        self,
        requests: Iterable[TranscodeRequest],
        *,
        cancellation: CancellationToken | None = None,
    ) -> list[TranscodeResult]:
        token = cancellation or CancellationToken()
        queued = list(requests)
        results: list[TranscodeResult] = []
        for index, request in enumerate(queued):
            if token.is_cancelled:
                results.extend(self._skipped(item, "Queue cancelled") for item in queued[index:])
                break
            result = self._run_one(request, token)
            results.append(result)
            if result.status == JobStatus.CANCELLED:
                results.extend(self._skipped(item, "Queue cancelled") for item in queued[index + 1 :])
                break
        return results

    def _run_one(self, request: TranscodeRequest, token: CancellationToken) -> TranscodeResult:
        plan = None
        input_size = 0
        log_path = None
        started_elapsed = 0.0
        try:
            preset = get_preset(request.preset_id)
            capability = self.capability(request.preset_id)
            if not capability.available:
                return self._failed(request, capability.message or f"Encoder unavailable: {preset.encoder}")
            input_info = probe_media(request.input_path, self.ffprobe)
            input_size = input_info.size
            plan = build_plan(input_info, request.output_path, preset, ffmpeg=self.ffmpeg)
            log_path = plan.log_path
            runner = run_transcode(
                plan,
                cancellation=token,
                progress_callback=self.progress_callback,
            )
            started_elapsed = runner.elapsed_seconds
            if runner.status != JobStatus.COMPLETED:
                if not self.keep_temporary_on_failure:
                    _cleanup(plan.temporary_path)
                return TranscodeResult(
                    job_id=plan.job_id,
                    status=runner.status,
                    input_path=plan.input_path,
                    output_path=plan.output_path,
                    log_path=plan.log_path,
                    preset_id=request.preset_id,
                    message=runner.message,
                    input_size=input_size,
                    elapsed_seconds=runner.elapsed_seconds,
                )
            validation = validate_output(plan, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
            if not validation.valid:
                if not self.keep_temporary_on_failure:
                    _cleanup(plan.temporary_path)
                return TranscodeResult(
                    job_id=plan.job_id,
                    status=JobStatus.FAILED,
                    input_path=plan.input_path,
                    output_path=plan.output_path,
                    log_path=plan.log_path,
                    preset_id=request.preset_id,
                    message="; ".join(validation.errors),
                    input_size=input_size,
                    output_size=validation.output_info.size if validation.output_info else 0,
                    elapsed_seconds=runner.elapsed_seconds,
                    validation=validation,
                    warnings=validation.warnings,
                )
            _commit_output(plan.temporary_path, plan.output_path)
            output_size = plan.output_path.stat().st_size
            return TranscodeResult(
                job_id=plan.job_id,
                status=JobStatus.COMPLETED,
                input_path=plan.input_path,
                output_path=plan.output_path,
                log_path=plan.log_path,
                preset_id=request.preset_id,
                message="Transcode completed; source kept",
                input_size=input_size,
                output_size=output_size,
                elapsed_seconds=runner.elapsed_seconds,
                validation=validation,
                warnings=validation.warnings,
            )
        except (OSError, PlanError, ProbeError, ValueError) as exc:
            if plan is not None and not self.keep_temporary_on_failure:
                _cleanup(plan.temporary_path)
            return TranscodeResult(
                job_id=plan.job_id if plan else "",
                status=JobStatus.FAILED,
                input_path=request.input_path.expanduser().resolve(),
                output_path=request.output_path.expanduser().resolve(),
                log_path=log_path,
                preset_id=request.preset_id,
                message=str(exc),
                input_size=input_size,
                elapsed_seconds=started_elapsed,
            )

    @staticmethod
    def _failed(request: TranscodeRequest, message: str) -> TranscodeResult:
        return TranscodeResult(
            job_id="",
            status=JobStatus.FAILED,
            input_path=request.input_path.expanduser().resolve(),
            output_path=request.output_path.expanduser().resolve(),
            log_path=None,
            preset_id=request.preset_id,
            message=message,
        )

    @staticmethod
    def _skipped(request: TranscodeRequest, message: str) -> TranscodeResult:
        return TranscodeResult(
            job_id="",
            status=JobStatus.SKIPPED,
            input_path=request.input_path.expanduser().resolve(),
            output_path=request.output_path.expanduser().resolve(),
            log_path=None,
            preset_id=request.preset_id,
            message=message,
        )
