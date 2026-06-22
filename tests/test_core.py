from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dedupe.cache import Cache
from dedupe.cli import parse_args
from dedupe.exact_hash import find_exact_duplicates
from dedupe.folder_compare import build_cluster_assignments, compare_folders
from dedupe.models import FileRecord, NormalizedName
from dedupe.name_normalizer import PROMPT_HASH, _coerce_normalized_results, _extract_json_object, fallback_normalize


class CoreTests(unittest.TestCase):
    def test_fallback_normalize_removes_common_noise(self) -> None:
        result = fallback_normalize("[Group] Show_Name.01.1080p.x264.SUB")
        self.assertEqual(result.core_title, "Show Name")
        self.assertEqual(result.episode, 1)
        self.assertIn("sub", result.flags)

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
                        "matching": {"video": 80, "folder": 40, "name": 85},
                        "names": {
                            "name_provider": "lmstudio",
                            "lmstudio_url": "http://localhost:1234/v1",
                            "lmstudio_model": "local-model",
                        },
                        "video": {"skip": True},
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
                    "--name-provider",
                    "none",
                    "--no-skip-video",
                    "--workers",
                    "6",
                ]
            )

        self.assertEqual([str(path) for path in config.folders], ["from-cli"])
        self.assertEqual(str(config.output), "from-cli.html")
        self.assertEqual(str(config.json_output), "from-config.json")
        self.assertEqual(config.video_threshold, 95)
        self.assertEqual(config.folder_threshold, 40)
        self.assertEqual(config.name_provider, "none")
        self.assertFalse(config.skip_video)
        self.assertEqual(config.workers, 6)

    def test_inspect_cache_does_not_require_folders(self) -> None:
        config = parse_args(["--inspect-cache", "--cache", "example.sqlite3"])
        self.assertTrue(config.inspect_cache)
        self.assertEqual(config.folders, [])

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
                self.assertEqual(folder_pairs[0].similarity, 100.0)
                self.assertEqual(folder_pairs[0].content_similarity, 100.0)
                self.assertEqual(folder_pairs[0].name_assisted_similarity, 50.0)


if __name__ == "__main__":
    unittest.main()
