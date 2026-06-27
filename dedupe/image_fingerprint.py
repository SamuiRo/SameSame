from __future__ import annotations

import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import Cache
from .models import FileRecord, ImageMatch
from .progress import check_cancelled, tqdm
from .video_fingerprint import hamming_similarity, phash

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

PIL_AVAILABLE = Image is not None and ImageOps is not None
LOGGER = logging.getLogger(__name__)
STRUCTURE_WEIGHT = 0.8
COLOR_WEIGHT = 0.2


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
            average = rgb.resize((1, 1), Image.Resampling.LANCZOS).getpixel((0, 0))
            structure_hash = phash(rgb)
            return [structure_hash, int(average[0]), int(average[1]), int(average[2])]
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        LOGGER.warning("Cannot fingerprint image %s: %s", record.path, exc)
        return None


def image_similarity(left: list[int], right: list[int]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    structure_similarity = hamming_similarity([left[0]], [right[0]])
    color_distance = sum(abs(a - b) for a, b in zip(left[1:], right[1:]))
    color_similarity = 100.0 * (1.0 - color_distance / (3 * 255))
    return max(0.0, STRUCTURE_WEIGHT * structure_similarity + COLOR_WEIGHT * color_similarity)


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
    if any(fingerprint is None or len(fingerprint) != 4 for fingerprint in fingerprints):
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}
    if threshold <= 0:
        return {(left, right) for left in range(len(candidates)) for right in range(left + 1, len(candidates))}
    if threshold > 100:
        return set()

    minimum_structure_similarity = max(0.0, (threshold - COLOR_WEIGHT * 100.0) / STRUCTURE_WEIGHT)
    max_distance = math.floor(64 * (1.0 - minimum_structure_similarity / 100.0) + 1e-9)
    block_count = min(64, max_distance + 1)
    base_block_size, larger_blocks = divmod(64, block_count)
    block_sizes = [base_block_size + (1 if index < larger_blocks else 0) for index in range(block_count)]

    blocks: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, fingerprint in enumerate(fingerprints):
        check_cancelled()
        structure_hash = fingerprint[0] if fingerprint else 0
        remaining_bits = 64
        for block_index, block_size in enumerate(block_sizes):
            remaining_bits -= block_size
            block_value = (structure_hash >> remaining_bits) & ((1 << block_size) - 1)
            blocks[(block_index, block_value)].append(index)

    pairs: set[tuple[int, int]] = set()
    for indexes in blocks.values():
        check_cancelled()
        for left_position, left in enumerate(indexes):
            for right in indexes[left_position + 1 :]:
                pairs.add((min(left, right), max(left, right)))
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
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fingerprint_image, record): record for record in missing}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Image fingerprints", unit="file"):
                record = futures[future]
                fingerprint = future.result()
                if fingerprint:
                    record.image_fingerprint = fingerprint
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
