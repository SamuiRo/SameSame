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
XXHASH_ALGO = "xxh3_128"
BLAKE2B_ALGO = "blake2b-128"


def current_hash_algo() -> str:
    try:
        import xxhash  # noqa: F401  # type: ignore
    except ImportError:
        return BLAKE2B_ALGO
    return XXHASH_ALGO


def _new_hasher() -> tuple[str, object]:
    try:
        import xxhash  # type: ignore

        return XXHASH_ALGO, xxhash.xxh3_128()
    except ImportError:
        return BLAKE2B_ALGO, hashlib.blake2b(digest_size=16)


def _hash_digest(hasher: object) -> str:
    if hasattr(hasher, "hexdigest"):
        return str(hasher.hexdigest())
    raise TypeError("Unsupported hasher")


def _update_hash(hasher: object, data: bytes) -> None:
    if hasattr(hasher, "update"):
        hasher.update(data)
        return
    raise TypeError("Unsupported hasher")


def partial_hash(path: Path, size: int) -> tuple[str, str]:
    offsets = {0}
    if size > PARTIAL_CHUNK_SIZE:
        offsets.add(max(0, size - PARTIAL_CHUNK_SIZE))
    for fraction in (1 / 3, 1 / 2, 2 / 3):
        offsets.add(max(0, min(size - PARTIAL_CHUNK_SIZE, int(size * fraction))))

    algo, hasher = _new_hasher()
    _update_hash(hasher, str(size).encode("ascii"))
    with path.open("rb") as fh:
        for offset in sorted(offsets):
            fh.seek(offset)
            _update_hash(hasher, fh.read(PARTIAL_CHUNK_SIZE))
    return algo, _hash_digest(hasher)


def full_hash(path: Path) -> tuple[str, str]:
    algo, hasher = _new_hasher()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            _update_hash(hasher, chunk)
    return algo, _hash_digest(hasher)


def _compute_partial(record: FileRecord, algo: str) -> tuple[str, str | None, str | None]:
    if record.partial_hash and record.partial_hash_algo == algo:
        return record.path_key, record.partial_hash, record.partial_hash_algo
    try:
        computed_algo, value = partial_hash(record.path, record.size)
        return record.path_key, value, computed_algo
    except OSError as exc:
        LOGGER.warning("Cannot partially hash %s: %s", record.path, exc)
        return record.path_key, None, None


def _compute_full(record: FileRecord, algo: str) -> tuple[str, str | None, str | None]:
    if record.full_hash and record.full_hash_algo == algo:
        return record.path_key, record.full_hash, record.full_hash_algo
    try:
        computed_algo, value = full_hash(record.path)
        return record.path_key, value, computed_algo
    except OSError as exc:
        LOGGER.warning("Cannot fully hash %s: %s", record.path, exc)
        return record.path_key, None, None


def find_exact_duplicates(records: list[FileRecord], cache: Cache, workers: int = 4) -> list[ExactDuplicateGroup]:
    algo = current_hash_algo()
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        by_size[record.size].append(record)

    partial_targets = [
        record
        for group in by_size.values()
        if len(group) > 1
        for record in group
        if not record.partial_hash or record.partial_hash_algo != algo
    ]
    if partial_targets:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_compute_partial, record, algo): record for record in partial_targets}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Partial hashes", unit="file"):
                record = futures[future]
                _, value, computed_algo = future.result()
                if value:
                    record.partial_hash = value
                    record.partial_hash_algo = computed_algo
                    cache.upsert_file(record)
        cache.conn.commit()

    by_size_partial: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    for group in by_size.values():
        if len(group) <= 1:
            continue
        for record in group:
            if record.partial_hash and record.partial_hash_algo == algo:
                by_size_partial[(record.size, record.partial_hash)].append(record)

    full_targets = [
        record
        for group in by_size_partial.values()
        if len(group) > 1
        for record in group
        if not record.full_hash or record.full_hash_algo != algo
    ]
    if full_targets:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_compute_full, record, algo): record for record in full_targets}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Full hashes", unit="file"):
                record = futures[future]
                _, value, computed_algo = future.result()
                if value:
                    record.full_hash = value
                    record.full_hash_algo = computed_algo
                    cache.upsert_file(record)
        cache.conn.commit()

    by_full: dict[str, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.full_hash and record.full_hash_algo == algo:
            by_full[record.full_hash].append(record)

    groups = [
        ExactDuplicateGroup(hash_value=hash_value, paths=sorted(r.path_key for r in group), size=group[0].size)
        for hash_value, group in by_full.items()
        if len(group) > 1
    ]
    groups.sort(key=lambda item: (-item.size, item.paths))
    return groups
