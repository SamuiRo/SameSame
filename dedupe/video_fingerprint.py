from __future__ import annotations

import io
import json
import logging
import math
import shutil
import subprocess
from concurrent.futures import as_completed
from functools import lru_cache
from pathlib import Path

from . import process
from .cache import Cache
from .models import GRID_FINGERPRINT_VERSION, VIDEO_FINGERPRINT_VERSION, FileRecord, VideoMatch
from .progress import cancellable_executor, check_cancelled, tqdm

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

PIL_AVAILABLE = Image is not None

LOGGER = logging.getLogger(__name__)
SAMPLE_COUNT = 15
SAMPLE_POINTS = tuple((index + 1) / (SAMPLE_COUNT + 1) for index in range(SAMPLE_COUNT))
MINIMUM_FRAME_COVERAGE = 0.5
FRAME_RETRY_NUDGE = 0.5
DEFAULT_MAX_DURATION_RATIO = 1.5
DEFAULT_MAX_DURATION_DELTA = 15 * 60.0
ALIGNMENT_MAX_SHIFT = 3 * 60.0
# Absolute-time alignment grid (A-01): perceptual hashes taken at fixed
# offsets from the start of the file rather than at fractions of its duration,
# so two files of different lengths produce directly comparable sequences and
# alignment becomes an integer shift over cached hashes instead of a per-pair
# re-decode of one file.
# Cadence is chosen per file from a power-of-two ladder so that a 4-second
# clip is still sampled usefully while a 3-hour file stays bounded. Powers of
# two specifically: two files may land on different rungs, and a coarser grid
# is then exactly a stride of the finer one, so their samples still describe
# the same absolute moments.
GRID_BASE_CADENCE = 1.0
GRID_MAX_CADENCE = 32.0
GRID_TARGET_SAMPLES = 96
GRID_MIN_OVERLAP = 2
GRID_MIN_OVERLAP_RATIO = 0.5
GRID_MAX_CONSECUTIVE_FAILURES = 3
GRID_MISSING_SAMPLE = -1


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
        result = process.run(command, capture_output=True, text=True, check=True, timeout=30)
        value = json.loads(result.stdout).get("format", {}).get("duration")
        return float(value) if value is not None else None
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
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
        result = process.run(command, capture_output=True, check=True, timeout=60)
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
    row_coefficients = [[sum(row[x] * cosines[v][x] for x in range(size)) for v in range(hash_size)] for row in pixels]
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


def fingerprint_video(path: Path, duration: float, ffmpeg: str) -> list[int] | None:
    hashes: list[int] = []
    for fraction in SAMPLE_POINTS:
        timestamp = max(0.0, duration * fraction)
        frame = _extract_frame(path, timestamp, ffmpeg)
        if frame is None:
            # One retry with a nudged timestamp: the original position may have
            # landed on a corrupt frame or a transient decode failure.
            frame = _extract_frame(path, timestamp + FRAME_RETRY_NUDGE, ffmpeg)
        if frame is None:
            continue
        hashes.append(phash(frame))
    if len(hashes) < math.ceil(SAMPLE_COUNT * MINIMUM_FRAME_COVERAGE):
        return None
    return hashes


def grid_cadence(duration: float) -> float:
    """Seconds between alignment samples for a file of this duration.

    A pure function of duration, so it never has to be stored: any caller
    holding the (cached) duration can recompute how a cached grid was laid
    out. Doubling until the sample count is bounded keeps a 3-hour file from
    costing hundreds of seeks while a 4-second clip still gets sampled.
    """
    cadence = GRID_BASE_CADENCE
    while cadence < GRID_MAX_CADENCE and duration / cadence > GRID_TARGET_SAMPLES:
        cadence *= 2.0
    return cadence


def grid_timestamps(duration: float, cadence: float) -> list[float]:
    """Absolute sample positions: whole multiples of `cadence`.

    Multiples, not midpoints, so that a grid sampled at `2 * cadence` is
    exactly every second entry of one sampled at `cadence` - which is what
    lets two files on different rungs of the ladder still be compared.
    """
    if duration <= 0.0 or cadence <= 0.0:
        return []
    timestamps: list[float] = []
    timestamp = cadence
    while timestamp < duration:
        timestamps.append(timestamp)
        timestamp += cadence
    return timestamps


