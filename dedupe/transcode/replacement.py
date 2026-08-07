from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..actions import ActionOutcome, FileAction, FileActionService, OperationStatus
from .command_builder import replacement_target
from .models import JobStatus, TranscodeResult
from .source_cleanup import cleanup_transcode_source


@dataclass(frozen=True, slots=True)
class ReplacementResult:
    success: bool
    source_path: Path
    encoded_path: Path
    target_path: Path
    message: str
    cleanup: ActionOutcome | None = None
    #: True when the source is gone but the encode never reached its target, so
    #: the only surviving copy sits at `encoded_path` under a staging name.
    needs_attention: bool = False


def _move_encoded(source: Path, destination: Path) -> None:
    shutil.move(str(source), str(destination))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def replace_source_with_transcode(
    result: TranscodeResult,
    *,
    cleanup_action: FileAction,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
    allow_unsafe_recycle: bool = False,
    allow_permanent_delete: bool = False,
) -> ReplacementResult:
    """Put a validated encode at its source's own path, under the source's name.

    The source has to go before the encode can take its path, so the two steps
    are ordered by which one leaves a good file on disk at every moment. When
    the source is not already an MKV its target path is free, so the encode
    moves in first and the source is cleaned up afterwards. When the source *is*
    the target, cleanup has to happen first — and only quarantine can be undone
    if the move then fails, which is why a failure there is reported loudly
    rather than swallowed.
    """
    source = result.input_path.resolve()
    encoded = result.output_path.resolve()
    target = replacement_target(source)

    def failure(message: str, *, cleanup: ActionOutcome | None = None, attention: bool = False) -> ReplacementResult:
        return ReplacementResult(False, source, encoded, target, message, cleanup, attention)

    if result.status != JobStatus.COMPLETED or result.validation is None or not result.validation.valid:
        return failure("Only a completed, validated transcode can replace its source")
    if not source.is_file() or not encoded.is_file():
        return failure("The source or encoded output is no longer available")
    if encoded == source:
        return failure("Encoded output must be separate from the source")
    if target != source and target.exists() and target != encoded:
        return failure(f"Replacement target already exists: {target}")
    if result.output_sha256 is None or result.output_modified_at is None:
        return failure("Transcode result does not carry the output identity required for replacement")

    encoded_stat = encoded.stat()
    if encoded_stat.st_size != result.output_size or abs(encoded_stat.st_mtime - result.output_modified_at) > 0.000001:
        return failure("Encoded output changed after validation")

    def move_into_place() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        _move_encoded(encoded, target)
        if target.stat().st_size != result.output_size or _sha256(target) != result.output_sha256:
            raise RuntimeError("Replaced output failed its content identity check")

    def discard_staging_log() -> None:
        """Drop the FFmpeg log once the job is done and verified.

        Its name is built from the staging path, so keeping it would leave a
        `<name>.samesame-new.mkv.ffmpeg.log` beside every file in the library,
        pointing at something that no longer exists. A failed replacement keeps
        its log, which is when the diagnostics are worth anything.
        """
        if result.log_path is None:
            return
        try:
            result.log_path.unlink(missing_ok=True)
        except OSError:
            pass

    def clean_up_source() -> ActionOutcome:
        return cleanup_transcode_source(
            result,
            action=cleanup_action,
            journal_path=journal_path,
            quarantine_root=quarantine_root,
            collection_root=collection_root,
            recycle=recycle,
            allow_unsafe_recycle=allow_unsafe_recycle,
            allow_permanent_delete=allow_permanent_delete,
        )

    if target != source:
        # The source keeps a different extension, so nothing occupies the target.
        try:
            move_into_place()
        except Exception as exc:  # noqa: BLE001 - the source is still intact here.
            return failure(f"Could not move the encode to {target}: {exc}")
        cleanup = clean_up_source()
        if cleanup.status != OperationStatus.COMPLETED:
            return ReplacementResult(
                False,
                source,
                encoded,
                target,
                f"Encode is in place at {target}, but the original was kept: {cleanup.message}",
                cleanup,
            )
        discard_staging_log()
        return ReplacementResult(True, source, encoded, target, f"Replaced {target.name}", cleanup)

    cleanup = clean_up_source()
    if cleanup.status != OperationStatus.COMPLETED:
        return failure(f"Source was kept, so the encode stays at {encoded.name}: {cleanup.message}", cleanup=cleanup)
    try:
        move_into_place()
    except Exception as exc:  # noqa: BLE001 - the source is already gone; say so plainly.
        if cleanup.action == FileAction.QUARANTINE:
            rollback = FileActionService(journal_path, quarantine_root).restore(cleanup.operation_id)
            if rollback.status == OperationStatus.COMPLETED:
                return failure(
                    f"Could not move the encode to {target}: {exc}; the original was restored and the "
                    f"encode is still at {encoded.name}",
                    cleanup=cleanup,
                )
            return failure(
                f"Could not move the encode to {target}: {exc}; restoring the original also failed "
                f"({rollback.message}). The encode is intact at {encoded}",
                cleanup=cleanup,
                attention=True,
            )
        return failure(
            f"Could not move the encode to {target}: {exc}. The original is gone and the only copy is "
            f"the validated encode at {encoded}",
            cleanup=cleanup,
            attention=True,
        )
    discard_staging_log()
    return ReplacementResult(True, source, encoded, target, f"Replaced {target.name}", cleanup)
