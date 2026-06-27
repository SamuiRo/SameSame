from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import FileRecord
from .models import FileIdentity

MTIME_TOLERANCE = 0.000001
IDENTITY_HASH_ALGORITHM = "sha256"
HASH_CHUNK_SIZE = 4 * 1024 * 1024


class PreflightError(RuntimeError):
    pass


def _stat_file(path: Path) -> tuple[int, float]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise PreflightError(f"Cannot access source file: {path}: {exc}") from exc
    if not path.is_file():
        raise PreflightError(f"Source is not a regular file: {path}")
    return stat.st_size, stat.st_mtime


def _identity_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_identity(record: FileRecord) -> FileIdentity:
    size, modified_at = _stat_file(record.path)
    if size != record.size or abs(modified_at - record.mtime) > MTIME_TOLERANCE:
        raise PreflightError(f"File changed since the scan: {record.path}")
    try:
        hash_value = _identity_hash(record.path)
    except OSError as exc:
        raise PreflightError(f"Cannot hash source file: {record.path}: {exc}") from exc
    return FileIdentity(size, modified_at, hash_value, IDENTITY_HASH_ALGORITHM)


def verify_identity(path: Path, expected: FileIdentity) -> None:
    size, modified_at = _stat_file(path)
    if size != expected.size:
        raise PreflightError(f"File size changed before action: {path}")
    if abs(modified_at - expected.modified_at) > MTIME_TOLERANCE:
        raise PreflightError(f"File modification time changed before action: {path}")
    try:
        hash_value = _identity_hash(path)
    except OSError as exc:
        raise PreflightError(f"Cannot verify source file: {path}: {exc}") from exc
    if expected.hash_algorithm != IDENTITY_HASH_ALGORITHM or hash_value != expected.hash_value:
        raise PreflightError(f"File content changed before action: {path}")


def verify_content(path: Path, expected: FileIdentity) -> None:
    """Verify content after a move without requiring the original mtime."""

    size, _modified_at = _stat_file(path)
    if size != expected.size:
        raise PreflightError(f"Moved file size does not match preflight identity: {path}")
    hash_value = _identity_hash(path)
    if expected.hash_algorithm != IDENTITY_HASH_ALGORITHM or hash_value != expected.hash_value:
        raise PreflightError(f"Moved file content does not match preflight identity: {path}")
