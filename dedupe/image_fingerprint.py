from __future__ import annotations

import logging
from concurrent.futures import as_completed

import numpy as np

from .cache import Cache
from .models import IMAGE_FINGERPRINT_VERSION, FileRecord, ImageMatch
from .progress import cancellable_executor, check_cancelled, tqdm
from .video_fingerprint import NOISE_FLOOR, calibrate_similarity, hamming_similarity, phash

try:
    from PIL import Image, ImageChops, ImageOps
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

PIL_AVAILABLE = Image is not None and ImageOps is not None and ImageChops is not None
LOGGER = logging.getLogger(__name__)
# Average colour is a veto, not an addend (A-06). Two images whose average
# colours are this far apart are not the same image, whatever their structure
# hashes say - which matters most for pictures that have no structure to
# compare at all, where a perceptual hash of one flat colour looks much like a
# perceptual hash of another. Derived from real evidence: 720 transformed
# duplicates (including brightness and saturation grading) never fall below
# 83.27 here, while solid-colour pairs reach at most 66.67.
COLOR_VETO_SIMILARITY = 70.0
# The dihedral group: four rotations, each optionally mirrored (A-15). A
# perceptual hash of a rotated picture has nothing in common with the
# original - measured on real images, every 90-degree rotation and every
# mirror scored exactly 0 - so the orientations are enumerated at
# fingerprint time and the best one wins at comparison time.
ORIENTATION_COUNT = 8
FINGERPRINT_LENGTH = ORIENTATION_COUNT + 3
# A uniform border is trimmed before hashing, so letterboxing and page
# margins stop changing the descriptor. Pixels within this distance of the
# border colour count as border.
BORDER_TOLERANCE = 24
# Refuse to trim away most of the picture: past this point it is a crop, not
# a border, and cropping is not what trimming is for.
MINIMUM_TRIMMED_AREA = 0.4
# phash()'s own hash_size(8) * highfreq_factor(4) defaults. Shrinking to this
# size once, before generating the 8 dihedral views, means phash()'s internal
# resize becomes a same-size no-op instead of downsizing the full trimmed
# image 8 times over (A-35).
HASH_GRID_SIZE = 32


def trim_border(image: Image.Image) -> Image.Image:
    """Drop a uniform border so letterboxing and margins stop mattering.

    The reference colour is the median of the four corners rather than one
    corner: rotating or mirroring permutes the corners, so any single-corner
    choice would make trimming orientation-dependent and destroy the exact
    match the dihedral views exist to provide.
    """
    width, height = image.size
    # Locate the border on a contrast-normalised copy, then crop the original.
    # The tolerance below is an absolute number of levels, so on the raw image
    # a mere 20% darkening shrinks every difference by 20% and moves the
    # boundary: measured on two real frames, that turned a pair scoring 100
    # into 43.75 and 50.00. Normalising first makes the decision invariant to
    # brightness and contrast changes, which is exactly the class of edit that
    # was breaking it.
    probe = ImageOps.autocontrast(image)
    corners = [
        probe.getpixel((0, 0)),
        probe.getpixel((width - 1, 0)),
        probe.getpixel((0, height - 1)),
        probe.getpixel((width - 1, height - 1)),
    ]
    reference = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*corners))
    difference = ImageChops.difference(probe, Image.new(probe.mode, probe.size, reference)).convert("L")
    box = difference.point(lambda value: 255 if value > BORDER_TOLERANCE else 0).getbbox()
    if box is None:
        return image
    if (box[2] - box[0]) * (box[3] - box[1]) < width * height * MINIMUM_TRIMMED_AREA:
        return image
    return image.crop(box)


def _orientations(image: Image.Image) -> list[Image.Image]:
    views = []
    for candidate in (image, image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)):
        for quarter_turns in range(4):
            views.append(candidate.rotate(90 * quarter_turns, expand=True))
    return views


# Values accepted for the `orientations` parameter below and the
# `--image-orientations` CLI/config setting. "full" is the default and only
# mode that detects rotated/mirrored duplicates (A-15); "canonical" trades
# that away for speed - profiled at ~22% of per-image fingerprinting time and
# a larger share of the comparison stage, since both scale with orientation
# count.
IMAGE_ORIENTATION_MODES = ("full", "canonical")


