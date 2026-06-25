from __future__ import annotations

import io
import json
import logging
import math
import shutil
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from .cache import Cache
from .models import FileRecord, VideoMatch
from .progress import tqdm

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

PIL_AVAILABLE = Image is not None

LOGGER = logging.getLogger(__name__)
SAMPLE_POINTS = (0.10, 0.30, 0.50, 0.70, 0.90)


def resolve_binary(name_or_path: str) -> str | None:
    candidate = Path(name_or_path)
    if candidate.exists():
        return str(candidate)
    return shutil.which(name_or_path)


def check_video_tools(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> tuple[str | None, str | None]:
    return resolve_binary(ffmpeg), resolve_binary(ffprobe)


def get_duration(path: Path, ffprobe: str) -> float | None:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        value = json.loads(result.stdout).get("format", {}).get("duration")
        return float(value) if value is not None else None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("Cannot read video duration for %s: %s", path, exc)
        return None


def _extract_frame(path: Path, timestamp: float, ffmpeg: str) -> Image.Image | None:
    if Image is None:
        return None
    command = [
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=60)
        if not result.stdout:
            return None
        image = Image.open(io.BytesIO(result.stdout))
        return image.convert("L")
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.warning("Cannot extract frame from %s at %.2fs: %s", path, timestamp, exc)
        return None


@lru_cache(maxsize=8)
def _cosine_table(size: int, hash_size: int) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(math.cos(((2 * position + 1) * frequency * math.pi) / (2 * size)) for position in range(size))
        for frequency in range(hash_size)
    )


def phash(image: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    if Image is None:
        raise RuntimeError("Pillow is required for perceptual video hashing.")
    size = hash_size * highfreq_factor
    image = image.resize((size, size), Image.Resampling.LANCZOS).convert("L")
    pixels = [[image.getpixel((x, y)) for x in range(size)] for y in range(size)]
    cosines = _cosine_table(size, hash_size)
    row_coefficients = [
        [sum(row[x] * cosines[v][x] for x in range(size)) for v in range(hash_size)]
        for row in pixels
    ]
    coeffs: list[float] = []
    for u in range(hash_size):
        for v in range(hash_size):
            total = sum(row_coefficients[y][v] * cosines[u][y] for y in range(size))
            cu = 1 / math.sqrt(2) if u == 0 else 1
            cv = 1 / math.sqrt(2) if v == 0 else 1
            coeffs.append(0.25 * cu * cv * total)
    median_values = sorted(coeffs[1:])
    median = median_values[len(median_values) // 2]
    value = 0
    for coeff in coeffs:
        value = (value << 1) | int(coeff > median)
    return value


def fingerprint_video(
    path: Path,
    duration: float,
    ffmpeg: str,
    *,
    sample_span: float | None = None,
    start_offset: float = 0.0,
) -> list[int] | None:
    span = duration if sample_span is None else min(sample_span, max(0.0, duration - start_offset))
    hashes: list[int] = []
    for fraction in SAMPLE_POINTS:
        timestamp = max(0.0, start_offset + span * fraction)
        frame = _extract_frame(path, timestamp, ffmpeg)
        if frame is None:
            return None
        hashes.append(phash(frame))
    return hashes


def hamming_similarity(left: list[int], right: list[int], bits_per_hash: int = 64) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    total_bits = len(left) * bits_per_hash
    distance = sum((a ^ b).bit_count() for a, b in zip(left, right))
    return max(0.0, 100.0 * (1.0 - distance / total_bits))


def _candidate_pairs(
    candidates: list[FileRecord],
    max_candidates_per_bucket: int,
    threshold: float = 90.0,
) -> set[tuple[int, int]]:
    if len(candidates) <= max_candidates_per_bucket:
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}

    fingerprints = [record.fingerprint for record in candidates]
    fingerprint_lengths = {len(fingerprint) for fingerprint in fingerprints if fingerprint}
    if len(fingerprint_lengths) != 1 or any(not fingerprint for fingerprint in fingerprints):
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}

    sample_count = fingerprint_lengths.pop()
    total_bits = sample_count * 64
    if threshold <= 0:
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}
    if threshold > 100:
        return set()

    # If a pair is within the allowed total Hamming distance, at least one of
    # max_distance + 1 disjoint blocks must be identical (pigeonhole principle).
    # Indexing those blocks reduces the candidate set without dropping any
    # pair that can satisfy the configured threshold.
    max_distance = math.floor(total_bits * (1.0 - threshold / 100.0) + 1e-9)
    block_count = min(total_bits, max_distance + 1)
    base_block_size, larger_blocks = divmod(total_bits, block_count)
    block_sizes = [base_block_size + (1 if index < larger_blocks else 0) for index in range(block_count)]

    blocks: dict[tuple[int, int], list[int]] = defaultdict(list)
    fingerprint_mask = (1 << 64) - 1
    for index, fingerprint in enumerate(fingerprints):
        combined = 0
        for hash_value in fingerprint or []:
            combined = (combined << 64) | (hash_value & fingerprint_mask)
        remaining_bits = total_bits
        for block_index, block_size in enumerate(block_sizes):
            remaining_bits -= block_size
            block_mask = (1 << block_size) - 1
            block_value = (combined >> remaining_bits) & block_mask
            blocks[(block_index, block_value)].append(index)

    pairs: set[tuple[int, int]] = set()
    for indexes in blocks.values():
        if len(indexes) < 2:
            continue
        for left_pos, left in enumerate(indexes):
            for right in indexes[left_pos + 1 :]:
                pairs.add((min(left, right), max(left, right)))
    return pairs