def fingerprint_video_grid(path: Path, duration: float, ffmpeg: str) -> list[int] | None:
    """Perceptual hashes on an absolute-time grid, for alignment (A-01).

    Computed once per file and cached. The proportional `fingerprint_video`
    above stays the primary cheap score; this one exists so that alignment -
    which used to re-decode the longer file once per candidate offset per
    *pair*, at ~11 s a pair - becomes an integer shift over cached hashes.

    Failed frames are kept as `GRID_MISSING_SAMPLE` placeholders rather than
    dropped, because dropping one would shift every later sample off the
    absolute grid and silently misalign the whole sequence.
    """
    timestamps = grid_timestamps(duration, grid_cadence(duration))
    if not timestamps:
        return None
    # A grid can be hundreds of samples, each costing two ffmpeg spawns when a
    # frame fails. Give up as soon as enough samples have failed that the
    # coverage floor is already unreachable, so an unreadable file (or a
    # missing ffmpeg) costs a handful of spawns instead of hundreds.
    allowed_failures = len(timestamps) - math.ceil(len(timestamps) * MINIMUM_FRAME_COVERAGE)
    failures = 0
    consecutive_failures = 0
    hashes: list[int] = []
    for timestamp in timestamps:
        frame = _extract_frame(path, timestamp, ffmpeg)
        if frame is None:
            frame = _extract_frame(path, timestamp + FRAME_RETRY_NUDGE, ffmpeg)
        if frame is None:
            failures += 1
            consecutive_failures += 1
            if failures > allowed_failures or consecutive_failures > GRID_MAX_CONSECUTIVE_FAILURES:
                LOGGER.warning("Cannot build an alignment grid for %s: too many unreadable frames", path)
                return None
        else:
            consecutive_failures = 0
        hashes.append(phash(frame) if frame is not None else GRID_MISSING_SAMPLE)
    return hashes


def _grid_overlap(left: list[int], right: list[int], shift: int) -> list[tuple[int, int]]:
    """Sample pairs that overlap when `right` is shifted `shift` slots later."""
    start = max(0, shift)
    stop = min(len(left), len(right) + shift)
    return [
        (left[index], right[index - shift])
        for index in range(start, stop)
        if left[index] != GRID_MISSING_SAMPLE and right[index - shift] != GRID_MISSING_SAMPLE
    ]


def _to_common_cadence(
    left: list[int],
    left_duration: float,
    right: list[int],
    right_duration: float,
) -> tuple[list[int], list[int], float]:
    """Put two grids on the same cadence by striding the finer one.

    Cadences come from a power-of-two ladder, so the coarser grid's samples
    are always a subset of the finer grid's - taking every Nth entry lines
    them back up on identical absolute timestamps.
    """
    left_cadence = grid_cadence(left_duration)
    right_cadence = grid_cadence(right_duration)
    cadence = max(left_cadence, right_cadence)
    if left_cadence < cadence:
        stride = int(round(cadence / left_cadence))
        left = left[stride - 1 :: stride]
    if right_cadence < cadence:
        stride = int(round(cadence / right_cadence))
        right = right[stride - 1 :: stride]
    return left, right, cadence


def grid_aligned_similarity(
    left: list[int] | None,
    right: list[int] | None,
    left_duration: float | None,
    right_duration: float | None,
    max_shift: float = ALIGNMENT_MAX_SHIFT,
) -> float:
    """Raw-scale similarity after aligning two absolute-time grids.

    Two stages, both cheap. First pick the best integer shift by mean Hamming
    similarity - one pass per candidate shift over cached integers. Then score
    that alignment with `ordered_sequence_similarity`, the same DP (and the
    same coverage penalty, and therefore the same scale) the unaligned path
    uses, over an evenly spaced subsample of the aligned overlap. Scoring the
    winner with the DP rather than the mean matters: a plain mean loses real
    duplicates whose frames drift slightly against the grid.
    """
    if not left or not right or left_duration is None or right_duration is None:
        return 0.0
    left, right, cadence = _to_common_cadence(left, left_duration, right, right_duration)
    if not left or not right:
        return 0.0
    max_steps = max(0, int(max_shift / cadence)) if cadence > 0 else 0
    minimum_overlap = max(GRID_MIN_OVERLAP, math.ceil(GRID_MIN_OVERLAP_RATIO * min(len(left), len(right))))
    best_pairs: list[tuple[int, int]] = []
    best_mean = -1.0
    for shift in range(-max_steps, max_steps + 1):
        overlap = _grid_overlap(left, right, shift)
        if len(overlap) < minimum_overlap:
            continue
        mean = sum(hamming_similarity([a], [b]) for a, b in overlap) / len(overlap)
        if mean > best_mean:
            best_mean = mean
            best_pairs = overlap
    if not best_pairs:
        return 0.0
    if len(best_pairs) > SAMPLE_COUNT:
        step = len(best_pairs) / SAMPLE_COUNT
        best_pairs = [best_pairs[int(index * step)] for index in range(SAMPLE_COUNT)]
    return ordered_sequence_similarity([a for a, _ in best_pairs], [b for _, b in best_pairs])


