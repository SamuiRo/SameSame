from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ..models import FileRecord
from .journal import OperationJournal
from .models import ActionOutcome, FileAction, FileIdentity, OperationRecord, OperationStatus, PreparedAction
from .preflight import PreflightError, prepare_identity, verify_content, verify_identity


def _retry_sharing_violation(operation: Callable[[], None], attempts: int = 8, delay: float = 0.25) -> None:
    for attempt in range(attempts):
        try:
            operation()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) != 32 or attempt + 1 >= attempts:
                raise
            time.sleep(delay)


class FileActionService:
    def __init__(
        self,
        journal_path: Path,
        quarantine_root: Path,
        *,
        recycle: Callable[[str], None] | None = None,
        allow_unsafe_recycle: bool = False,
    ) -> None:
        self.journal_path = journal_path
        self.quarantine_root = quarantine_root
        self._recycle = recycle
        self.allow_unsafe_recycle = allow_unsafe_recycle or recycle is not None

    def prepare(
        self,
        record: FileRecord,
        action: FileAction,
        *,
        group_id: str | None = None,
    ) -> PreparedAction:
        if action == FileAction.RESTORE:
            raise ValueError("Use restore() for quarantine restoration")
        identity = prepare_identity(record) if action in {FileAction.QUARANTINE, FileAction.RECYCLE} else None
        request = PreparedAction(
            operation_id=uuid.uuid4().hex,
            action=action,
            source=record.path.resolve(),
            collection_root=record.root.resolve(),
            identity=identity,
            quarantine_root=self.quarantine_root.resolve() if action == FileAction.QUARANTINE else None,
            group_id=group_id,
        )
        if action == FileAction.QUARANTINE:
            request = replace(request, destination=self._quarantine_destination(request))
        return request

    def perform(
        self,
        record: FileRecord,
        action: FileAction,
        *,
        group_id: str | None = None,
    ) -> ActionOutcome:
        try:
            request = self.prepare(record, action, group_id=group_id)
        except Exception as exc:  # noqa: BLE001 - failed preflight must still be journaled.
            request = PreparedAction(
                operation_id=uuid.uuid4().hex,
                action=action,
                source=record.path.resolve(),
                collection_root=record.root.resolve(),
                identity=None,
                quarantine_root=self.quarantine_root.resolve() if action == FileAction.QUARANTINE else None,
                group_id=group_id,
            )
            outcome = ActionOutcome(
                request.operation_id,
                action,
                OperationStatus.FAILED,
                request.source,
                message=str(exc),
                group_id=group_id,
            )
            with OperationJournal(self.journal_path) as journal:
                journal.record_requested(request)
                journal.record_outcome(outcome)
            return outcome
        return self.execute(request)

    def perform_if_matches(
        self,
        record: FileRecord,
        action: FileAction,
        reference: FileRecord,
        *,
        group_id: str | None = None,
    ) -> ActionOutcome:
        """Perform a mutation only when source and keeper still have identical SHA-256 content."""
        request: PreparedAction | None = None
        try:
            request = self.prepare(record, action, group_id=group_id)
            reference_identity = prepare_identity(reference)
            if request.identity is None:
                raise PreflightError("Content comparison requires a preflight identity")
            if (
                request.identity.size != reference_identity.size
                or request.identity.hash_algorithm != reference_identity.hash_algorithm
                or request.identity.hash_value != reference_identity.hash_value
            ):
                outcome = ActionOutcome(
                    request.operation_id,
                    action,
                    OperationStatus.SKIPPED,
                    request.source,
                    message="Source no longer matches the selected keeper by SHA-256; no file was moved",
                    group_id=group_id,
                )
                with OperationJournal(self.journal_path) as journal:
                    journal.record_requested(request)
                    journal.record_outcome(outcome)
                return outcome
        except Exception as exc:  # noqa: BLE001 - comparison failures must be journaled without mutating files.
            request = request or PreparedAction(
                operation_id=uuid.uuid4().hex,
                action=action,
                source=record.path.resolve(),
                collection_root=record.root.resolve(),
                identity=None,
                quarantine_root=self.quarantine_root.resolve() if action == FileAction.QUARANTINE else None,
                group_id=group_id,
            )
            outcome = ActionOutcome(
                request.operation_id,
                action,
                OperationStatus.FAILED,
                request.source,
                destination=request.destination,
                message=f"Cannot compare source with selected keeper: {exc}",
                group_id=group_id,
            )
            with OperationJournal(self.journal_path) as journal:
                journal.record_requested(request)
                journal.record_outcome(outcome)
            return outcome
        return self.execute(request)

    def execute(self, request: PreparedAction) -> ActionOutcome:
        with OperationJournal(self.journal_path) as journal:
            journal.record_requested(request)
            destination = request.destination
            try:
                if request.action in {FileAction.KEEP, FileAction.IGNORE}:
                    outcome = ActionOutcome(
                        request.operation_id,
                        request.action,
                        OperationStatus.COMPLETED,
                        request.source,
                        message=f"Review decision recorded: {request.action.value}",
                        group_id=request.group_id,
                    )
                elif request.action == FileAction.QUARANTINE:
                    if request.identity is None or request.quarantine_root is None:
                        raise PreflightError("Quarantine request is missing preflight identity or destination root")
                    verify_identity(request.source, request.identity)
                    destination = request.destination or self._quarantine_destination(request)
                    if destination.exists():
                        destination = self._available_path(destination)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _retry_sharing_violation(lambda: shutil.move(str(request.source), str(destination)))
                    try:
                        verify_content(destination, request.identity)
                    except Exception as exc:
                        rollback = self._rollback_move(destination, request.source, request.identity)
                        destination = destination if destination.exists() else None
                        raise RuntimeError(f"Quarantine verification failed: {exc}; {rollback}") from exc
                    outcome = ActionOutcome(
                        request.operation_id,
                        request.action,
                        OperationStatus.COMPLETED,
                        request.source,
                        destination=destination,
                        message="File moved to quarantine",
                        reversible=True,
                        group_id=request.group_id,
                    )
                elif request.action == FileAction.RECYCLE:
                    if request.identity is None:
                        raise PreflightError("Recycle request is missing preflight identity")
                    if not self.allow_unsafe_recycle:
                        raise RuntimeError(
                            "Operating-system recycle is blocked by Safe mode because Windows may permanently "
                            "delete files when Recycle Bin is disabled or unavailable; use quarantine instead"
                        )
                    verify_identity(request.source, request.identity)
                    recycle = self._recycle
                    if recycle is None:
                        try:
                            from send2trash import send2trash
                        except ImportError as exc:
                            raise RuntimeError("Recycle support requires Send2Trash; install the GUI extra") from exc
                        recycle = send2trash
                    _retry_sharing_violation(lambda: recycle(str(request.source)))
                    if request.source.exists():
                        raise RuntimeError(f"Recycle integration returned but the source still exists: {request.source}")
                    outcome = ActionOutcome(
                        request.operation_id,
                        request.action,
                        OperationStatus.COMPLETED,
                        request.source,
                        message="File sent to the operating-system recycle bin",
                        group_id=request.group_id,
                    )
                elif request.action == FileAction.RESTORE:
                    if request.identity is None or request.destination is None:
                        raise PreflightError("Restore request is missing identity or original path")
                    if request.destination.exists():
                        raise PreflightError(f"Cannot restore because the original path exists: {request.destination}")
                    verify_content(request.source, request.identity)
                    request.destination.parent.mkdir(parents=True, exist_ok=True)
                    _retry_sharing_violation(lambda: shutil.move(str(request.source), str(request.destination)))
                    try:
                        verify_content(request.destination, request.identity)
                    except Exception as exc:
                        rollback = self._rollback_move(request.destination, request.source, request.identity)
                        destination = request.destination if request.destination.exists() else None
                        raise RuntimeError(f"Restore verification failed: {exc}; {rollback}") from exc
                    destination = request.destination
                    outcome = ActionOutcome(
                        request.operation_id,
                        request.action,
                        OperationStatus.COMPLETED,
                        request.source,
                        destination=destination,
                        message="Quarantined file restored to its original path",
                        group_id=request.group_id,
                    )
                else:
                    raise ValueError(f"Unsupported action: {request.action}")
            except Exception as exc:  # noqa: BLE001 - persist every recoverable operation failure.
                outcome = ActionOutcome(
                    request.operation_id,
                    request.action,
                    OperationStatus.FAILED,
                    request.source,
                    destination=destination,
                    message=str(exc),
                    group_id=request.group_id,
                )
            journal.record_outcome(outcome)
            return outcome

    def record_skipped(
        self,
        record: FileRecord,
        action: FileAction,
        message: str,
        *,
        group_id: str | None = None,
    ) -> ActionOutcome:
        request = PreparedAction(
            operation_id=uuid.uuid4().hex,
            action=action,
            source=record.path.resolve(),
            collection_root=record.root.resolve(),
            identity=None,
            group_id=group_id,
        )
        outcome = ActionOutcome(
            request.operation_id,
            action,
            OperationStatus.SKIPPED,
            request.source,
            message=message,
            group_id=group_id,
        )
        with OperationJournal(self.journal_path) as journal:
            journal.record_requested(request)
            journal.record_outcome(outcome)
        return outcome

    def restore(self, operation_id: str) -> ActionOutcome:
        with OperationJournal(self.journal_path) as journal:
            original = journal.get(operation_id)
        if original is None:
            raise ValueError(f"Unknown operation: {operation_id}")
        if (
            original.action != FileAction.QUARANTINE
            or original.status != OperationStatus.COMPLETED
            or not original.reversible
            or original.destination is None
            or original.identity is None
        ):
            raise ValueError("Only completed reversible quarantine operations can be restored")
        request = PreparedAction(
            operation_id=uuid.uuid4().hex,
            action=FileAction.RESTORE,
            source=original.destination,
            collection_root=original.source.parent,
            identity=original.identity,
            destination=original.source,
            group_id=original.group_id,
            related_operation_id=original.operation_id,
        )
        return self.execute(request)

    def recent_operations(self, limit: int = 500) -> list[OperationRecord]:
        with OperationJournal(self.journal_path) as journal:
            return journal.recent(limit)

    def _quarantine_destination(self, request: PreparedAction) -> Path:
        if request.quarantine_root is None:
            raise PreflightError("Quarantine root is not configured")
        try:
            relative = request.source.relative_to(request.collection_root)
        except ValueError:
            relative = Path(request.source.name)
        root_name = request.collection_root.name or "root"
        root_digest = hashlib.sha256(str(request.collection_root).casefold().encode("utf-8")).hexdigest()[:8]
        destination = request.quarantine_root / f"{root_name}-{root_digest}" / relative
        return self._available_path(destination)

    @staticmethod
    def _available_path(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(1, 10000):
            candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"Cannot allocate a collision-free quarantine path for {path}")

    @staticmethod
    def _rollback_move(current: Path, original: Path, identity: FileIdentity) -> str:
        if original.exists():
            return "rollback skipped because the original path is occupied"
        if not current.exists():
            return "rollback failed because the moved file is unavailable"
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            _retry_sharing_violation(lambda: shutil.move(str(current), str(original)))
            verify_content(original, identity)
            return "source restored to its original location"
        except Exception as exc:  # noqa: BLE001 - preserve the primary failure while reporting rollback state.
            return f"rollback failed: {exc}"
