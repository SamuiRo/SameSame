from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

from ..actions.models import MUTATING_ACTIONS, ActionOutcome, FileAction, FileIdentity, PreparedAction
from ..actions.preflight import IDENTITY_HASH_ALGORITHM
from ..actions.service import FileActionService
from .models import JobStatus, TranscodeResult


def _volume_of(path: Path) -> str:
    """Identify the volume a path lives on, so callers can predict a move's cost."""
    resolved = path.expanduser()
    # The path itself may not exist yet (an unconfigured quarantine root, say),
    # so walk up to the nearest ancestor that does before asking the filesystem.
    for candidate in (resolved, *resolved.parents):
        if candidate.exists():
            resolved = candidate
            break
    drive = os.path.splitdrive(str(resolved.absolute()))[0]
    return os.path.normcase(drive)


def cleanup_frees_source_space(action: FileAction, sources: Iterable[Path], quarantine_root: Path) -> bool:
    """Report whether a cleanup action actually releases bytes on the sources' volume.

    Quarantine is a move. Within one volume that is a rename, so the originals
    keep occupying exactly as much space as before and a "remove originals"
    batch can leave the disk fuller than it started. Recycle and permanent
    delete always release the bytes (recycle only once the bin is emptied, which
    Windows may skip entirely when the bin is disabled for that drive).
    """
    if action not in MUTATING_ACTIONS:
        raise ValueError(f"Unsupported transcode source cleanup action: {action}")
    if action != FileAction.QUARANTINE:
        return True
    target = _volume_of(quarantine_root)
    return all(_volume_of(source) != target for source in sources)


def cleanup_transcode_source(
    result: TranscodeResult,
    *,
    action: FileAction,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
    allow_unsafe_recycle: bool = False,
    allow_permanent_delete: bool = False,
) -> ActionOutcome:
    """Identity-check and quarantine/recycle/delete a source only after a validated transcode."""
    if action not in MUTATING_ACTIONS:
        raise ValueError(f"Unsupported transcode source cleanup action: {action}")
    if result.status != JobStatus.COMPLETED or result.validation is None or not result.validation.valid:
        raise ValueError("Only a completed and validated transcode can recycle its source")
    if result.input_modified_at is None or not result.input_sha256 or result.input_size <= 0:
        raise ValueError("Transcode result does not contain the source identity required for recycling")

    source = result.input_path.expanduser().resolve()
    root = (collection_root or source.parent).expanduser().resolve()
    request = PreparedAction(
        operation_id=uuid.uuid4().hex,
        action=action,
        source=source,
        collection_root=root,
        identity=FileIdentity(
            size=result.input_size,
            modified_at=result.input_modified_at,
            hash_value=result.input_sha256,
            hash_algorithm=IDENTITY_HASH_ALGORITHM,
        ),
        quarantine_root=quarantine_root.resolve() if action == FileAction.QUARANTINE else None,
        group_id=f"transcode:{result.job_id}",
    )
    service = FileActionService(
        journal_path,
        quarantine_root,
        recycle=recycle,
        allow_unsafe_recycle=allow_unsafe_recycle,
        allow_permanent_delete=allow_permanent_delete,
    )
    return service.execute(request)


def recycle_transcode_source(
    result: TranscodeResult,
    *,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
    allow_unsafe_recycle: bool = False,
) -> ActionOutcome:
    return cleanup_transcode_source(
        result,
        action=FileAction.RECYCLE,
        journal_path=journal_path,
        quarantine_root=quarantine_root,
        collection_root=collection_root,
        recycle=recycle,
        allow_unsafe_recycle=allow_unsafe_recycle,
    )