def hamming_similarity(left: list[int], right: list[int], bits_per_hash: int = 64) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    total_bits = len(left) * bits_per_hash
    distance = sum((a ^ b).bit_count() for a, b in zip(left, right))
    return max(0.0, 100.0 * (1.0 - distance / total_bits))


# Two unrelated Hamming-based fingerprints of any bit width differ in ~half
# their bits, so hamming_similarity converges to ~50% for content with
# nothing in common - measured at mean 52.3%, range 49.6-55.5% over 300
# random-pair simulations. A raw threshold therefore does not mean what it
# appears to: "85%" sounds like "very similar" but is actually most of the
# way from "identical" (100%) to "coin flip" (50%), and a real anime corpus
# confirmed genuine duplicates commonly score in the low-to-mid 50s pre- and
# even post-alignment (F-24).
NOISE_FLOOR = 50.0


def calibrate_similarity(raw: float) -> float:
    """Rescale a raw Hamming-based similarity to a 0-100 confidence where 0
    means "indistinguishable from unrelated content" and 100 means
    identical, instead of 0 meaning "bit-for-bit opposite" (F-24).

    Deliberately NOT applied inside `ordered_sequence_similarity` or
    `video_similarity` themselves: `find_video_matches`'s
    `ALIGNMENT_RECHECK_FLOOR` circuit-breaker needs to distinguish a
    genuinely degenerate raw score (e.g. 0.0, common in mocked tests) from a
    merely muddy real one (raw 51-56 is common for real confirmed
    duplicates) - both collapse to the same clamped 0 after calibration, so
    that comparison must stay on the raw scale. Calibration is applied once,
    at the outer boundary (`find_video_matches`, `find_image_matches`,
    `audio_similarity`, and the benchmark tool), where the result is about to
    be shown to a user or compared against a user-supplied (calibrated)
    threshold.
    """
    return max(0.0, min(100.0, (raw - NOISE_FLOOR) / (100.0 - NOISE_FLOOR) * 100.0))


def decalibrate_similarity(calibrated: float) -> float:
    """Inverse of `calibrate_similarity`: convert a calibrated threshold back
    to the raw scale so it can be compared against a raw similarity value."""
    return calibrated / 100.0 * (100.0 - NOISE_FLOOR) + NOISE_FLOOR


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
    dp = [[[negative_infinity] * (shorter_length + 1) for _ in range(right_length + 1)] for _ in range(left_length + 1)]
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


def _ensure_grid_fingerprint(record: FileRecord, ffmpeg: str) -> tuple[str, list[int] | None]:
    if record.grid_fingerprint is not None:
        return record.path_key, record.grid_fingerprint
    if record.duration is None:
        return record.path_key, None
    return record.path_key, fingerprint_video_grid(record.path, record.duration, ffmpeg)


def video_similarity(left: FileRecord, right: FileRecord) -> float:
    """Raw-scale similarity for a pair, using the cached alignment grid when
    both records have one.

    Takes no `ffmpeg` argument any more: alignment no longer decodes anything
    at pair time (A-01). Callers that want alignment must populate
    `grid_fingerprint` first - `find_video_matches` does this in a parallel
    stage, and the benchmark tool does it per pair.
    """
    if left.fingerprint is None or right.fingerprint is None:
        return 0.0
    similarity = ordered_sequence_similarity(left.fingerprint, right.fingerprint)
    aligned = grid_aligned_similarity(
        left.grid_fingerprint,
        right.grid_fingerprint,
        left.duration,
        right.duration,
    )
    return max(similarity, aligned)


