from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FileAction(str, Enum):
    KEEP = "keep"
    IGNORE = "ignore"
    QUARANTINE = "quarantine"
    RECYCLE = "recycle"
    RESTORE = "restore"


class OperationStatus(str, Enum):
    REQUESTED = "requested"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    size: int
    modified_at: float
    hash_value: str
    hash_algorithm: str


@dataclass(frozen=True, slots=True)
class PreparedAction:
    operation_id: str
    action: FileAction
    source: Path
    collection_root: Path
    identity: FileIdentity | None
    quarantine_root: Path | None = None
    destination: Path | None = None
    group_id: str | None = None
    related_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    operation_id: str
    action: FileAction
    status: OperationStatus
    source: Path
    destination: Path | None = None
    message: str = ""
    reversible: bool = False
    group_id: str | None = None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    action: FileAction
    status: OperationStatus
    source: Path
    destination: Path | None
    requested_at: float
    finished_at: float | None
    identity: FileIdentity | None
    group_id: str | None
    related_operation_id: str | None
    message: str
    reversible: bool
