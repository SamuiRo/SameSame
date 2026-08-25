from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cache import Cache
from dedupe.cli import main
from dedupe.exact_hash import find_exact_duplicates
from dedupe.folder_compare import build_cluster_assignments
from dedupe import image_fingerprint
from dedupe.image_fingerprint import (
    COLOR_VETO_SIMILARITY,
    FINGERPRINT_LENGTH,
    ORIENTATION_COUNT,
    PIL_AVAILABLE,
    _popcount64,
    fingerprint_image,
    find_image_matches,
    image_similarity,
    trim_border,
)
from dedupe.models import FileRecord, NormalizedName
from dedupe.scanner import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, normalize_extensions

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard dependency, guarded for safety only
    np = None  # type: ignore[assignment]

if PIL_AVAILABLE:
    from PIL import Image, ImageDraw, ImageEnhance


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is required for image fingerprint tests")
class ImageFingerprintTests(unittest.TestCase):
    def _create_images(self, base: Path) -> tuple[Path, Path, Path]:
        source = base / "source.png"
        resized = base / "nested" / "resized.jpg"
        different = base / "different.png"
        resized.parent.mkdir(parents=True)

        image = Image.new("RGB", (320, 200), "navy")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 25, 290, 175), fill="gold")
        draw.ellipse((100, 45, 220, 165), fill="crimson")
        draw.line((0, 0, 319, 199), fill="white", width=6)
        image.save(source)
        image.resize((800, 500)).save(resized, quality=72)

        other = Image.new("RGB", (320, 200), "darkgreen")
        other_draw = ImageDraw.Draw(other)
        other_draw.polygon([(160, 10), (310, 190), (10, 190)], fill="cyan")
        other.save(different)
        return source, resized, different

    def _records(self, paths: tuple[Path, ...], root: Path) -> list[FileRecord]:
        records = []
        for path in paths:
            stat = path.stat()
            records.append(
                FileRecord(
                    path=path.resolve(),
                    root=root.resolve(),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    raw_name=path.stem,
                )
            )
        return records

    def test_default_extensions_include_images_and_video(self) -> None:
        extensions = normalize_extensions(None)
        self.assertTrue(IMAGE_EXTENSIONS <= extensions)
        self.assertTrue(VIDEO_EXTENSIONS <= extensions)

    def test_resized_reencoded_image_matches_but_different_image_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, resized, different = self._create_images(base)
            records = self._records((source, resized, different), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=90, workers=1)

            matched_names = [{Path(match.left).name, Path(match.right).name} for match in matches]
            self.assertIn({"source.png", "resized.jpg"}, matched_names)
            self.assertNotIn({"source.png", "different.png"}, matched_names)

    def test_exact_image_duplicates_are_not_repeated_as_perceptual_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, _, _ = self._create_images(base)
            copy = base / "copy.png"
            copy.write_bytes(source.read_bytes())
            records = self._records((source, copy), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                exact_groups = find_exact_duplicates(records, cache, workers=1)
                image_matches = find_image_matches(records, cache, threshold=90, workers=1)

            self.assertEqual(len(exact_groups), 1)
            self.assertEqual(image_matches, [])

    def _textured(self, path: Path, seed: int = 0) -> Path:
        image = Image.new("RGB", (320, 200), "navy")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20 + seed, 15, 300, 150), fill="gold")
        draw.ellipse((70, 30 + seed, 250, 180), fill="crimson")
        draw.line((0, 199, 319, seed), fill="white", width=6)
        image.save(path)
        return path

    def _fingerprint(self, path: Path) -> list[int]:
        record = FileRecord(path, path.parent, 0, 0.0, path.name)
        fingerprint = fingerprint_image(record)
        assert fingerprint is not None
        return fingerprint

    def test_rotations_and_mirrors_match_exactly(self) -> None:
        # A-15: a perceptual hash of a rotated picture shares nothing with the
        # original - every 90-degree rotation and every mirror scored exactly
        # 0 before the dihedral views were stored.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            original = self._fingerprint(source)

            with Image.open(source) as opened:
                image = opened.convert("RGB")
                for label, view in (
                    ("rotate 90", image.rotate(90, expand=True)),
                    ("rotate 180", image.rotate(180, expand=True)),
                    ("rotate 270", image.rotate(270, expand=True)),
                    ("mirror", image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
                ):
                    target = base / f"{label.replace(' ', '_')}.png"
                    view.save(target)
                    with self.subTest(transform=label):
                        self.assertEqual(image_similarity(self._fingerprint(target), original), 100.0)

    def test_canonical_orientation_mode_stores_one_hash_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            record = FileRecord(source, base, 0, 0.0, source.name)
            fingerprint = fingerprint_image(record, orientations="canonical")
            assert fingerprint is not None
            self.assertEqual(fingerprint[:ORIENTATION_COUNT], [fingerprint[0]] * ORIENTATION_COUNT)
            self.assertEqual(len(fingerprint), FINGERPRINT_LENGTH)

    def test_canonical_orientation_mode_misses_a_rotated_duplicate(self) -> None:
        # The speed/detection trade-off canonical mode exists for: a 90-degree
        # rotation matches at 100.0 in the default "full" mode (A-15) but is
        # indistinguishable from an unrelated image once only one orientation
        # is fingerprinted per side.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            original_record = FileRecord(source, base, 0, 0.0, source.name)
            original = fingerprint_image(original_record, orientations="canonical")
            assert original is not None

            with Image.open(source) as opened:
                rotated_path = base / "rotated.png"
                opened.convert("RGB").rotate(90, expand=True).save(rotated_path)
            rotated_record = FileRecord(rotated_path, base, 0, 0.0, rotated_path.name)
            rotated = fingerprint_image(rotated_record, orientations="canonical")
            assert rotated is not None

            self.assertEqual(image_similarity(rotated, original), 0.0)

    def test_canonical_orientation_mode_still_matches_same_orientation_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, resized, different = self._create_images(base)
            records = self._records((source, resized, different), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=90, workers=1, orientations="canonical")

            matched_names = [{Path(match.left).name, Path(match.right).name} for match in matches]
            self.assertIn({"source.png", "resized.jpg"}, matched_names)
            self.assertNotIn({"source.png", "different.png"}, matched_names)

    def test_mixed_orientation_cache_finds_a_rotated_duplicate_in_either_order(self) -> None:
        # A-36: a cache can hold both fingerprint kinds at once, because
        # switching --image-orientations does not invalidate it. Only one
        # direction of such a pair can see a rotation - all eight views of the
        # left side against the right side's canonical view - so before
        # full-orientation fingerprints were ordered first, this same pair
        # scored 100.0 or was missed entirely depending on directory order.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            rotated_path = base / "rotated.png"
            with Image.open(source) as opened:
                opened.convert("RGB").rotate(90, expand=True).save(rotated_path)

            for index, order in enumerate(((source, rotated_path), (rotated_path, source))):
                with self.subTest(first=order[0].name):
                    records = self._records(order, base)
                    records[0].image_fingerprint = fingerprint_image(records[0], orientations="canonical")
                    records[1].image_fingerprint = fingerprint_image(records[1], orientations="full")
                    with Cache(base / f"cache-{index}.sqlite3") as cache:
                        matches = find_image_matches(records, cache, threshold=90, workers=1)
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(matches[0].similarity, 100.0)

    def test_letterboxing_is_trimmed_before_hashing(self) -> None:
        # A-15: bars and margins used to change the descriptor outright.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            with Image.open(source) as opened:
                image = opened.convert("RGB")
                boxed = Image.new("RGB", (image.width, image.height + 60), "black")
                boxed.paste(image, (0, 30))
                boxed.save(base / "letterboxed.png")

            self.assertEqual(
                image_similarity(self._fingerprint(base / "letterboxed.png"), self._fingerprint(source)),
                100.0,
            )

    def test_border_trimming_survives_a_brightness_change(self) -> None:
        # A-15: the border tolerance is an absolute number of levels, so on a
        # raw image a 20% darkening shrinks every difference by 20% and moves
        # the boundary - measured on two real frames, that turned a pair
        # scoring 100 into 43.75 and 50.00. Detection runs on a
        # contrast-normalised copy for exactly this reason.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._textured(base / "source.png")
            with Image.open(source) as opened:
                image = opened.convert("RGB")
                boxed = Image.new("RGB", (image.width, image.height + 60), "black")
                boxed.paste(image, (0, 30))
                boxed.save(base / "boxed.png")
                ImageEnhance.Brightness(boxed).enhance(0.8).save(base / "boxed_dark.png")

            self.assertEqual(
                trim_border(Image.open(base / "boxed.png").convert("RGB")).size,
                trim_border(Image.open(base / "boxed_dark.png").convert("RGB")).size,
            )

    def test_trimming_refuses_to_swallow_the_picture(self) -> None:
        # A-15: trimming is for borders. A picture that is mostly one colour
        # must not be reduced to whatever small area differs from it.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            image = Image.new("RGB", (400, 400), "white")
            ImageDraw.Draw(image).rectangle((180, 180, 220, 220), fill="black")
            image.save(base / "mostly_blank.png")

            trimmed = trim_border(Image.open(base / "mostly_blank.png").convert("RGB"))
            self.assertEqual(trimmed.size, (400, 400))

    def test_different_solid_colors_are_not_false_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            red = base / "red.png"
            blue = base / "blue.png"
            Image.new("RGB", (200, 200), "red").save(red)
            Image.new("RGB", (200, 200), "blue").save(blue)
            records = self._records((red, blue), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=90, workers=1)

            self.assertEqual(matches, [])

    def test_solid_colors_stay_apart_at_the_default_threshold(self) -> None:
        # A-06: colour is no longer 20% of the score, so this case now rests
        # entirely on the veto. Two flat images have almost no structure to
        # compare - red vs white hash to 81.25% similar - and the old test
        # only passed because it used a threshold far above the default.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = []
            for name in ("red", "white", "blue"):
                path = base / f"{name}.png"
                Image.new("RGB", (200, 200), name).save(path)
                paths.append(path)
            records = self._records(tuple(paths), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=60, workers=1)

            self.assertEqual(matches, [])

    def test_colour_grading_still_matches(self) -> None:
        # A-06: the veto must not reject a duplicate that was merely graded
        # differently - the reason a *calibrated* colour term was rejected.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source.png"
            graded = base / "graded.png"
            image = Image.new("RGB", (320, 200), "navy")
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 25, 290, 175), fill="gold")
            draw.ellipse((100, 45, 220, 165), fill="crimson")
            image.save(source)
            ImageEnhance.Brightness(image).enhance(1.2).save(graded)
            records = self._records((source, graded), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=60, workers=1)

            self.assertEqual(len(matches), 1)

    def test_similar_average_colour_does_not_lift_an_unrelated_pair(self) -> None:
        # A-06: the old blend added up to 20 points for a shared average
        # colour, which unrelated images share far more often than structure.
        # Colour may now only veto, so it cannot raise this pair at all.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            left = base / "left.png"
            right = base / "right.png"
            for path, shape in ((left, "rect"), (right, "ellipse")):
                image = Image.new("RGB", (240, 240), "gray")
                draw = ImageDraw.Draw(image)
                if shape == "rect":
                    draw.rectangle((10, 10, 120, 230), fill="black")
                    draw.rectangle((130, 10, 230, 230), fill="white")
                else:
                    draw.ellipse((20, 60, 220, 180), fill="white")
                    draw.line((0, 0, 239, 239), fill="black", width=20)
                image.save(path)
            records = self._records((left, right), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_image_matches(records, cache, threshold=60, workers=1)

            self.assertEqual(matches, [])

    def test_image_fingerprints_are_reused_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, resized, _ = self._create_images(base)
            records = self._records((source, resized), base)
            cache_path = base / "cache.sqlite3"

            with Cache(cache_path) as cache:
                cache.upsert_files(records)
                first_matches = find_image_matches(records, cache, threshold=90, workers=1)

            cached_records = self._records((source, resized), base)
            with Cache(cache_path) as cache:
                for record in cached_records:
                    self.assertTrue(cache.hydrate_if_current(record))
                    self.assertIsNotNone(record.image_fingerprint)
                with patch("dedupe.image_fingerprint.fingerprint_image", side_effect=AssertionError("cache miss")):
                    second_matches = find_image_matches(cached_records, cache, threshold=90, workers=1)

            self.assertEqual(first_matches, second_matches)

    def test_exact_and_resized_copies_share_one_content_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, resized, _ = self._create_images(base)
            exact_copy = base / "exact-copy.png"
            exact_copy.write_bytes(source.read_bytes())
            records = self._records((source, exact_copy, resized), base)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                exact_groups = find_exact_duplicates(records, cache, workers=1)
                image_matches = find_image_matches(records, cache, threshold=90, workers=1)

            normalized = {record.raw_name: NormalizedName(record.raw_name, record.raw_name) for record in records}
            assignments, _clusters = build_cluster_assignments(
                records,
                exact_groups,
                [],
                normalized,
                image_matches=image_matches,
            )
            cluster_ids = {assignments[record.path_key].cluster_id for record in records}
            levels = {assignments[record.path_key].level for record in records}

            self.assertEqual(len(cluster_ids), 1)
            self.assertEqual(levels, {"image"})

    def test_cli_scans_nested_images_and_writes_image_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            left = base / "left"
            right = base / "right"
            left.mkdir()
            right.mkdir()
            source, resized, different = self._create_images(base / "fixtures")
            left_source = left / "photo.png"
            right_resized = right / "deep" / "photo-copy.jpg"
            right_different = right / "different.png"
            right_resized.parent.mkdir()
            left_source.write_bytes(source.read_bytes())
            right_resized.write_bytes(resized.read_bytes())
            right_different.write_bytes(different.read_bytes())

            json_report = base / "report.json"
            html_report = base / "report.html"
            exit_code = main(
                [
                    "--folders",
                    str(left),
                    str(right),
                    "--name-provider",
                    "none",
                    "--cache",
                    str(base / "cache.sqlite3"),
                    "--json-output",
                    str(json_report),
                    "--output",
                    str(html_report),
                ]
            )

            report = json.loads(json_report.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["scanned_files"], 3)
            self.assertEqual(len(report["image_matches"]), 1)
            self.assertEqual(report["warnings"], [])
            self.assertTrue(report["folder_pairs"][0]["matched"][0]["content_backed"])
            self.assertEqual(report["folder_pairs"][0]["matched"][0]["level"], "image")
            self.assertIn("Similar images", html_report.read_text(encoding="utf-8"))


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is required for image fingerprint tests")
class ImageBatchComparisonTests(unittest.TestCase):
    """A-18: the bit-blocking candidate prefilter had no selectivity at the
    shipped threshold (generating ~100% of all pairs from 1200 images up) and
    still paid the cost of building that near-total set. It is replaced by a
    tiled numpy comparison of every pair, verified below equivalent to the
    pairwise `image_similarity` it replaces - not merely "no test failed",
    since these tests are checked to fail against a broken batch comparator.
    """

    def _random_records(self, count: int, seed: int) -> list[FileRecord]:
        rng = random.Random(seed)
        records = []
        for index in range(count):
            fingerprint = [rng.getrandbits(64) for _ in range(ORIENTATION_COUNT)]
            fingerprint += [rng.randint(0, 255) for _ in range(3)]
            record = FileRecord(
                path=Path(f"/fake/img_{index}.png"),
                root=Path("/fake"),
                size=100,
                mtime=0.0,
                raw_name=f"img_{index}",
            )
            record.image_fingerprint = fingerprint
            records.append(record)
        return records

    def _brute_force_matches(self, records: list[FileRecord], threshold: float) -> set[tuple[str, str, float]]:
        expected = set()
        for left_index in range(len(records)):
            for right_index in range(left_index + 1, len(records)):
                left, right = records[left_index], records[right_index]
                if left.full_hash and left.full_hash == right.full_hash:
                    continue
                similarity = image_similarity(left.image_fingerprint, right.image_fingerprint)
                if similarity >= threshold:
                    pair = sorted((left.path_key, right.path_key))
                    expected.add((pair[0], pair[1], round(similarity, 2)))
        return expected

    def test_batched_comparison_matches_pairwise_image_similarity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for threshold, tile in ((60.0, 7), (60.0, 200), (85.0, 11), (0.0, 50), (100.0, 50)):
                with self.subTest(threshold=threshold, tile=tile):
                    records = self._random_records(140, seed=hash((threshold, tile)) & 0xFFFFFFFF)
                    expected = self._brute_force_matches(records, threshold)
                    with Cache(base / f"cache-{threshold}-{tile}.sqlite3") as cache:
                        matches = find_image_matches(
                            records, cache, threshold=threshold, workers=1, max_candidates=tile
                        )
                    actual = {(match.left, match.right, match.similarity) for match in matches}
                    self.assertEqual(actual, expected)

    def test_batched_comparison_agrees_at_the_colour_veto_boundary(self) -> None:
        # The batch form solves `100 * (1 - distance/765) < COLOR_VETO_SIMILARITY`
        # for the distance so the veto is one integer comparison instead of a
        # second full-size float array. That rearrangement is only safe if it
        # lands on exactly the same side of the boundary as the original, and
        # random fingerprints are a poor way to check one specific integer -
        # so walk every distance across it.
        boundary = round((100.0 - COLOR_VETO_SIMILARITY) * (3 * 255) / 100.0)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = []
            for offset in range(-4, 5):
                record = FileRecord(base / f"c{offset}.png", base, 100, 0.0, f"c{offset}")
                # Identical structure, so only the colour veto can separate
                # these from the reference below.
                record.image_fingerprint = [0] * ORIENTATION_COUNT + [boundary + offset, 0, 0]
                records.append(record)
            reference = FileRecord(base / "reference.png", base, 100, 0.0, "reference")
            reference.image_fingerprint = [0] * ORIENTATION_COUNT + [0, 0, 0]
            records.append(reference)

            expected = self._brute_force_matches(records, 90.0)
            with Cache(base / "cache.sqlite3") as cache:
                matches = find_image_matches(records, cache, threshold=90.0, workers=1, max_candidates=4)
            self.assertEqual({(m.left, m.right, m.similarity) for m in matches}, expected)
            # Guard the test itself: the boundary must actually fall inside
            # the swept range, or this proves nothing.
            self.assertEqual(image_similarity(records[0].image_fingerprint, reference.image_fingerprint), 100.0)
            self.assertEqual(image_similarity(records[-2].image_fingerprint, reference.image_fingerprint), 0.0)

    def test_oversized_tile_request_is_clamped_without_changing_results(self) -> None:
        # A-36: `max_candidates` is a quadratic memory claim - 20,000 per side
        # would be ~12 GB of tile buffers - so an unclamped large value is a
        # MemoryError rather than a slow run. The clamp must bite, and must
        # not change which pairs are found. The limit is patched down here
        # rather than allocating a real 4096-wide tile in a unit test.
        observed_tiles: list[tuple[int, int]] = []
        real_batch = image_fingerprint._batch_similarity

        def spy(left_hashes, left_colors, right_hashes, right_colors):  # type: ignore[no-untyped-def]
            observed_tiles.append((left_hashes.shape[0], right_hashes.shape[0]))
            return real_batch(left_hashes, left_colors, right_hashes, right_colors)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = self._random_records(60, seed=42)
            expected = self._brute_force_matches(records, 60.0)
            with (
                patch.object(image_fingerprint, "IMAGE_COMPARISON_TILE_LIMIT", 8),
                patch.object(image_fingerprint, "_batch_similarity", spy),
                Cache(base / "cache.sqlite3") as cache,
            ):
                matches = find_image_matches(records, cache, threshold=60.0, workers=1, max_candidates=10**6)

            self.assertEqual({(m.left, m.right, m.similarity) for m in matches}, expected)
            self.assertTrue(observed_tiles)
            self.assertLessEqual(max(max(pair) for pair in observed_tiles), 8)

    def test_exact_hash_pairs_are_excluded_even_when_perceptually_similar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = self._random_records(30, seed=1)
            records[3].full_hash = records[9].full_hash = "same-hash"
            records[9].image_fingerprint = list(records[3].image_fingerprint)
            with Cache(Path(tmp) / "cache.sqlite3") as cache:
                matches = find_image_matches(records, cache, threshold=0.0, workers=1, max_candidates=5)
            pairs = {(match.left, match.right) for match in matches}
            self.assertNotIn(
                tuple(sorted((records[3].path_key, records[9].path_key))),
                pairs,
            )

    def test_malformed_fingerprint_length_is_skipped_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = self._random_records(10, seed=2)
            records[4].image_fingerprint = records[4].image_fingerprint[:-1]
            with Cache(Path(tmp) / "cache.sqlite3") as cache:
                matches = find_image_matches(records, cache, threshold=0.0, workers=1, max_candidates=3)
            touched = {records[4].path_key} & ({m.left for m in matches} | {m.right for m in matches})
            self.assertEqual(touched, set())


@unittest.skipUnless(PIL_AVAILABLE and np is not None, "numpy and Pillow are required")
class PopcountTests(unittest.TestCase):
    def test_popcount64_matches_int_bit_count(self) -> None:
        rng = random.Random(0)
        values = [rng.getrandbits(64) for _ in range(500)] + [0, 2**64 - 1, 1, 1 << 63]
        expected = [value.bit_count() for value in values]
        actual = _popcount64(np.array(values, dtype=np.uint64)).tolist()
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
