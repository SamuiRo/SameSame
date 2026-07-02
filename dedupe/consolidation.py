from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .name_normalizer import fallback_normalize

HASH_CHUNK_SIZE = 4 * 1024 * 1024
MTIME_TOLERANCE = 0.000001
WRAPPER_FOLDER_RE = re.compile(r"^(?:folder|source|copy|set)[ _.-]*\d+$", re.IGNORECASE)
TITLE_ID_PREFIX_RE = re.compile(r"^\s*\[F\d+\]\s*", re.IGNORECASE)
INVALID_WINDOWS_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PlanStatus(str, Enum):
    READY = "ready"
    ALREADY_IN_PLACE = "already in place"
    CONFLICT = "conflict"


class BatchStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled back"
    FAILED = "failed"
    UNDONE = "undone"


@dataclass(frozen=True, slots=True)
class FolderMapping:
    source: Path
    relative_destination: Path
    file_count: int


@dataclass(frozen=True, slots=True)
class PlannedMove:
    source: Path
    destination: Path
    size: int
    modified_at: float
    status: PlanStatus
    message: str = ""


@dataclass(frozen=True, slots=True)
class ConsolidationPlan:
    title_root: Path
    final_root: Path
    mappings: tuple[FolderMapping, ...]
    moves: tuple[PlannedMove, ...]

    @property
    def ready_moves(self) -> tuple[PlannedMove, ...]:
        return tuple(move for move in self.moves if move.status == PlanStatus.READY)

    @property
    def conflicts(self) -> tuple[PlannedMove, ...]:
        return tuple(move for move in self.moves if move.status == PlanStatus.CONFLICT)

    def select_moves(self, selected: Iterable[PlannedMove]) -> ConsolidationPlan:
        selected_keys = {(str(move.source), str(move.destination)) for move in selected}
        return replace(
            self,
            moves=tuple(
                move
                for move in self.moves
                if move.status != PlanStatus.READY or (str(move.source), str(move.destination)) in selected_keys
            ),
        )


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    batch_id: str
    status: BatchStatus
    title_root: Path
    final_root: Path
    moved_count: int
    message: str


@dataclass(frozen=True, slots=True)
class ConsolidationBatch:
    batch_id: str
    status: BatchStatus
    title_root: Path
    final_root: Path
    created_at: float
    finished_at: float | None
    message: str
    moved_count: int


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def validate_folder_name(name: str) -> str:
    value = name.strip().rstrip(". ")
    if not value or value in {".", ".."}:
        raise ValueError("Final folder name cannot be empty")
    if INVALID_WINDOWS_NAME_RE.search(value):
        raise ValueError("Final folder name contains characters that Windows does not allow")
    return value


