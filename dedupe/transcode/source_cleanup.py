from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from ..actions.models import ActionOutcome, FileAction, FileIdentity, PreparedAction
from ..actions.preflight import IDENTITY_HASH_ALGORITHM
from ..actions.service import FileActionService
from .models import JobStatus, TranscodeResult


def cleanup_transcode_source(
    result: TranscodeResult,
    *,
    action: FileAction,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
    allow_unsafe_recycle: bool = False,
) -> ActionOutcome:
    """Identity-check and quarantine/recycle a source only after a validated transcode."""
    if action not in {FileAction.QUARANTINE, FileAction.RECYCLE}:
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
    )
    return service.execute(request)


def recycle_transcode_source(
    result: TranscodeResult,
    *,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
) -> ActionOutcome:
    return cleanup_transcode_source(
        result,
        action=FileAction.RECYCLE,
        journal_path=journal_path,
        quarantine_root=quarantine_root,
        collection_root=collection_root,
        recycle=recycle,
        allow_unsafe_recycle=recycle is not None,
    )
