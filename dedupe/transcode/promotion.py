from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..actions import ActionOutcome, FileAction, FileActionService, OperationStatus
from ..models import FileRecord
from .models import JobStatus, TranscodeResult


@dataclass(frozen=True, slots=True)
class PromotionResult:
    success: bool
    source_path: Path
    encoded_path: Path
    target_path: Path
    message: str
    quarantine: ActionOutcome | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _move_encoded(source: Path, destination: Path) -> None:
    shutil.move(str(source), str(destination))


def promote_transcode(
    result: TranscodeResult,
    *,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
) -> PromotionResult:
    """Quarantine the source, then promote a verified MKV into the collection."""
    source = result.input_path.resolve()
    encoded = result.output_path.resolve()
    target = source if source.suffix.casefold() == ".mkv" else source.with_suffix(".mkv")
    if result.status != JobStatus.COMPLETED or result.validation is None or not result.validation.valid:
        return PromotionResult(False, source, encoded, target, "Only a completed, validated transcode can be promoted")
    if not source.is_file() or not encoded.is_file():
        return PromotionResult(False, source, encoded, target, "The source or encoded output is no longer available")
    if encoded == source:
        return PromotionResult(False, source, encoded, target, "Encoded output must be separate from the source")
    if target != source and target.exists() and target != encoded:
        return PromotionResult(False, source, encoded, target, f"Replacement target already exists: {target}")

    source_stat = source.stat()
    if (
        result.input_sha256 is None
        or result.input_modified_at is None
        or source_stat.st_size != result.input_size
        or abs(source_stat.st_mtime - result.input_modified_at) > 0.000001
        or _sha256(source) != result.input_sha256
    ):
        return PromotionResult(False, source, encoded, target, "Source changed after transcoding")

    encoded_size = encoded.stat().st_size
    if result.output_sha256 is None:
        return PromotionResult(False, source, encoded, target, "Validated output identity is unavailable")
    encoded_hash = _sha256(encoded)
    if encoded_size != result.output_size or encoded_hash != result.output_sha256:
        return PromotionResult(False, source, encoded, target, "Encoded output changed after validation")
    source_stat = source.stat()
    root = (collection_root or source.parent).resolve()
    record = FileRecord(source, root, source_stat.st_size, source_stat.st_mtime, source.stem)
    action_service = FileActionService(journal_path, quarantine_root)
    quarantine = action_service.perform(record, FileAction.QUARANTINE)
    if quarantine.status != OperationStatus.COMPLETED:
        return PromotionResult(False, source, encoded, target, quarantine.message, quarantine)

    try:
        if target == encoded:
            promoted = target
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _move_encoded(encoded, target)
            promoted = target
        if promoted.stat().st_size != encoded_size or _sha256(promoted) != encoded_hash:
            raise RuntimeError("Promoted output failed its content identity check")
    except Exception as exc:  # noqa: BLE001 - attempt rollback after every promotion failure.
        try:
            if target != encoded and target.exists() and not encoded.exists():
                _move_encoded(target, encoded)
        except OSError:
            pass
        rollback = action_service.restore(quarantine.operation_id)
        rollback_note = "source restored" if rollback.status == OperationStatus.COMPLETED else f"restore failed: {rollback.message}"
        return PromotionResult(False, source, encoded, target, f"Promotion failed: {exc}; {rollback_note}", quarantine)

    return PromotionResult(
        True,
        source,
        encoded,
        promoted,
        "Original quarantined and validated transcode promoted",
        quarantine,
    )