def validate_relative_destination(value: str | Path) -> Path:
    text = str(value).strip()
    if text in {"", "."}:
        return Path()
    path = Path(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Target subfolder must stay inside the final title folder")
    if any(INVALID_WINDOWS_NAME_RE.search(part) or part.rstrip(". ") != part for part in path.parts):
        raise ValueError("Target subfolder contains characters that Windows does not allow")
    return path


class ConsolidationPlanner:
    def discover_source_folders(self, title_root: Path) -> list[Path]:
        root = title_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Title folder is unavailable: {root}")
        sources: list[Path] = []
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = sorted(name for name in directory_names if not (Path(current) / name).is_symlink())
            if file_names:
                sources.append(Path(current).resolve())
        return sorted(sources, key=lambda path: (len(path.parts), str(path).casefold()))

    @staticmethod
    def suggest_final_name(title_root: Path) -> str:
        suggestion = TITLE_ID_PREFIX_RE.sub("", title_root.name).strip() or title_root.name
        try:
            return validate_folder_name(suggestion)
        except ValueError:
            return validate_folder_name(title_root.name)

    def suggest_mapping(self, title_root: Path, source: Path, final_name: str) -> FolderMapping:
        root = title_root.expanduser().resolve()
        resolved_source = source.expanduser().resolve()
        if not _is_relative_to(resolved_source, root):
            raise ValueError(f"Source folder is outside the selected title folder: {resolved_source}")
        relative = resolved_source.relative_to(root)
        meaningful_parts = tuple(part for part in relative.parts if not WRAPPER_FOLDER_RE.fullmatch(part))
        leaf_name = meaningful_parts[-1] if meaningful_parts else ""
        relative_destination = self._suggest_relative_destination(leaf_name, final_name)
        file_count = sum(1 for path in resolved_source.iterdir() if path.is_file())
        return FolderMapping(resolved_source, relative_destination, file_count)

    @staticmethod
    def _suggest_relative_destination(leaf_name: str, final_name: str) -> Path:
        if not leaf_name:
            return Path()
        leaf_core = fallback_normalize(leaf_name).core_title.casefold().strip()
        title_core = fallback_normalize(final_name).core_title.casefold().strip()
        leaf_compact = "".join(character for character in leaf_core if character.isalnum())
        title_compact = "".join(character for character in title_core if character.isalnum())
        if leaf_compact == title_compact:
            return Path()
        if title_compact and leaf_compact.endswith(title_compact):
            return Path()
        return Path(leaf_name)

    def suggested_mappings(self, title_root: Path, final_name: str) -> list[FolderMapping]:
        mappings = [
            self.suggest_mapping(title_root, source, final_name) for source in self.discover_source_folders(title_root)
        ]
        if len(mappings) == 1:
            return [replace(mappings[0], relative_destination=Path())]
        return mappings

    def build_plan(
        self,
        title_root: Path,
        final_name: str,
        mappings: Iterable[FolderMapping],
    ) -> ConsolidationPlan:
        root = title_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Title folder is unavailable: {root}")
        name = validate_folder_name(final_name)
        requested_final_root = root.parent / name
        if requested_final_root.is_symlink():
            raise ValueError(f"Final title folder cannot be a symbolic link: {requested_final_root}")
        final_root = requested_final_root.resolve()
        if final_root.parent != root.parent.resolve():
            raise ValueError(f"Final title folder must stay beside the selected title folder: {final_root}")
        normalized_mappings: list[FolderMapping] = []
        planned: list[PlannedMove] = []
        destinations: dict[str, list[int]] = {}
        for mapping in mappings:
            source = mapping.source.expanduser().resolve()
            if not source.is_dir() or not _is_relative_to(source, root):
                raise ValueError(f"Source folder is unavailable or outside the title folder: {source}")
            relative_destination = validate_relative_destination(mapping.relative_destination)
            direct_files = sorted(
                (path for path in source.iterdir() if path.is_file()), key=lambda path: path.name.casefold()
            )
            normalized_mappings.append(FolderMapping(source, relative_destination, len(direct_files)))
            for path in direct_files:
                resolved_source = path.resolve()
                if path.is_symlink() or not _is_relative_to(resolved_source, root):
                    raise ValueError(f"Source file cannot be a symbolic link or leave the title folder: {path}")
                destination = (final_root / relative_destination / path.name).resolve()
                if not _is_relative_to(destination, final_root):
                    raise ValueError(f"Destination escapes the final title folder: {destination}")
                stat = resolved_source.stat()
                if _path_key(resolved_source) == _path_key(destination):
                    status = PlanStatus.ALREADY_IN_PLACE
                    message = "File is already at the proposed destination"
                elif destination.exists():
                    status = PlanStatus.CONFLICT
                    message = "Destination already exists; SameSame will never overwrite it"
                else:
                    status = PlanStatus.READY
                    message = "Ready to move"
                index = len(planned)
                planned.append(PlannedMove(resolved_source, destination, stat.st_size, stat.st_mtime, status, message))
                destinations.setdefault(_path_key(destination), []).append(index)
        for indexes in destinations.values():
            if len(indexes) < 2:
                continue
            for index in indexes:
                move = planned[index]
                if move.status == PlanStatus.READY:
                    planned[index] = replace(
                        move,
                        status=PlanStatus.CONFLICT,
                        message="Several selected files have the same proposed destination",
                    )
        return ConsolidationPlan(root, final_root, tuple(normalized_mappings), tuple(planned))


class ConsolidationService:
    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path

    def execute(
        self,
        plan: ConsolidationPlan,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ConsolidationResult:
        moves = list(plan.ready_moves)
        if not moves:
            raise ValueError("The consolidation plan contains no selected files ready to move")
        batch_id = uuid.uuid4().hex
        connection = self._connect()
        completed: list[tuple[str, PlannedMove, str]] = []
        try:
            self._record_batch(connection, batch_id, plan, BatchStatus.RUNNING)
            total = len(moves)
            for index, move in enumerate(moves, start=1):
                if progress:
                    progress(index - 1, total, f"Validating {move.source.name}")
                move_id = uuid.uuid4().hex
                self._record_move_requested(connection, move_id, batch_id, move)
                try:
                    digest = self._move_one(move)
                except Exception as exc:
                    self._record_move_status(connection, move_id, "failed", str(exc), None)
                    rollback_errors = self._rollback_completed(connection, completed)
                    if move.destination.exists() and not move.source.exists():
                        rollback_errors.append(f"{move.destination}: current file could not be restored")
                    self._remove_empty_tree(
                        plan.final_root, protected=set(), include_root=plan.final_root != plan.title_root
                    )
                    status = BatchStatus.FAILED if rollback_errors else BatchStatus.ROLLED_BACK
                    message = f"Consolidation failed at {move.source}: {exc}"
                    if rollback_errors:
                        message += "; rollback problems: " + "; ".join(rollback_errors)
                    self._finish_batch(connection, batch_id, status, message)
                    return ConsolidationResult(
                        batch_id,
                        status,
                        plan.title_root,
                        plan.final_root,
                        0 if not rollback_errors else len(completed),
                        message,
                    )
                completed.append((move_id, move, digest))
                self._record_move_status(connection, move_id, "completed", "File moved and SHA-256 verified", digest)
                if progress:
                    progress(index, total, f"Moved {move.source.name}")
            self._remove_empty_tree(
                plan.title_root,
                protected={plan.final_root},
                include_root=plan.final_root != plan.title_root,
            )
            message = f"Moved and verified {len(completed)} file(s)"
            self._finish_batch(connection, batch_id, BatchStatus.COMPLETED, message)
            return ConsolidationResult(
                batch_id,
                BatchStatus.COMPLETED,
                plan.title_root,
                plan.final_root,
                len(completed),
                message,
            )
        finally:
            connection.close()

    def undo(
        self,
        batch_id: str,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ConsolidationResult:
        connection = self._connect()
        try:
            batch_row = connection.execute(
                "SELECT * FROM consolidation_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch_row is None:
                raise ValueError(f"Unknown consolidation batch: {batch_id}")
            status = BatchStatus(str(batch_row["status"]))
            if status not in {BatchStatus.COMPLETED, BatchStatus.RUNNING, BatchStatus.FAILED}:
                raise ValueError("Only a completed or interrupted consolidation batch can be undone")
            rows = connection.execute(
                "SELECT * FROM consolidation_moves WHERE batch_id = ? AND status = 'completed' ORDER BY sequence DESC",
                (batch_id,),
            ).fetchall()
            total = len(rows)
            for index, row in enumerate(rows, start=1):
                source = Path(str(row["source"]))
                destination = Path(str(row["destination"]))
                digest = str(row["sha256"] or "")
                if progress:
                    progress(index - 1, total, f"Restoring {source.name}")
                if source.exists():
                    raise FileExistsError(f"Cannot undo because the original path is occupied: {source}")
                if not destination.is_file() or _hash_file(destination) != digest:
                    raise RuntimeError(f"Cannot undo because the moved file changed or is unavailable: {destination}")
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
                if not source.is_file() or _hash_file(source) != digest:
                    raise RuntimeError(f"Undo verification failed: {source}")
                connection.execute(
                    "UPDATE consolidation_moves SET status = 'undone', message = ? WHERE move_id = ?",
                    ("Restored to original path and SHA-256 verified", str(row["move_id"])),
                )
                connection.commit()
                if progress:
                    progress(index, total, f"Restored {source.name}")
            final_root = Path(str(batch_row["final_root"]))
            self._remove_empty_tree(
                final_root,
                protected=set(),
                include_root=not bool(batch_row["final_root_existed"]),
            )
            message = f"Restored {total} file(s) to their original paths"
            self._finish_batch(connection, batch_id, BatchStatus.UNDONE, message)
            return ConsolidationResult(
                batch_id,
                BatchStatus.UNDONE,
                Path(str(batch_row["title_root"])),
                final_root,
                total,
                message,
            )
        finally:
            connection.close()

    def latest_undoable_batch(self) -> ConsolidationBatch | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT b.*, COUNT(m.move_id) AS moved_count
                FROM consolidation_batches b
                LEFT JOIN consolidation_moves m ON m.batch_id = b.batch_id AND m.status = 'completed'
                WHERE b.status IN (?, ?, ?)
                GROUP BY b.batch_id
                HAVING COUNT(m.move_id) > 0
                ORDER BY b.created_at DESC
                LIMIT 1
                """,
                (BatchStatus.COMPLETED.value, BatchStatus.RUNNING.value, BatchStatus.FAILED.value),
            ).fetchone()
            return self._batch_from_row(row) if row is not None else None
        finally:
            connection.close()

    def _move_one(self, move: PlannedMove) -> str:
        stat = move.source.stat()
        if not move.source.is_file():
            raise ValueError(f"Source is not a regular file: {move.source}")
        if stat.st_size != move.size or abs(stat.st_mtime - move.modified_at) > MTIME_TOLERANCE:
            raise RuntimeError(f"File changed after preview was generated: {move.source}")
        if move.destination.exists():
            raise FileExistsError(f"Destination appeared after preview; no file was overwritten: {move.destination}")
        digest = _hash_file(move.source)
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.destination))
        try:
            if not move.destination.is_file() or move.destination.stat().st_size != move.size:
                raise RuntimeError("destination size does not match")
            if _hash_file(move.destination) != digest:
                raise RuntimeError("destination SHA-256 does not match")
        except Exception as exc:
            rollback_message = self._rollback_one(move, digest)
            raise RuntimeError(f"Move verification failed: {exc}; {rollback_message}") from exc
        return digest

    def _rollback_completed(
        self,
        connection: sqlite3.Connection,
        completed: list[tuple[str, PlannedMove, str]],
    ) -> list[str]:
        errors: list[str] = []
        for move_id, move, digest in reversed(completed):
            message = self._rollback_one(move, digest)
            if message.startswith("restored"):
                self._record_move_status(connection, move_id, "rolled_back", message, digest)
            else:
                errors.append(f"{move.destination}: {message}")
        return errors

    @staticmethod
    def _rollback_one(move: PlannedMove, digest: str) -> str:
        if move.source.exists():
            return "rollback blocked because the original path is occupied"
        if not move.destination.is_file():
            return "rollback blocked because the destination is unavailable"
        try:
            if _hash_file(move.destination) != digest:
                return "rollback blocked because destination content changed"
            move.source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.destination), str(move.source))
            if _hash_file(move.source) != digest:
                return "rollback verification failed"
            return "restored to original path and SHA-256 verified"
        except Exception as exc:  # noqa: BLE001 - retain the primary failure and report rollback state.
            return f"rollback failed: {exc}"

    @staticmethod
    def _remove_empty_tree(root: Path, *, protected: set[Path], include_root: bool = False) -> None:
        if not root.exists() or not root.is_dir():
            return
        protected_keys = {_path_key(path) for path in protected}
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        if include_root:
            directories.append(root)
        for directory in directories:
            if _path_key(directory) in protected_keys:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.journal_path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_batches (
                batch_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                title_root TEXT NOT NULL,
                final_root TEXT NOT NULL,
                final_root_existed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                finished_at REAL,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        batch_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(consolidation_batches)").fetchall()
        }
        if "final_root_existed" not in batch_columns:
            connection.execute(
                "ALTER TABLE consolidation_batches ADD COLUMN final_root_existed INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_moves (
                move_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                expected_size INTEGER NOT NULL,
                expected_mtime REAL NOT NULL,
                sha256 TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(batch_id) REFERENCES consolidation_batches(batch_id)
            )
            """
        )
        connection.commit()
        return connection

    @staticmethod
    def _record_batch(
        connection: sqlite3.Connection,
        batch_id: str,
        plan: ConsolidationPlan,
        status: BatchStatus,
    ) -> None:
        connection.execute(
            """
            INSERT INTO consolidation_batches(
                batch_id, status, title_root, final_root, final_root_existed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                status.value,
                str(plan.title_root),
                str(plan.final_root),
                int(plan.final_root.exists()),
                time.time(),
            ),
        )
        connection.commit()

    @staticmethod
    def _record_move_requested(
        connection: sqlite3.Connection,
        move_id: str,
        batch_id: str,
        move: PlannedMove,
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COUNT(*) FROM consolidation_moves WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO consolidation_moves(
                move_id, batch_id, sequence, source, destination,
                expected_size, expected_mtime, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested')
            """,
            (
                move_id,
                batch_id,
                sequence,
                str(move.source),
                str(move.destination),
                move.size,
                move.modified_at,
            ),
        )
        connection.commit()

    @staticmethod
    def _record_move_status(
        connection: sqlite3.Connection,
        move_id: str,
        status: str,
        message: str,
        digest: str | None,
    ) -> None:
        connection.execute(
            "UPDATE consolidation_moves SET status = ?, message = ?, sha256 = ? WHERE move_id = ?",
            (status, message, digest, move_id),
        )
        connection.commit()

    @staticmethod
    def _finish_batch(
        connection: sqlite3.Connection,
        batch_id: str,
        status: BatchStatus,
        message: str,
    ) -> None:
        connection.execute(
            "UPDATE consolidation_batches SET status = ?, finished_at = ?, message = ? WHERE batch_id = ?",
            (status.value, time.time(), message, batch_id),
        )
        connection.commit()

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> ConsolidationBatch:
        return ConsolidationBatch(
            batch_id=str(row["batch_id"]),
            status=BatchStatus(str(row["status"])),
            title_root=Path(str(row["title_root"])),
            final_root=Path(str(row["final_root"])),
            created_at=float(row["created_at"]),
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            message=str(row["message"] or ""),
            moved_count=int(row["moved_count"]),
        )
