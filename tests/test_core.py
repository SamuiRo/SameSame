from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.cache import Cache
from dedupe.cli import parse_args
from dedupe.exact_hash import find_exact_duplicates
from dedupe.folder_compare import build_cluster_assignments, compare_folders
from dedupe.models import ExactDuplicateGroup, FileRecord, NormalizedName
from dedupe.name_normalizer import (
    PROMPT_HASH,
    _coerce_normalized_results,
    _extract_json_object,
    contextual_name,
    fallback_normalize,
    find_name_hints,
    normalize_names,
)
from dedupe.scanner import scan_folders
from dedupe.video_fingerprint import (
    find_video_matches,
    ordered_sequence_similarity,
    video_durations_compatible,
)


class CoreTests(unittest.TestCase):
    def test_fallback_normalize_removes_common_noise(self) -> None:
        result = fallback_normalize("[Group] Show_Name.01.1080p.x264.SUB")
        self.assertEqual(result.core_title, "Show Name")
        self.assertEqual(result.episode, 1)
        self.assertIn("sub", result.flags)

    def test_fallback_normalize_uses_last_number_as_episode(self) -> None:
        result = fallback_normalize("Kutsujoku-2-The-Animation-1_sub")
        self.assertEqual(result.episode, 1)
        self.assertEqual(result.core_title, "Kutsujoku 2 The Animation")

    def test_fallback_normalize_ignores_resolution_without_p_suffix(self) -> None:
        result = fallback_normalize("Anehame-Ore-no-Hatsukoi_01_raw_720")
        self.assertEqual(result.episode, 1)
        self.assertEqual(result.core_title, "Anehame Ore no Hatsukoi raw 720")

    def test_fallback_normalize_treats_alt_as_release_marker(self) -> None:
        regular = fallback_normalize("Ane Chijo Max Heart Ep.4")
        alternate = fallback_normalize("Ane Chijo Max Heart Ep.4 alt")
        self.assertEqual(regular.cluster_key, alternate.cluster_key)

    def test_contextual_name_uses_parent_for_generic_episode_filename(self) -> None:
        root = Path("library").resolve()
        first = FileRecord(root / "Season One" / "01 - Episode 1 [1080p].mp4", root, 1, 0, "01 - Episode 1 [1080p]")
        second = FileRecord(root / "Season Two" / "01 - Episode 1 [1080p].mp4", root, 1, 0, "01 - Episode 1 [1080p]")
        self.assertIn("Season One", contextual_name(first))
        self.assertIn("Season Two", contextual_name(second))
        self.assertNotEqual(contextual_name(first), contextual_name(second))

    def test_generic_episode_names_in_different_seasons_do_not_create_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = [
                FileRecord(root / season / "01 - Episode 1 [1080p].mp4", root, 1, 0, "01 - Episode 1 [1080p]")
                for season in ("Main Series", "Full Moon Night R")
            ]
            with Cache(root / "cache.sqlite3") as cache:
                normalized = normalize_names(records, cache, name_provider="none")
                hints = find_name_hints(records, normalized, set(), set())
            self.assertNotEqual(
                normalized[records[0].path_key].cluster_key,
                normalized[records[1].path_key].cluster_key,
            )
            self.assertEqual(hints, [])

    def test_lmstudio_json_helpers_parse_response_content(self) -> None:
        parsed = _extract_json_object('```json\n{"results":[{"id":0,"core_title":"Cowboy Bebop","year":1998,"episode":1,"flags":["dub"]}]}\n```')
        results = _coerce_normalized_results(["Cowboy.Bebop.E01.1998.DUB"], parsed["results"], "lmstudio")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].core_title, "Cowboy Bebop")
        self.assertEqual(results[0].source, "lmstudio")
        self.assertEqual(results[0].year, 1998)

    def test_config_file_values_can_be_overridden_from_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "samesame.json"
            config_path.write_text(
                json.dumps(
                    {
                        "folders": ["from-config"],
                        "reports": {"html": "from-config.html", "json": "from-config.json"},
                        "matching": {"video": 80, "image": 88, "audio": 87, "folder": 40, "name": 85},
                        "names": {
                            "name_provider": "lmstudio",
                            "lmstudio_url": "http://localhost:1234/v1",
                            "lmstudio_model": "local-model",
                        },
                        "video": {"skip": True},
                        "images": {"skip": True, "max_candidates": 175},
                        "audio": {"skip": True},
                        "workers": 2,
                    }
                ),
                encoding="utf-8",
            )

            config = parse_args(
                [
                    "--config",
                    str(config_path),
                    "--folders",
                    "from-cli",
                    "--output",
                    "from-cli.html",
                    "--video-threshold",
                    "95",
                    "--audio-threshold",
                    "93",
                    "--name-provider",
                    "none",
                    "--no-skip-video",
                    "--no-skip-images",
                    "--no-skip-audio",
                    "--workers",
                    "6",
                ]
            )

        self.assertEqual([str(path) for path in config.folders], ["from-cli"])
        self.assertEqual(str(config.output), "from-cli.html")
        self.assertEqual(str(config.json_output), "from-config.json")
        self.assertEqual(config.video_threshold, 95)
        self.assertEqual(config.image_threshold, 88)
        self.assertEqual(config.audio_threshold, 93)
        self.assertEqual(config.folder_threshold, 40)
        self.assertEqual(config.name_provider, "none")
        self.assertFalse(config.skip_video)
        self.assertFalse(config.skip_images)
        self.assertFalse(config.skip_audio)
        self.assertEqual(config.max_image_candidates, 175)
        self.assertEqual(config.workers, 6)

    def test_inspect_cache_does_not_require_folders(self) -> None:
        config = parse_args(["--inspect-cache", "--cache", "example.sqlite3"])
        self.assertTrue(config.inspect_cache)
        self.assertEqual(config.folders, [])

    def test_cache_migrates_older_database_with_audio_fingerprint_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.sqlite3"
            connection = sqlite3.connect(cache_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE files (
                        path TEXT PRIMARY KEY,
                        size INTEGER NOT NULL,
                        mtime REAL NOT NULL,
                        partial_hash TEXT,
                        full_hash TEXT,
                        duration REAL,
                        fingerprint TEXT,
                        image_fingerprint TEXT,
                        raw_name TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with Cache(cache_path) as cache:
                columns = {
                    row["name"]
                    for row in cache.conn.execute("PRAGMA table_info(files)")
                }

            self.assertIn("audio_fingerprint", columns)
            self.assertIn("fingerprint_version", columns)

    def test_cache_ignores_legacy_video_fingerprint_without_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record = FileRecord(base / "video.mp4", base, 100, 1, "video", fingerprint=[0] * 5)
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_file(record)
                cache.conn.commit()
                hydrated = FileRecord(base / "video.mp4", base, 100, 1, "video")
                self.assertTrue(cache.hydrate_if_current(hydrated))
            self.assertIsNone(hydrated.fingerprint)

    def test_scan_deduplicates_resolved_paths_from_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            nested = base / "nested"
            nested.mkdir()
            media = nested / "episode.mkv"
            media.write_bytes(b"media")

            with Cache(base / "cache.sqlite3") as cache:
                records = scan_folders([base, nested], {".mkv"}, cache)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].path, media.resolve())
            self.assertEqual(records[0].root, base.resolve())

    def test_name_cache_is_provider_model_prompt_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with Cache(Path(tmp) / "cache.sqlite3") as cache:
                cache.upsert_name(
                    NormalizedName(raw_name="Show.Name.01", core_title="Show Name", source="lmstudio"),
                    provider="lmstudio",
                    model="local-model",
                    prompt_hash=PROMPT_HASH,
                )
                self.assertIsNotNone(
                    cache.get_name("Show.Name.01", provider="lmstudio", model="local-model", prompt_hash=PROMPT_HASH)
                )
                self.assertIsNone(
                    cache.get_name("Show.Name.01", provider="anthropic", model="claude-haiku-4-5", prompt_hash=PROMPT_HASH)
                )

    def test_exact_duplicates_and_folder_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            left = base / "left"
            right = base / "right"
            left.mkdir()
            right.mkdir()
            left_file = left / "Alpha_01.mkv"
            right_file = right / "Translated_Title_01.mkv"
            other_file = right / "Other_02.mkv"
            left_file.write_bytes(b"same content")
            right_file.write_bytes(b"same content")
            other_file.write_bytes(b"other content")

            records = []
            for path, root in ((left_file, left), (right_file, right), (other_file, right)):
                stat = path.stat()
                records.append(FileRecord(path=path, root=root, size=stat.st_size, mtime=stat.st_mtime, raw_name=path.stem))

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                exact = find_exact_duplicates(records, cache, workers=1)
                self.assertEqual(len(exact), 1)
                self.assertEqual(len(exact[0].paths), 2)
                self.assertTrue(all(record.full_hash_algo for record in records if record.size == len(b"same content")))

                normalized = {record.raw_name: fallback_normalize(record.raw_name) for record in records}
                assignments = build_cluster_assignments(records, exact, [], normalized)
                folder_pairs = compare_folders(records, assignments, threshold=50)
                self.assertEqual(len(folder_pairs), 1)
                self.assertEqual(folder_pairs[0].similarity, 50.0)
                self.assertEqual(folder_pairs[0].content_similarity, 50.0)
                self.assertEqual(folder_pairs[0].name_assisted_similarity, 50.0)

    def test_video_match_keeps_identical_fingerprints_for_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [
                FileRecord(base / "left.mp4", base, 100, 1, "left", duration=60.0, fingerprint=[0] * 5),
                FileRecord(base / "right.mkv", base, 200, 1, "right", duration=60.0, fingerprint=[0] * 5),
            ]
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_video_matches(
                    records,
                    cache,
                    threshold=90,
                    ffmpeg="unused",
                    ffprobe="unused",
                    workers=1,
                )

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].similarity, 100.0)

    def test_video_sequence_similarity_tolerates_inserted_samples(self) -> None:
        source = [index << 56 for index in range(15)]
        edited = source[:7] + [(1 << 64) - 1, (1 << 64) - 2] + source[7:]
        self.assertGreaterEqual(ordered_sequence_similarity(source, edited), 95.0)

    def test_video_duration_compatibility_rejects_compilation(self) -> None:
        self.assertTrue(video_durations_compatible(1200.0, 1475.0))
        self.assertFalse(video_durations_compatible(1200.0, 7200.0))

    def test_video_match_realigns_samples_for_small_duration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [
                FileRecord(base / "short.mp4", base, 100, 1, "short", duration=60.0, fingerprint=[0] * 5),
                FileRecord(
                    base / "extended.mkv",
                    base,
                    200,
                    1,
                    "extended",
                    duration=61.0,
                    fingerprint=[(1 << 16) - 1] * 5,
                ),
            ]
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                with patch("dedupe.video_fingerprint.fingerprint_video", return_value=[0] * 5):
                    matches = find_video_matches(
                        records,
                        cache,
                        threshold=90,
                        ffmpeg="unused",
                        ffprobe="unused",
                        workers=1,
                    )

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].similarity, 100.0)
            self.assertEqual(matches[0].duration_delta, 1.0)

    def test_large_video_bucket_blocking_keeps_near_duplicate_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [
                FileRecord(base / "left.mp4", base, 100, 1, "left", duration=60.0, fingerprint=[0] * 5),
                FileRecord(
                    base / "right.mkv",
                    base,
                    200,
                    1,
                    "right",
                    duration=60.0,
                    fingerprint=[1 << 63] * 5,
                ),
                FileRecord(
                    base / "other.webm",
                    base,
                    300,
                    1,
                    "other",
                    duration=60.0,
                    fingerprint=[(1 << 64) - 1] * 5,
                ),
            ]
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                matches = find_video_matches(
                    records,
                    cache,
                    threshold=90,
                    ffmpeg="unused",
                    ffprobe="unused",
                    workers=1,
                    max_candidates_per_bucket=2,
                )

            matched_paths = [{match.left, match.right} for match in matches]
            self.assertIn({str(base / "left.mp4"), str(base / "right.mkv")}, matched_paths)

    def test_content_similarity_counts_unconfirmed_files_in_union(self) -> None:
        left = Path("left").resolve()
        right = Path("right").resolve()
        records = [
            FileRecord(left / "shared.mp4", left, 1, 0, "shared"),
            FileRecord(right / "shared-copy.mkv", right, 1, 0, "shared-copy"),
            FileRecord(left / "left-only.mp4", left, 2, 0, "left-only"),
            FileRecord(right / "right-only-1.mp4", right, 3, 0, "right-only-1"),
            FileRecord(right / "right-only-2.mp4", right, 4, 0, "right-only-2"),
        ]
        exact = [
            ExactDuplicateGroup(
                "shared-hash",
                [str(left / "shared.mp4"), str(right / "shared-copy.mkv")],
                1,
            )
        ]
        normalized = {record.raw_name: NormalizedName(record.raw_name, record.raw_name) for record in records}
        assignments = build_cluster_assignments(records, exact, [], normalized)
        folder_pairs = compare_folders(records, assignments, threshold=0)

        self.assertEqual(len(folder_pairs), 1)
        self.assertEqual(folder_pairs[0].content_similarity, 25.0)


if __name__ == "__main__":
    unittest.main()
