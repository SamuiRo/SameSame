from __future__ import annotations

import logging
import math
from collections import defaultdict
from concurrent.futures import as_completed

from .cache import Cache
from .models import IMAGE_FINGERPRINT_VERSION, FileRecord, ImageMatch
from .progress import cancellable_executor, check_cancelled, tqdm
from .video_fingerprint import calibrate_similarity, decalibrate_similarity, hamming_similarity, phash

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


def fingerprint_image(record: FileRecord) -> list[int] | None:
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
            hashes = [phash(view) for view in _orientations(trimmed)]
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


def _candidate_pairs(
    candidates: list[FileRecord],
    max_candidates: int,
    threshold: float,
) -> set[tuple[int, int]]:
    if len(candidates) <= max_candidates:
        pairs: set[tuple[int, int]] = set()
        for left in range(len(candidates)):
            check_cancelled()
            pairs.update((left, right) for right in range(left + 1, len(candidates)))
        return pairs

    fingerprints = [record.image_fingerprint for record in candidates]
    if any(fingerprint is None or len(fingerprint) != FINGERPRINT_LENGTH for fingerprint in fingerprints):
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}
    if threshold <= 0:
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}
    if threshold > 100:
        return set()

    # `threshold` is calibrated (F-24); the bit-distance math below needs the
    # raw scale, since a Hamming distance has no notion of "noise floor" -
    # only the reported/compared similarity does. Convert once, here, rather
    # than changing `image_similarity`'s or the caller's contract. Since A-06
    # the score *is* the calibrated structure similarity, so the bound is
    # exact rather than an allowance for a second blended term.
    minimum_structure_similarity_raw = decalibrate_similarity(min(100.0, threshold))
    max_distance = math.floor(64 * (1.0 - minimum_structure_similarity_raw / 100.0) + 1e-9)
    block_count = min(64, max_distance + 1)
    base_block_size, larger_blocks = divmod(64, block_count)
    block_sizes = [base_block_size + (1 if index < larger_blocks else 0) for index in range(block_count)]

    blocks: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, fingerprint in enumerate(fingerprints):
        check_cancelled()
        # Indexed under all eight orientations, not just the stored one: a
        # rotated duplicate shares no bits with the upright view, so blocking
        # on one orientation would drop exactly the pairs A-15 added.
        for structure_hash in (fingerprint or [0])[:ORIENTATION_COUNT]:
            remaining_bits = 64
            for block_index, block_size in enumerate(block_sizes):
                remaining_bits -= block_size
                block_value = (structure_hash >> remaining_bits) & ((1 << block_size) - 1)
                blocks[(block_index, block_value)].append(index)

    pairs: set[tuple[int, int]] = set()
    for indexes in blocks.values():
        check_cancelled()
        unique = sorted(set(indexes))
        for left_position, left in enumerate(unique):
            for right in unique[left_position + 1 :]:
                pairs.add((left, right))
    return pairs


def find_image_matches(
    records: list[FileRecord],
    cache: Cache,
    threshold: float,
    workers: int = 4,
    max_candidates: int = 250,
) -> list[ImageMatch]:
    missing = [record for record in records if record.image_fingerprint is None]
    if missing:
        with cancellable_executor(workers) as executor:
            futures = {executor.submit(fingerprint_image, record): record for record in missing}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Image fingerprints", unit="file"):
                record = futures[future]
                fingerprint = future.result()
                if fingerprint:
                    record.image_fingerprint = fingerprint
                    record.image_fingerprint_version = IMAGE_FINGERPRINT_VERSION
                    cache.upsert_file(record)
        cache.conn.commit()

    candidates = [record for record in records if record.image_fingerprint is not None]
    matches: list[ImageMatch] = []
    for left_index, right_index in _candidate_pairs(candidates, max_candidates, threshold):
        check_cancelled()
        left = candidates[left_index]
        right = candidates[right_index]
        if left.full_hash and left.full_hash == right.full_hash:
            continue
        if left.image_fingerprint is None or right.image_fingerprint is None:
            continue
        similarity = image_similarity(left.image_fingerprint, right.image_fingerprint)
        if threshold <= similarity:
            pair = sorted((left.path_key, right.path_key))
            matches.append(ImageMatch(pair[0], pair[1], round(similarity, 2)))
    matches.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return matches