def find_video_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    ffmpeg: str,
    ffprobe: str,
    workers: int = 2,
    max_duration_ratio: float = DEFAULT_MAX_DURATION_RATIO,
    max_duration_delta: float = DEFAULT_MAX_DURATION_DELTA,
) -> list[VideoMatch]:
    # `threshold` arrives on the calibrated scale (F-24) - everything below
    # this line compares against the raw internal scale instead, so it is
    # converted once, up front. See `calibrate_similarity` for why the
    # comparisons themselves stay raw rather than calibrating every score.
    raw_threshold = decalibrate_similarity(threshold)
    missing_duration = [record for record in records if record.duration is None]
    if missing_duration:
        with cancellable_executor(workers) as executor:
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
        with cancellable_executor(workers) as executor:
            futures = {executor.submit(_ensure_fingerprint, record, ffmpeg): record for record in missing_fingerprint}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Video fingerprints", unit="file"):
                record = futures[future]
                _, fingerprint = future.result()
                if fingerprint:
                    record.fingerprint = fingerprint
                    record.fingerprint_version = VIDEO_FINGERPRINT_VERSION
                    cache.upsert_file(record)
        cache.conn.commit()
        excluded = sum(1 for record in missing_fingerprint if not record.fingerprint)
        if excluded:
            LOGGER.warning(
                "%d of %d videos could not be fingerprinted (too few readable frames) and were excluded from video matching",
                excluded,
                len(missing_fingerprint),
            )

    # Alignment grids, computed once per file and cached forever (A-01).
    # This is the whole reason alignment is affordable: it moves the ffmpeg
    # cost from "once per candidate offset per pair, every scan" to "once per
    # file, ever".
    missing_grid = [
        record
        for record in records
        if record.duration is not None and record.fingerprint and not record.grid_fingerprint
    ]
    if missing_grid:
        with cancellable_executor(workers) as executor:
            futures = {executor.submit(_ensure_grid_fingerprint, record, ffmpeg): record for record in missing_grid}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Video alignment grids", unit="file"):
                record = futures[future]
                _, grid = future.result()
                if grid:
                    record.grid_fingerprint = grid
                    record.grid_fingerprint_version = GRID_FINGERPRINT_VERSION
                    cache.upsert_file(record)
        cache.conn.commit()

    # Sort by duration and sweep forward from each record: ratio and delta to
    # a later (equal-or-longer) candidate only ever grow as duration
    # increases, so compatibility can only go True -> False along this
    # sweep, never back. That makes the inner loop's first incompatible
    # `right` a safe place to stop, and every unordered pair is generated
    # exactly once - no bucket windows, no re-visiting the same pair from
    # multiple buckets (F-01).
    candidates = sorted(
        (record for record in records if record.duration is not None and record.fingerprint),
        key=lambda record: record.duration,  # type: ignore[arg-type,return-value]
    )

    matches: list[VideoMatch] = []
    for left_index, left in enumerate(tqdm(candidates, desc="Video matching", unit="file")):
        for right in candidates[left_index + 1 :]:
            # Checked per pair, not per outer record: an outer iteration can
            # span hundreds of pairs, and a cancelled scan must not have to
            # wait for all of them (A-05).
            check_cancelled()
            if not video_durations_compatible(
                left.duration,  # type: ignore[arg-type]
                right.duration,  # type: ignore[arg-type]
                max_ratio=max_duration_ratio,
                max_delta=max_duration_delta,
            ):
                break
            if left.full_hash and left.full_hash == right.full_hash:
                continue
            pair = tuple(sorted((left.path_key, right.path_key)))
            delta = abs(left.duration - right.duration)  # type: ignore[operator]
            # Alignment now runs for every compatible pair, with no score
            # floor and no duration-delta gate: on cached grids it costs about
            # a millisecond, so the circuit-breakers those gates provided are
            # no longer worth the recall they cost (A-01). It is also the
            # matcher's strongest discriminator - on the real library, true
            # duplicates gain up to +76 points from it while confirmed
            # unrelated pairs gain exactly zero (A-02).
            similarity = video_similarity(left, right)
            if raw_threshold <= similarity:
                calibrated = calibrate_similarity(similarity)
                matches.append(VideoMatch(pair[0], pair[1], round(calibrated, 2), round(delta, 3)))
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
