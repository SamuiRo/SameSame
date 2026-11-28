from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterable
from queue import Empty, Queue
from threading import Thread
from typing import TextIO

from .. import process
from ..events import CancellationToken
from .models import JobStatus, RunnerResult, TranscodePlan, TranscodeProgress


ProgressCallback = Callable[[TranscodeProgress], None]


def _seconds(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return 0.0


def _progress(plan: TranscodePlan, values: dict[str, str]) -> TranscodeProgress:
    elapsed = _seconds(values.get("out_time", ""))
    if elapsed <= 0:
        try:
            elapsed = int(values.get("out_time_us") or values.get("out_time_ms") or 0) / 1_000_000
        except ValueError:
            elapsed = 0.0
    duration = plan.input_info.duration
    percent = min(100.0, max(0.0, 100.0 * elapsed / duration)) if duration > 0 else 0.0
    try:
        fps = float(values["fps"]) if values.get("fps") else None
    except ValueError:
        fps = None
    return TranscodeProgress(
        job_id=plan.job_id,
        input_path=plan.input_path,
        seconds=elapsed,
        duration=duration,
        percent=percent,
        speed=values.get("speed"),
        fps=fps,
        message=values.get("progress", ""),
    )


def _read_lines(stream: Iterable[str], lines: Queue[str | None]) -> None:
    try:
        for line in stream:
            lines.put(str(line).strip())
    finally:
        lines.put(None)


def _remove_scratch(plan: TranscodePlan) -> None:
    for path in plan.scratch_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _prepare(plan: TranscodePlan, log_file: TextIO) -> str | None:
    """Run the plan's preparation commands; return an error message on failure."""
    for command in plan.preparation_commands:
        try:
            completed = process.run(command, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Cannot extract cover art: {exc}"
        if completed.stderr:
            log_file.write(completed.stderr)
        if completed.returncode != 0:
            diagnostic = (completed.stderr or "").strip().splitlines()
            detail = diagnostic[-1] if diagnostic else f"FFmpeg exited with code {completed.returncode}"
            return f"Cannot extract cover art: {detail}; see {plan.log_path}"
    return None


def run_transcode(
    plan: TranscodePlan,
    *,
    cancellation: CancellationToken | None = None,
    progress_callback: ProgressCallback | None = None,
) -> RunnerResult:
    try:
        return _run_transcode(plan, cancellation=cancellation, progress_callback=progress_callback)
    finally:
        _remove_scratch(plan)


def _run_transcode(
    plan: TranscodePlan,
    *,
    cancellation: CancellationToken | None,
    progress_callback: ProgressCallback | None,
) -> RunnerResult:
    token = cancellation or CancellationToken()
    started = time.monotonic()
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with plan.log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            preparation_error = _prepare(plan, log_file)
            if preparation_error is not None:
                return RunnerResult(JobStatus.FAILED, None, time.monotonic() - started, preparation_error)
            log_file.flush()
            ffmpeg_process = process.popen(
                plan.command,
                stdout=subprocess.PIPE,
                stderr=log_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert ffmpeg_process.stdout is not None
            lines: Queue[str | None] = Queue()
            reader = Thread(target=_read_lines, args=(ffmpeg_process.stdout, lines), daemon=True)
            reader.start()
            values: dict[str, str] = {}
            stream_finished = False
            while ffmpeg_process.poll() is None or not stream_finished:
                if token.is_cancelled and ffmpeg_process.poll() is None:
                    ffmpeg_process.terminate()
                    try:
                        ffmpeg_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        ffmpeg_process.kill()
                        ffmpeg_process.wait(timeout=5)
                try:
                    line = lines.get(timeout=0.2)
                except Empty:
                    continue
                if line is None:
                    stream_finished = True
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key] = value
                if key == "progress" and progress_callback is not None:
                    progress_callback(_progress(plan, values))
                    values.clear()
            return_code = ffmpeg_process.wait()
            reader.join(timeout=1)
            ffmpeg_process.stdout.close()
    except OSError as exc:
        return RunnerResult(
            status=JobStatus.FAILED,
            return_code=None,
            elapsed_seconds=time.monotonic() - started,
            message=f"Cannot start FFmpeg: {exc}",
        )

    elapsed = time.monotonic() - started
    if token.is_cancelled:
        return RunnerResult(JobStatus.CANCELLED, return_code, elapsed, "Transcode cancelled")
    if return_code != 0:
        return RunnerResult(
            JobStatus.FAILED,
            return_code,
            elapsed,
            f"FFmpeg exited with code {return_code}; see {plan.log_path}",
        )
    return RunnerResult(JobStatus.COMPLETED, return_code, elapsed)
