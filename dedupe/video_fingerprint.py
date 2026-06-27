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
from .models import VIDEO_FINGERPRINT_VERSION, FileRecord, VideoMatch
from .progress import check_cancelled, tqdm

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

PIL_AVAILABLE = Image is not None

LOGGER = logging.getLogger(__name__)
SAMPLE_COUNT = 15
SAMPLE_POINTS = tuple((index + 1) / (SAMPLE_COUNT + 1) for index in range(SAMPLE_COUNT))
DEFAULT_MAX_DURATION_RATIO = 1.5
DEFAULT_MAX_DURATION_DELTA = 15 * 60.0
ALIGNMENT_RECHECK_FLOOR = 65.0
ALIGNMENT_RECHECK_MAX_DELTA = 3 * 60.0


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


def ordered_sequence_similarity(
    left: list[int],
    right: list[int],
    minimum_coverage: float = 0.6,
) -> float:
    if not left or not right:
        return 0.0
    left_length = len(left)
    right_length = len(right)
    shorter_length = min(left_length, right_length)
    minimum_matches = max(1, math.ceil(shorter_length * minimum_coverage))
    negative_infinity = float("-inf")
    dp = [
        [[negative_infinity] * (shorter_length + 1) for _ in range(right_length + 1)]
        for _ in range(left_length + 1)
    ]
    for left_index in range(left_length + 1):
        for right_index in range(right_length + 1):
            dp[left_index][right_index][0] = 0.0
    for left_index in range(1, left_length + 1):
        for right_index in range(1, right_length + 1):
            frame_similarity = hamming_similarity(
                [left[left_index - 1]],
                [right[right_index - 1]],
            )
            for matches in range(1, min(left_index, right_index, shorter_length) + 1):
                dp[left_index][right_index][matches] = max(
                    dp[left_index - 1][right_index][matches],
                    dp[left_index][right_index - 1][matches],
                    dp[left_index - 1][right_index - 1][matches - 1] + frame_similarity,
                )
    best = 0.0
    for matches in range(minimum_matches, shorter_length + 1):
        total = dp[left_length][right_length][matches]
        if total == negative_infinity:
            continue
        coverage = matches / shorter_length
        coverage_penalty = (1.0 - coverage) * 20.0
        best = max(best, total / matches - coverage_penalty)
    return max(0.0, min(100.0, best))


def video_durations_compatible(
    left_duration: float,
    right_duration: float,
    max_ratio: float = DEFAULT_MAX_DURATION_RATIO,
    max_delta: float = DEFAULT_MAX_DURATION_DELTA,
) -> bool:
    shorter = min(left_duration, right_duration)
    longer = max(left_duration, right_duration)
    if shorter <= 0:
        return False
    return longer / shorter <= max_ratio and longer - shorter <= max_delta


def _candidate_pairs(
    candidates: list[FileRecord],
    max_candidates_per_bucket: int,
    threshold: float = 90.0,
) -> set[tuple[int, int]]:
    del max_candidates_per_bucket, threshold
    pairs: set[tuple[int, int]] = set()
    for left in range(len(candidates)):
        check_cancelled()
        pairs.update((left, right) for right in range(left + 1, len(candidates)))
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
        return ordered_sequence_similarity(left.fingerprint, right.fingerprint)

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
            best = max(best, ordered_sequence_similarity(shorter.fingerprint, aligned))
    return best


def video_similarity(
    left: FileRecord,
    right: FileRecord,
    ffmpeg: str,
    aligned_cache: dict[tuple[str, float, float], list[int] | None] | None = None,
) -> float:
    if left.fingerprint is None or right.fingerprint is None:
        return 0.0
    similarity = ordered_sequence_similarity(left.fingerprint, right.fingerprint)
    if (
        left.duration is not None
        and right.duration is not None
        and abs(left.duration - right.duration) > 0.05
    ):
        similarity = max(
            similarity,
            _duration_aligned_similarity(left, right, ffmpeg, aligned_cache if aligned_cache is not None else {}),
        )
    return similarity


def find_video_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    ffmpeg: str,
    ffprobe: str,
    workers: int = 2,
    duration_tolerance: float = 2.0,
    max_duration_ratio: float = DEFAULT_MAX_DURATION_RATIO,
    max_duration_delta: float = DEFAULT_MAX_DURATION_DELTA,
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
                    record.fingerprint_version = VIDEO_FINGERPRINT_VERSION
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
        check_cancelled()
        candidates: list[FileRecord] = []
        minimum_duration = max(0, math.floor(max(key / max_duration_ratio, key - max_duration_delta)))
        maximum_duration = math.ceil(min(key * max_duration_ratio, key + max_duration_delta))
        for nearby in range(minimum_duration, maximum_duration + 1):
            candidates.extend(buckets.get(nearby, []))
        for left_index, right_index in _candidate_pairs(candidates, max_candidates_per_bucket, threshold):
            check_cancelled()
            left = candidates[left_index]
            right = candidates[right_index]
            pair = tuple(sorted((left.path_key, right.path_key)))
            if pair in seen or left.full_hash and left.full_hash == right.full_hash:
                continue
            seen.add(pair)
            if left.duration is None or right.duration is None or left.fingerprint is None or right.fingerprint is None:
                continue
            delta = abs(left.duration - right.duration)
            if not video_durations_compatible(
                left.duration,
                right.duration,
                max_ratio=max_duration_ratio,
                max_delta=max_duration_delta,
            ):
                continue
            similarity = ordered_sequence_similarity(left.fingerprint, right.fingerprint)
            if (
                ALIGNMENT_RECHECK_FLOOR <= similarity < threshold
                and 0.05 < delta <= ALIGNMENT_RECHECK_MAX_DELTA
            ):
                similarity = video_similarity(left, right, ffmpeg, aligned_fingerprints)
            if threshold <= similarity:
                matches.append(VideoMatch(pair[0], pair[1], round(similarity, 2), round(delta, 3)))
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
