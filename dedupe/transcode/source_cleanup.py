from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from ..actions.models import ActionOutcome, FileAction, FileIdentity, PreparedAction
from ..actions.preflight import IDENTITY_HASH_ALGORITHM
from ..actions.service import FileActionService
from .models import JobStatus, TranscodeResult


def recycle_transcode_source(
    result: TranscodeResult,
    *,
    journal_path: Path,
    quarantine_root: Path,
    collection_root: Path | None = None,
    recycle: Callable[[str], None] | None = None,
) -> ActionOutcome:
    """Identity-check and recycle a source only after a validated transcode."""
    if result.status != JobStatus.COMPLETED or result.validation is None or not result.validation.valid:
        raise ValueError("Only a completed and validated transcode can recycle its source")
    if result.input_modified_at is None or not result.input_sha256 or result.input_size <= 0:
        raise ValueError("Transcode result does not contain the source identity required for recycling")

    source = result.input_path.expanduser().resolve()
    root = (collection_root or source.parent).expanduser().resolve()
    request = PreparedAction(
        operation_id=uuid.uuid4().hex,
        action=FileAction.RECYCLE,
        source=source,
        collection_root=root,
        identity=FileIdentity(
            size=result.input_size,
            modified_at=result.input_modified_at,
            hash_value=result.input_sha256,
            hash_algorithm=IDENTITY_HASH_ALGORITHM,
        ),
        group_id=f"transcode:{result.job_id}",
    )
    service = FileActionService(journal_path, quarantine_root, recycle=recycle)
    return service.execute(request)
