from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cache import Cache
from dedupe.cli import main
from dedupe.exact_hash import find_exact_duplicates
from dedupe.folder_compare import build_cluster_assignments
from dedupe.image_fingerprint import (
    PIL_AVAILABLE,
    fingerprint_image,
    find_image_matches,
    image_similarity,
    trim_border,
)
from dedupe.models import FileRecord, NormalizedName
from dedupe.scanner import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, normalize_extensions

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


if __name__ == "__main__":
    unittest.main()
