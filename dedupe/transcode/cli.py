from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from ..events import CancellationToken
from .capabilities import check_encoder_capability
from .command_builder import build_plan, default_output_path
from .models import JobStatus, TranscodeProgress, TranscodeRequest, TranscodeResult
from .presets import PRESETS, get_preset
from .probe import probe_media
from .queue import TranscodeQueue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Source-preserving FFmpeg transcode queue")
    parser.add_argument("inputs", nargs="*", type=Path, help="video files to transcode")
    parser.add_argument("--preset", choices=tuple(PRESETS), default="anime_x265_balanced")
    parser.add_argument("--output", type=Path, help="output path (only with one input)")
    parser.add_argument("--output-dir", type=Path, help="directory for generated output names")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument("--check-capabilities", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="probe inputs and print planned FFmpeg commands")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--keep-temporary-on-failure", action="store_true")
    return parser


def _request_paths(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[TranscodeRequest]:
    if not args.inputs:
        parser.error("at least one input is required")
    if args.output is not None and len(args.inputs) != 1:
        parser.error("--output can only be used with one input")
    if args.output is not None and args.output_dir is not None:
        parser.error("--output and --output-dir cannot be used together")
    return [
        TranscodeRequest(
            input_path=source,
            output_path=args.output or default_output_path(source, args.preset, args.output_dir),
            preset_id=args.preset,
        )
        for source in args.inputs
    ]


def _result_payload(result: TranscodeResult) -> dict[str, object]:
    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "input": str(result.input_path),
        "output": str(result.output_path),
        "log": str(result.log_path) if result.log_path else None,
        "preset": result.preset_id,
        "message": result.message,
        "input_size": result.input_size,
        "output_size": result.output_size,
        "saved_bytes": result.saved_bytes,
        "savings_percent": round(result.savings_percent, 3),
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "warnings": list(result.warnings),
    }


def _print_progress(progress: TranscodeProgress) -> None:
    speed = f" {progress.speed}" if progress.speed else ""
    print(f"\r{progress.input_path.name}: {progress.percent:6.2f}%{speed}", end="", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_presets:
        for preset in PRESETS.values():
            print(f"{preset.preset_id:24} {preset.description}")
        if not args.inputs and not args.check_capabilities:
            return 0
    if args.check_capabilities:
        payload: list[dict[str, object]] = []
        for preset in PRESETS.values():
            capability = check_encoder_capability(preset, args.ffmpeg)
            payload.append(
                {
                    "preset": preset.preset_id,
                    "encoder": preset.encoder,
                    "available": capability.available,
                    "message": capability.message,
                }
            )
        if args.json_output:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            for item in payload:
                state = "available" if item["available"] else "unavailable"
                suffix = f": {item['message']}" if item["message"] else ""
                print(f"{item['preset']}: {state}{suffix}")
        if not args.inputs:
            return 0 if all(bool(item["available"]) for item in payload) else 1

    requests = _request_paths(args, parser)
    if args.dry_run:
        commands: list[dict[str, object]] = []
        for request in requests:
            info = probe_media(request.input_path, args.ffprobe)
            plan = build_plan(info, request.output_path, get_preset(request.preset_id), ffmpeg=args.ffmpeg)
            commands.append(
                {
                    "input": str(plan.input_path),
                    "output": str(plan.output_path),
                    "temporary": str(plan.temporary_path),
                    "log": str(plan.log_path),
                    "command": list(plan.command),
                }
            )
        if args.json_output:
            print(json.dumps(commands, indent=2, ensure_ascii=False))
        else:
            for item in commands:
                print(" ".join(str(part) for part in item["command"]))
        return 0

    token = CancellationToken()
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: token.cancel())
    queue = TranscodeQueue(
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        progress_callback=None if args.json_output else _print_progress,
        keep_temporary_on_failure=args.keep_temporary_on_failure,
    )
    try:
        results = queue.run(requests, cancellation=token)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    if not args.json_output:
        print(file=sys.stderr)
    if args.json_output:
        print(json.dumps([_result_payload(result) for result in results], indent=2, ensure_ascii=False))
    else:
        for result in results:
            savings = f", saved {result.savings_percent:.1f}%" if result.status == JobStatus.COMPLETED else ""
            print(f"{result.status.value}: {result.input_path} -> {result.output_path}{savings}")
            if result.message:
                print(f"  {result.message}")
            for warning in result.warnings:
                print(f"  warning: {warning}")
    if any(result.status == JobStatus.CANCELLED for result in results):
        return 130
    if token.is_cancelled:
        return 130
    return 0 if all(result.status == JobStatus.COMPLETED for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
