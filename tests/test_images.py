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
from dedupe.image_fingerprint import PIL_AVAILABLE, find_image_matches
from dedupe.models import FileRecord, NormalizedName
from dedupe.scanner import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, normalize_extensions

if PIL_AVAILABLE:
    from PIL import Image, ImageDraw


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

            normalized = {
                record.raw_name: NormalizedName(record.raw_name, record.raw_name)
                for record in records
            }
            assignments = build_cluster_assignments(
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
            self.assertIn("Схожі зображення", html_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
