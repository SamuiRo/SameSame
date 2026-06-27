from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import (
    ActionOutcome,
    FileAction,
    FileIdentity,
    OperationRecord,
    OperationStatus,
    PreparedAction,
)


class OperationJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def __enter__(self) -> OperationJournal:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                destination TEXT,
                requested_at REAL NOT NULL,
                finished_at REAL,
                expected_size INTEGER,
                expected_mtime REAL,
                expected_hash TEXT,
                expected_hash_algorithm TEXT,
                group_id TEXT,
                related_operation_id TEXT,
                message TEXT NOT NULL DEFAULT '',
                reversible INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.commit()

    def record_requested(self, request: PreparedAction) -> None:
        identity = request.identity
        self.connection.execute(
            """
            INSERT INTO operations(
                operation_id, action, status, source, destination, requested_at,
                expected_size, expected_mtime, expected_hash,
                expected_hash_algorithm, group_id, related_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.operation_id,
                request.action.value,
                OperationStatus.REQUESTED.value,
                str(request.source),
                str(request.destination) if request.destination else None,
                time.time(),
                identity.size if identity else None,
                identity.modified_at if identity else None,
                identity.hash_value if identity else None,
                identity.hash_algorithm if identity else None,
                request.group_id,
                request.related_operation_id,
            ),
        )
        self.connection.commit()

    def record_outcome(self, outcome: ActionOutcome) -> None:
        self.connection.execute(
            """
            UPDATE operations
            SET status = ?, destination = ?, finished_at = ?, message = ?, reversible = ?
            WHERE operation_id = ?
            """,
            (
                outcome.status.value,
                str(outcome.destination) if outcome.destination else None,
                time.time(),
                outcome.message,
                int(outcome.reversible),
                outcome.operation_id,
            ),
        )
        self.connection.commit()

    def get(self, operation_id: str) -> OperationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def recent(self, limit: int = 500) -> list[OperationRecord]:
        rows = self.connection.execute(
            "SELECT * FROM operations ORDER BY requested_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OperationRecord:
        identity = None
        if row["expected_hash"] is not None:
            identity = FileIdentity(
                size=int(row["expected_size"]),
                modified_at=float(row["expected_mtime"]),
                hash_value=str(row["expected_hash"]),
                hash_algorithm=str(row["expected_hash_algorithm"]),
            )
        return OperationRecord(
            operation_id=str(row["operation_id"]),
            action=FileAction(str(row["action"])),
            status=OperationStatus(str(row["status"])),
            source=Path(str(row["source"])),
            destination=Path(str(row["destination"])) if row["destination"] else None,
            requested_at=float(row["requested_at"]),
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            identity=identity,
            group_id=str(row["group_id"]) if row["group_id"] else None,
            related_operation_id=str(row["related_operation_id"]) if row["related_operation_id"] else None,
            message=str(row["message"] or ""),
            reversible=bool(row["reversible"]),
        )
