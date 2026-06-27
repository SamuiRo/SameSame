from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from queue import Empty, Queue
from threading import Thread

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


def _read_lines(stream: object, lines: Queue[str | None]) -> None:
    try:
        for line in stream:  # type: ignore[union-attr]
            lines.put(str(line).strip())
    finally:
        lines.put(None)


def run_transcode(
    plan: TranscodePlan,
    *,
    cancellation: CancellationToken | None = None,
    progress_callback: ProgressCallback | None = None,
) -> RunnerResult:
    token = cancellation or CancellationToken()
    started = time.monotonic()
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with plan.log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            process = subprocess.Popen(
                plan.command,
                stdout=subprocess.PIPE,
                stderr=log_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            lines: Queue[str | None] = Queue()
            reader = Thread(target=_read_lines, args=(process.stdout, lines), daemon=True)
            reader.start()
            values: dict[str, str] = {}
            stream_finished = False
            while process.poll() is None or not stream_finished:
                if token.is_cancelled and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
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
            return_code = process.wait()
            reader.join(timeout=1)
            process.stdout.close()
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
