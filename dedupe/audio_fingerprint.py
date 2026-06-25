from __future__ import annotations

import logging
import struct
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import Cache
from .models import AudioMatch, FileRecord
from .progress import tqdm
from .video_fingerprint import get_duration

LOGGER = logging.getLogger(__name__)
BITS_PER_VALUE = 32
MAX_ALIGNMENT_SHIFT = 8


def check_chromaprint(ffmpeg: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "muxer=chromaprint"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        return result.returncode == 0 and b"Muxer chromaprint" in result.stdout
    except subprocess.SubprocessError:
        return False


def fingerprint_audio(record: FileRecord, ffmpeg: str) -> list[int] | None:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(record.path),
        "-map",
        "0:a:0",
        "-f",
        "chromaprint",
        "-fp_format",
        "raw",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=180)
        if not result.stdout or len(result.stdout) % 4:
            return None
        return list(struct.unpack(f"<{len(result.stdout) // 4}I", result.stdout))
    except subprocess.SubprocessError as exc:
        LOGGER.warning("Cannot fingerprint audio %s: %s", record.path, exc)
        return None


def audio_similarity(left: list[int], right: list[int], max_shift: int = MAX_ALIGNMENT_SHIFT) -> float:
    if not left or not right:
        return 0.0
    shorter_length = min(len(left), len(right))
    minimum_overlap = max(1, int(shorter_length * 0.9))
    best = 0.0
    for shift in range(-max_shift, max_shift + 1):
        left_start = max(0, shift)
        right_start = max(0, -shift)
        overlap = min(len(left) - left_start, len(right) - right_start)
        if overlap < minimum_overlap:
            continue
        distance = sum(
            (left[left_start + index] ^ right[right_start + index]).bit_count()
            for index in range(overlap)
        )
        similarity = 100.0 * (1.0 - distance / (overlap * BITS_PER_VALUE))
        best = max(best, similarity)
    return max(0.0, best)


def _ensure_duration(record: FileRecord, ffprobe: str) -> tuple[str, float | None]:
    if record.duration is not None:
        return record.path_key, record.duration
    return record.path_key, get_duration(record.path, ffprobe)


def _ensure_fingerprint(record: FileRecord, ffmpeg: str) -> tuple[str, list[int] | None]:
    if record.audio_fingerprint is not None:
        return record.path_key, record.audio_fingerprint
    return record.path_key, fingerprint_audio(record, ffmpeg)


def find_audio_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    ffmpeg: str,
    ffprobe: str,
    workers: int = 2,
    duration_tolerance: float = 3.0,
) -> list[AudioMatch]:
    missing_duration = [record for record in records if record.duration is None]
    if missing_duration:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_ensure_duration, record, ffprobe): record for record in missing_duration}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Audio durations", unit="file"):
                record = futures[future]
                _, duration = future.result()
                if duration is not None:
                    record.duration = duration
                    cache.upsert_file(record)
        cache.conn.commit()

    missing_fingerprint = [record for record in records if record.audio_fingerprint is None]
    if missing_fingerprint:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_ensure_fingerprint, record, ffmpeg): record
                for record in missing_fingerprint
            }
            for future in tqdm(as_completed(futures), total=len(futures), desc="Audio fingerprints", unit="file"):
                record = futures[future]
                _, fingerprint = future.result()
                if fingerprint:
                    record.audio_fingerprint = fingerprint
                    cache.upsert_file(record)
        cache.conn.commit()

    buckets: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.duration is not None and record.audio_fingerprint:
            buckets[int(round(record.duration))].append(record)

    matches: list[AudioMatch] = []
    seen: set[tuple[str, str]] = set()
    for key in sorted(buckets):
        candidates: list[FileRecord] = []
        for nearby in range(int(key - duration_tolerance), int(key + duration_tolerance) + 1):
            candidates.extend(buckets.get(nearby, []))
        for left_index, left in enumerate(candidates):
            for right in candidates[left_index + 1 :]:
                pair = tuple(sorted((left.path_key, right.path_key)))
                if pair in seen or left.full_hash and left.full_hash == right.full_hash:
                    continue
                seen.add(pair)
                if (
                    left.duration is None
                    or right.duration is None
                    or left.audio_fingerprint is None
                    or right.audio_fingerprint is None
                ):
                    continue
                duration_delta = abs(left.duration - right.duration)
                if duration_delta > duration_tolerance:
                    continue
                similarity = audio_similarity(left.audio_fingerprint, right.audio_fingerprint)
                if similarity >= threshold:
                    matches.append(
                        AudioMatch(
                            pair[0],
                            pair[1],
                            round(similarity, 2),
                            round(duration_delta, 3),
                        )
                    )
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
