from __future__ import annotations

import logging
import struct
import subprocess
from collections import defaultdict
from concurrent.futures import as_completed

from . import process
from .cache import Cache
from .models import AudioMatch, FileRecord
from .progress import cancellable_executor, check_cancelled, tqdm
from .video_fingerprint import calibrate_similarity, get_duration

LOGGER = logging.getLogger(__name__)
BITS_PER_VALUE = 32
# ffmpeg's chromaprint muxer emits one 32-bit item per ~0.1266 s of audio.
ITEM_SECONDS = 0.1266
# Alignment used to allow 8 items - almost exactly one second (A-14). Two
# releases of the same content routinely differ by more than that at the head
# (a longer distributor logo, a different lead-in), and past one second the
# score collapses: measured on real library audio, a 2-second lead drops a
# byte-identical recording from 98.6 to 10.9. 30 seconds is affordable
# because the search is coarse-to-fine rather than exhaustive.
MAX_ALIGNMENT_SHIFT = int(round(30.0 / ITEM_SECONDS))
ALIGNMENT_PROBES = 64
# Extra content at the *end* of one release does not need a shift, only a
# smaller overlap, so this is deliberately looser than the shift range.
MINIMUM_OVERLAP_RATIO = 0.75
DEFAULT_DURATION_TOLERANCE = 120.0
# A fingerprint that repeats the same item carries no information, and two
# such fingerprints score high against each other whatever their content:
# two different pure tones score 75.00 (A-14). This is the audio counterpart
# of A-06's flat-image problem. Real library audio has 99.1-99.6% distinct
# items; silence and a constant tone have 0.7%.
MINIMUM_DISTINCT_RATIO = 0.5


def check_chromaprint(ffmpeg: str) -> bool:
    try:
        result = process.run(
            [ffmpeg, "-hide_banner", "-h", "muxer=chromaprint"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        return result.returncode == 0 and b"Muxer chromaprint" in result.stdout
    except (OSError, subprocess.SubprocessError):
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
        result = process.run(command, capture_output=True, check=True, timeout=180)
        if not result.stdout or len(result.stdout) % 4:
            return None
        return list(struct.unpack(f"<{len(result.stdout) // 4}I", result.stdout))
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("Cannot fingerprint audio %s: %s", record.path, exc)
        return None


def _overlap_bounds(left_length: int, right_length: int, shift: int, minimum_overlap: int) -> tuple[int, int, int]:
    left_start = max(0, shift)
    right_start = max(0, -shift)
    overlap = min(left_length - left_start, right_length - right_start)
    if overlap < minimum_overlap:
        return 0, 0, 0
    return left_start, right_start, overlap


def _raw_similarity_at(left: list[int], right: list[int], shift: int, minimum_overlap: int) -> float:
    left_start, right_start, overlap = _overlap_bounds(len(left), len(right), shift, minimum_overlap)
    if not overlap:
        return -1.0
    distance = sum((left[left_start + index] ^ right[right_start + index]).bit_count() for index in range(overlap))
    return 100.0 * (1.0 - distance / (overlap * BITS_PER_VALUE))


def _probe_similarity_at(left: list[int], right: list[int], shift: int, minimum_overlap: int) -> float:
    """Similarity at one shift over a bounded, evenly spaced sample.

    The exhaustive form is O(shift range x overlap), which at 30 seconds of
    shift and a 15-minute fingerprint measured 240 ms per pair - 91 minutes
    for a library-sized pass, the same shape as A-01's blowup. Probing keeps
    the search flat while the final score stays exact.
    """
    left_start, right_start, overlap = _overlap_bounds(len(left), len(right), shift, minimum_overlap)
    if not overlap:
        return -1.0
    step = max(1, overlap // ALIGNMENT_PROBES)
    distance = 0
    count = 0
    for index in range(0, overlap, step):
        distance += (left[left_start + index] ^ right[right_start + index]).bit_count()
        count += 1
    return 100.0 * (1.0 - distance / (count * BITS_PER_VALUE))


def _is_degenerate(fingerprint: list[int]) -> bool:
    """True when a fingerprint carries too little information to compare."""
    return len(set(fingerprint)) / len(fingerprint) < MINIMUM_DISTINCT_RATIO


def audio_similarity(left: list[int], right: list[int], max_shift: int = MAX_ALIGNMENT_SHIFT) -> float:
    if not left or not right:
        return 0.0
    if _is_degenerate(left) or _is_degenerate(right):
        return 0.0
    minimum_overlap = max(1, int(min(len(left), len(right)) * MINIMUM_OVERLAP_RATIO))

    # Every shift is probed, not a strided subset: the peak can be one item
    # wide, so a coarse pass would have to assume the score varies smoothly
    # around the true offset. Probing keeps the whole range affordable without
    # that assumption - it is the *overlap* that is subsampled, not the search.
    best_shift = 0
    best_probe = -1.0
    for shift in range(-max_shift, max_shift + 1):
        probe = _probe_similarity_at(left, right, shift, minimum_overlap)
        if probe > best_probe:
            best_probe = probe
            best_shift = shift
    if best_probe < 0.0:
        return 0.0

    # Purely Hamming-based (unlike image's structure+color blend), so the
    # whole result is calibrated in one place, unconditionally (F-24).
    return calibrate_similarity(max(0.0, _raw_similarity_at(left, right, best_shift, minimum_overlap)))


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
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> list[AudioMatch]:
    missing_duration = [record for record in records if record.duration is None]
    if missing_duration:
        with cancellable_executor(workers) as executor:
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
        with cancellable_executor(workers) as executor:
            futures = {executor.submit(_ensure_fingerprint, record, ffmpeg): record for record in missing_fingerprint}
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
        check_cancelled()
        candidates: list[FileRecord] = []
        for nearby in range(int(key - duration_tolerance), int(key + duration_tolerance) + 1):
            candidates.extend(buckets.get(nearby, []))
        for left_index, left in enumerate(candidates):
            check_cancelled()
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
