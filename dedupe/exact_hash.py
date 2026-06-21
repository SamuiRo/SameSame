from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .cache import Cache
from .models import ExactDuplicateGroup, FileRecord
from .progress import tqdm

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024
PARTIAL_CHUNK_SIZE = 64 * 1024


def _new_hasher() -> object:
    try:
        import xxhash  # type: ignore

        return xxhash.xxh3_128()
    except ImportError:
        return hashlib.blake2b(digest_size=16)


def _hash_digest(hasher: object) -> str:
    if hasattr(hasher, "hexdigest"):
        return str(hasher.hexdigest())
    raise TypeError("Unsupported hasher")


def _update_hash(hasher: object, data: bytes) -> None:
    if hasattr(hasher, "update"):
        hasher.update(data)
        return
    raise TypeError("Unsupported hasher")


def partial_hash(path: Path, size: int) -> str:
    offsets = {0}
    if size > PARTIAL_CHUNK_SIZE:
        offsets.add(max(0, size - PARTIAL_CHUNK_SIZE))
    for fraction in (1 / 3, 1 / 2, 2 / 3):
        offsets.add(max(0, min(size - PARTIAL_CHUNK_SIZE, int(size * fraction))))

    hasher = _new_hasher()
    _update_hash(hasher, str(size).encode("ascii"))
    with path.open("rb") as fh:
        for offset in sorted(offsets):
            fh.seek(offset)
            _update_hash(hasher, fh.read(PARTIAL_CHUNK_SIZE))
    return _hash_digest(hasher)


def full_hash(path: Path) -> str:
    hasher = _new_hasher()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            _update_hash(hasher, chunk)
    return _hash_digest(hasher)


def _compute_partial(record: FileRecord) -> tuple[str, str | None]:
    if record.partial_hash:
        return record.path_key, record.partial_hash
    try:
        return record.path_key, partial_hash(record.path, record.size)
    except OSError as exc:
        LOGGER.warning("Cannot partially hash %s: %s", record.path, exc)
        return record.path_key, None


def _compute_full(record: FileRecord) -> tuple[str, str | None]:
    if record.full_hash:
        return record.path_key, record.full_hash
    try:
        return record.path_key, full_hash(record.path)
    except OSError as exc:
        LOGGER.warning("Cannot fully hash %s: %s", record.path, exc)
        return record.path_key, None


def find_exact_duplicates(records: list[FileRecord], cache: Cache, workers: int = 4) -> list[ExactDuplicateGroup]:
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        by_size[record.size].append(record)

    partial_targets = [record for group in by_size.values() if len(group) > 1 for record in group if not record.partial_hash]
    if partial_targets:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_compute_partial, record): record for record in partial_targets}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Partial hashes", unit="file"):
                record = futures[future]
                _, value = future.result()
                if value:
                    record.partial_hash = value
                    cache.upsert_file(record)
        cache.conn.commit()

    by_size_partial: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    for group in by_size.values():
        if len(group) <= 1:
            continue
        for record in group:
            if record.partial_hash:
                by_size_partial[(record.size, record.partial_hash)].append(record)

    full_targets = [
        record
        for group in by_size_partial.values()
        if len(group) > 1
        for record in group
        if not record.full_hash
    ]
    if full_targets:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_compute_full, record): record for record in full_targets}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Full hashes", unit="file"):
                record = futures[future]
                _, value = future.result()
                if value:
                    record.full_hash = value
                    cache.upsert_file(record)
        cache.conn.commit()

    by_full: dict[str, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.full_hash:
            by_full[record.full_hash].append(record)

    groups = [
        ExactDuplicateGroup(hash_value=hash_value, paths=sorted(r.path_key for r in group), size=group[0].size)
        for hash_value, group in by_full.items()
        if len(group) > 1
    ]
    groups.sort(key=lambda item: (-item.size, item.paths))
    return groups