def fingerprint_image(record: FileRecord, orientations: str = "full") -> list[int] | None:
    if Image is None or ImageOps is None:
        return None
    try:
        with Image.open(record.path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                rgb = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                rgb = image.convert("RGB")
            trimmed = trim_border(rgb)
            # Averaged over the *trimmed* image, so that a letterboxed copy
            # and its original agree on colour too - otherwise the veto would
            # reject the very pairs trimming exists to reunite.
            average = trimmed.resize((1, 1), Image.Resampling.LANCZOS).getpixel((0, 0))
            # Shrink once here rather than let each of the 8 orientations
            # resize the full-resolution image inside phash() (A-35):
            # profiled at 45% of total fingerprinting time on a real 54k-image
            # library (PIL resize 30%, full-resolution rotate/transpose
            # 14.5%), because seven of those eight resizes throw away pixels
            # a rotation was just spent producing. Rotating/mirroring the
            # already-tiny grid instead measured 9x faster on 300 real
            # images. See IMAGE_FINGERPRINT_VERSION for the bit-identity
            # measurement this trades against.
            grid = trimmed.resize((HASH_GRID_SIZE, HASH_GRID_SIZE), Image.Resampling.LANCZOS).convert("L")
            if orientations == "canonical":
                # Skip generating and hashing the other 7 dihedral views
                # entirely - not just skip comparing them - and fill every
                # slot with the one computed hash. `image_similarity`/
                # `_batch_similarity` only ever read `right_hashes[:, 0]`, so
                # this is exactly equivalent to a single-orientation compare
                # on both sides, at the storage format every other code path
                # already expects.
                canonical_hash = phash(grid)
                hashes = [canonical_hash] * ORIENTATION_COUNT
            else:
                hashes = [phash(view) for view in _orientations(grid)]
            return [*hashes, int(average[0]), int(average[1]), int(average[2])]
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        LOGGER.warning("Cannot fingerprint image %s: %s", record.path, exc)
        return None


def image_similarity(left: list[int], right: list[int]) -> float:
    """Calibrated perceptual-structure similarity, 0-100, with a colour veto.

    Colour used to be blended in as 20% of the score. Measured on 132 real
    images (video frames and posters) against 720 transformed duplicates,
    that made the matcher *worse in both directions*: unrelated images share
    average colour far more often than they share structure - the colour term
    scores a median of 83.7 on unrelated pairs - so it inflated exactly the
    hardest false positives, while adding nothing structure did not already
    carry. Calibrating colour against its own noise floor came out worst of
    the three, because ordinary colour grading then reads as a real change.

        formulation           worst positive   worst negative   gap
        blend (old)                    77.86            54.03   +23.82
        blend, colour calibrated       66.60            48.95   +17.65
        structure only                 75.00            43.75   +31.25

    Colour still has one job structure cannot do: telling apart images with
    no structure to compare. Two solid colours produce near-identical
    perceptual hashes (red vs white: 81.25). So colour is kept, but only as a
    veto - it can reject a pair, never lift one (A-06).
    """
    if len(left) != FINGERPRINT_LENGTH or len(right) != FINGERPRINT_LENGTH:
        return 0.0
    color_distance = sum(abs(a - b) for a, b in zip(left[ORIENTATION_COUNT:], right[ORIENTATION_COUNT:]))
    if 100.0 * (1.0 - color_distance / (3 * 255)) < COLOR_VETO_SIMILARITY:
        return 0.0
    # The dihedral group is closed, so one side's eight views against the
    # other's own view already covers every relative orientation (A-15).
    best = max(hamming_similarity([value], [right[0]]) for value in left[:ORIENTATION_COUNT])
    return calibrate_similarity(best)


# SWAR 64-bit population count, vectorized over a numpy array. Portable to
# any numpy version (`numpy.bitwise_count` needs numpy>=2.0), and the batch
# comparator below calls it far more often than pure-Python `int.bit_count()`
# could keep up with at library scale.
_POPCOUNT_M1 = np.uint64(0x5555555555555555)
_POPCOUNT_M2 = np.uint64(0x3333333333333333)
_POPCOUNT_M4 = np.uint64(0x0F0F0F0F0F0F0F0F)
_POPCOUNT_H01 = np.uint64(0x0101010101010101)
_POPCOUNT_ONE = np.uint64(1)
_POPCOUNT_TWO = np.uint64(2)
_POPCOUNT_FOUR = np.uint64(4)
_POPCOUNT_FIFTYSIX = np.uint64(56)


def _popcount64_into(values: np.ndarray, scratch: np.ndarray) -> None:
    """SWAR popcount of `values`, in place, using `scratch` as its only
    temporary.

    The expression form of this (`_popcount64` below) allocates a fresh
    full-size uint64 array for each of its seven intermediate steps. At the
    comparison tile sizes this project runs, each of those is tens to
    hundreds of megabytes, allocated and freed eight times per tile - so the
    in-place form is both meaningfully faster (fewer trips to main memory)
    and the difference between a bounded and an alarming peak. Both buffers
    are allocated once per tile by `_batch_similarity` and reused across all
    eight orientations.
    """
    np.right_shift(values, _POPCOUNT_ONE, out=scratch)
    np.bitwise_and(scratch, _POPCOUNT_M1, out=scratch)
    np.subtract(values, scratch, out=values)
    np.bitwise_and(values, _POPCOUNT_M2, out=scratch)
    np.right_shift(values, _POPCOUNT_TWO, out=values)
    np.bitwise_and(values, _POPCOUNT_M2, out=values)
    np.add(values, scratch, out=values)
    np.right_shift(values, _POPCOUNT_FOUR, out=scratch)
    np.add(values, scratch, out=values)
    np.bitwise_and(values, _POPCOUNT_M4, out=values)
    np.multiply(values, _POPCOUNT_H01, out=values)
    np.right_shift(values, _POPCOUNT_FIFTYSIX, out=values)


def _popcount64(values: np.ndarray) -> np.ndarray:
    """Non-mutating popcount, for callers that want a plain function."""
    result = values.astype(np.uint64)
    _popcount64_into(result, np.empty_like(result))
    return result


# `image_similarity`'s two affine steps - raw Hamming similarity from a
# 0-64 bit distance, then `calibrate_similarity` - composed into one
# multiply-add so the batch form needs a single pass over one float buffer:
#
#     calibrate(100 * (1 - d/64)) == 100 - d * (100/64) * (100/(100-NOISE_FLOOR))
#
# At the shipped NOISE_FLOOR that is `100 - 3.125 * d`. Every value it can
# produce for an integer d in 0..64 is a multiple of 0.125 within +-100, so
# float32 represents all of them exactly and the result is bit-identical to
# the float64 form it replaces at half the memory.
_CALIBRATED_SLOPE = np.float32(-(100.0 / 64.0) * (100.0 / (100.0 - NOISE_FLOOR)))
_CALIBRATED_INTERCEPT = np.float32(100.0)
# `100 * (1 - color_distance / (3*255)) < COLOR_VETO_SIMILARITY`, solved for
# the distance, so the veto is one comparison against the integer distance
# instead of a second full-size float array. Written in this order because
# `(100 - 70) * 765 / 100` is exact in binary floating point where
# `(1 - 70/100) * 765` is not.
_COLOR_VETO_DISTANCE = (100.0 - COLOR_VETO_SIMILARITY) * (3 * 255) / 100.0


def _batch_similarity(
    left_hashes: np.ndarray,
    left_colors: np.ndarray,
    right_hashes: np.ndarray,
    right_colors: np.ndarray,
) -> np.ndarray:
    """`image_similarity`, batched over every (left[i], right[j]) pair at
    once. Verified equivalent to calling `image_similarity` pair-by-pair,
    including at the colour veto's exact boundary (`tests/test_images.py`).

    `left_hashes`/`right_hashes` are `(n, 8)` uint64, one column per dihedral
    orientation; only `right_hashes[:, 0]` (the canonical view) is used, by
    the same reasoning `image_similarity` documents (A-15: the group is
    closed, so one side's eight views against the other's own view already
    covers every relative orientation). `left_colors`/`right_colors` are
    `(n, 3)` average-colour channels.

    Every full-size intermediate is allocated once and written in place. The
    expression-style version this replaced allocated roughly a dozen of them
    per tile, which measured 784 MB peak and 3.2 s on a 4000x4000 tile
    against 528 MB and 1.7 s here, for bit-identical output (A-36).
    """
    shape = (left_hashes.shape[0], right_hashes.shape[0])
    canonical_right = right_hashes[:, 0]
    min_distance = np.full(shape, 64, dtype=np.uint8)
    distance = np.empty(shape, dtype=np.uint64)
    scratch = np.empty(shape, dtype=np.uint64)
    for orientation in range(ORIENTATION_COUNT):
        np.bitwise_xor(left_hashes[:, orientation][:, None], canonical_right[None, :], out=distance)
        _popcount64_into(distance, scratch)
        np.minimum(min_distance, distance, out=min_distance)
    del distance, scratch

    calibrated = min_distance.astype(np.float32)
    calibrated *= _CALIBRATED_SLOPE
    calibrated += _CALIBRATED_INTERCEPT
    np.clip(calibrated, 0.0, 100.0, out=calibrated)

    # Accumulated one channel at a time: the broadcast difference of all
    # three at once is an (n, m, 3) array, three times the size of anything
    # else here and the single largest allocation in the old form.
    color_distance = np.zeros(shape, dtype=np.int32)
    channel_distance = np.empty(shape, dtype=np.int32)
    for channel in range(3):
        np.subtract(
            left_colors[:, channel, None].astype(np.int32),
            right_colors[None, :, channel].astype(np.int32),
            out=channel_distance,
        )
        np.abs(channel_distance, out=channel_distance)
        color_distance += channel_distance
    calibrated[color_distance > _COLOR_VETO_DISTANCE] = 0.0
    return calibrated


# Upper bound on the comparison tile size, whatever `max_candidates` asks for
# (A-36). A tile holds several arrays of tile^2 elements, so the request is a
# quadratic memory claim: 4096 is already ~490 MB per tile and 20,000 would
# be ~12 GB. Nothing above a few hundred per side is faster either - measured
# on 20,000 fingerprints, the whole comparison took 12.1 s at 512 and 17.6 s
# at 4000, because past roughly a thousand the working set stops fitting in
# cache and every pass goes to main memory. So this ceiling only exists to
# turn a mistyped setting into a slow run instead of a MemoryError.
IMAGE_COMPARISON_TILE_LIMIT = 4096
# Chosen by measuring the full comparison across tile sizes on 20,000
# fingerprints (A-36): 32.2 s at 64, 13.1 s at 256, **12.1 s at 512**, 12.7 s
# at 1024, 17.6 s at 4000. 512 is the flat bottom of that curve and costs
# 4.6 MB of tile buffers, against 272 MB at the 4000 this replaced.
IMAGE_COMPARISON_TILE = 512


def find_image_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    workers: int = 4,
    max_candidates: int = IMAGE_COMPARISON_TILE,
    orientations: str = "full",
) -> list[ImageMatch]:
    """Fingerprint every unfingerprinted image, then compare all of them.

    `max_candidates` is a tile size, not a selectivity filter (A-18). The bit
    -blocking scheme this replaced needed narrow blocks to bound a Hamming
    search radius, and at the shipped `image_threshold` that radius (12 of 64
    bits) is wide enough that every block collides on any library above a
    few hundred images - measured generating ~100% of all pairs from 1200
    images up, while still paying the cost of building and internally
    deduplicating that same near-total pair set. Comparing every pair with
    numpy, tiled so memory stays bounded, is fast enough (measured: the full
    `n^2` comparison, not a filtered subset) that no selectivity trick is
    needed.

    `max_candidates` is therefore purely a memory/locality knob, and it is
    not monotone: raising it does not buy speed (A-36). Fewer Python-level
    iterations over tiles is a rounding error next to keeping each tile's
    buffers in cache, so the useful range is a few hundred per side and the
    request is clamped to `IMAGE_COMPARISON_TILE_LIMIT`.

    `orientations="canonical"` skips detecting rotated/mirrored duplicates
    (A-15) to fingerprint faster - see `IMAGE_ORIENTATION_MODES`. Cached
    fingerprints from a previous run are reused as-is regardless of which
    mode produced them (both are valid 11-element fingerprints in the same
    format), so switching modes only changes newly computed files; rerun
    with `--refresh-images` to apply a mode change to already-cached ones.
    """
    missing = [record for record in records if record.image_fingerprint is None]
    if missing:
        with cancellable_executor(workers) as executor:
            futures = {executor.submit(fingerprint_image, record, orientations): record for record in missing}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Image fingerprints", unit="file"):
                record = futures[future]
                fingerprint = future.result()
                if fingerprint:
                    record.image_fingerprint = fingerprint
                    record.image_fingerprint_version = IMAGE_FINGERPRINT_VERSION
                    cache.upsert_file(record)
                    cache.commit_periodically()
        cache.commit()

    candidates = [
        record
        for record in records
        if record.image_fingerprint is not None and len(record.image_fingerprint) == FINGERPRINT_LENGTH
    ]
    # A "canonical" fingerprint stores one hash in all eight slots, so it is
    # recognisable without being recorded anywhere - which matters because a
    # cache can hold both kinds at once (switching modes does not invalidate
    # it) and only one direction of a mixed pair can see a rotation: the
    # comparison reads all eight orientations of the left side against the
    # right side's canonical view alone. Ordering full-orientation
    # fingerprints first makes the left side of every mixed pair the one that
    # has the eight views, so a rotated duplicate is still found whenever
    # either side knows about rotations. Without this the answer depended on
    # which file happened to come first in the scan - the same pair scoring
    # 100.0 or 0.0 by directory order (A-36). The sort is stable, so pairs
    # where both sides agree keep the order they already had.
    canonical_flags: list[bool] = []
    for record in candidates:
        fingerprint = record.image_fingerprint
        assert fingerprint is not None
        canonical_flags.append(len(set(fingerprint[:ORIENTATION_COUNT])) == 1)
    if any(canonical_flags) and not all(canonical_flags):
        LOGGER.warning(
            "%d of %d cached image fingerprints were built without rotation/mirror views and %d with them; "
            "rotated duplicates between the two groups are only partly detectable. "
            "Rerun with --refresh-images to rebuild them all in the current --image-orientations mode.",
            sum(canonical_flags),
            len(candidates),
            len(candidates) - sum(canonical_flags),
        )
        candidates = [record for record, _ in sorted(zip(candidates, canonical_flags), key=lambda item: item[1])]
    matches: list[ImageMatch] = []
    total = len(candidates)
    if total < 2 or threshold > 100:
        return matches

    hashes = np.empty((total, ORIENTATION_COUNT), dtype=np.uint64)
    colors = np.empty((total, 3), dtype=np.int16)
    for index, record in enumerate(candidates):
        fingerprint = record.image_fingerprint
        assert fingerprint is not None
        hashes[index] = fingerprint[:ORIENTATION_COUNT]
        colors[index] = fingerprint[ORIENTATION_COUNT:]

    tile = max(1, min(max_candidates, IMAGE_COMPARISON_TILE_LIMIT))
    if max_candidates > IMAGE_COMPARISON_TILE_LIMIT:
        LOGGER.warning(
            "Image comparison tile size %d exceeds the %d limit and would claim roughly %.1f GB per tile; "
            "using %d instead.",
            max_candidates,
            IMAGE_COMPARISON_TILE_LIMIT,
            max_candidates**2 * 29 / 1e9,
            tile,
        )
    tile_starts = list(range(0, total, tile))
    tile_pairs = [
        (a_start, b_start) for a_position, a_start in enumerate(tile_starts) for b_start in tile_starts[a_position:]
    ]
    for a_start, b_start in tqdm(tile_pairs, desc="Image comparison", unit="tile"):
        check_cancelled()
        a_end = min(a_start + tile, total)
        b_end = min(b_start + tile, total)
        calibrated = _batch_similarity(
            hashes[a_start:a_end],
            colors[a_start:a_end],
            hashes[b_start:b_end],
            colors[b_start:b_end],
        )
        rows, cols = np.nonzero(calibrated >= threshold)
        if a_start == b_start:
            # Within one tile both axes cover the same candidates - keep only
            # one direction per pair and drop self-pairs.
            keep = rows < cols
            rows, cols = rows[keep], cols[keep]
        for row, col in zip(rows.tolist(), cols.tolist()):
            left = candidates[a_start + row]
            right = candidates[b_start + col]
            if left.full_hash and left.full_hash == right.full_hash:
                continue
            pair = sorted((left.path_key, right.path_key))
            matches.append(ImageMatch(pair[0], pair[1], round(float(calibrated[row, col]), 2)))
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