def _ensure_duration(record: FileRecord, ffprobe: str) -> tuple[str, float | None]:
    if record.duration is not None:
        return record.path_key, record.duration
    return record.path_key, get_duration(record.path, ffprobe)


def _ensure_fingerprint(record: FileRecord, ffmpeg: str) -> tuple[str, list[int] | None]:
    if record.fingerprint is not None:
        return record.path_key, record.fingerprint
    if record.duration is None:
        return record.path_key, None
    return record.path_key, fingerprint_video(record.path, record.duration, ffmpeg)


def _duration_aligned_similarity(
    left: FileRecord,
    right: FileRecord,
    ffmpeg: str,
    cache: dict[tuple[str, float, float], list[int] | None],
) -> float:
    if left.duration is None or right.duration is None or left.fingerprint is None or right.fingerprint is None:
        return 0.0
    shorter, longer = (left, right) if left.duration <= right.duration else (right, left)
    duration_delta = longer.duration - shorter.duration
    if duration_delta <= 0.05:
        return hamming_similarity(left.fingerprint, right.fingerprint)

    best = 0.0
    for start_offset in (0.0, duration_delta):
        key = (longer.path_key, round(shorter.duration, 3), round(start_offset, 3))
        if key not in cache:
            cache[key] = fingerprint_video(
                longer.path,
                longer.duration,
                ffmpeg,
                sample_span=shorter.duration,
                start_offset=start_offset,
            )
        aligned = cache[key]
        if aligned:
            best = max(best, hamming_similarity(shorter.fingerprint, aligned))
    return best


def find_video_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    ffmpeg: str,
    ffprobe: str,
    workers: int = 2,
    duration_tolerance: float = 2.0,
    max_candidates_per_bucket: int = 250,
) -> list[VideoMatch]:
    missing_duration = [record for record in records if record.duration is None]
    if missing_duration:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_ensure_duration, record, ffprobe): record for record in missing_duration}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Video durations", unit="file"):
                record = futures[future]
                _, duration = future.result()
                if duration is not None:
                    record.duration = duration
                    cache.upsert_file(record)
        cache.conn.commit()

    missing_fingerprint = [record for record in records if record.duration is not None and record.fingerprint is None]
    if missing_fingerprint:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_ensure_fingerprint, record, ffmpeg): record for record in missing_fingerprint}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Video fingerprints", unit="file"):
                record = futures[future]
                _, fingerprint = future.result()
                if fingerprint:
                    record.fingerprint = fingerprint
                    cache.upsert_file(record)
        cache.conn.commit()

    buckets: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.duration is not None and record.fingerprint:
            buckets[int(round(record.duration))].append(record)

    matches: list[VideoMatch] = []
    seen: set[tuple[str, str]] = set()
    aligned_fingerprints: dict[tuple[str, float, float], list[int] | None] = {}
    bucket_keys = sorted(buckets)
    for key in bucket_keys:
        candidates: list[FileRecord] = []
        for nearby in range(int(key - duration_tolerance), int(key + duration_tolerance) + 1):
            candidates.extend(buckets.get(nearby, []))
        for left_index, right_index in _candidate_pairs(candidates, max_candidates_per_bucket, threshold):
            left = candidates[left_index]
            right = candidates[right_index]
            pair = tuple(sorted((left.path_key, right.path_key)))
            if pair in seen or left.full_hash and left.full_hash == right.full_hash:
                continue
            seen.add(pair)
            if left.duration is None or right.duration is None or left.fingerprint is None or right.fingerprint is None:
                continue
            delta = abs(left.duration - right.duration)
            if delta > duration_tolerance:
                continue
            similarity = hamming_similarity(left.fingerprint, right.fingerprint)
            if similarity < threshold and delta > 0.05:
                similarity = max(
                    similarity,
                    _duration_aligned_similarity(left, right, ffmpeg, aligned_fingerprints),
                )
            if threshold <= similarity:
                matches.append(VideoMatch(pair[0], pair[1], round(similarity, 2), round(delta, 3)))
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
