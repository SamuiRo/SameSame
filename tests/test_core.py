from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe import __version__
from dedupe.cache import Cache
from dedupe.cli import parse_args
from dedupe.exact_hash import find_exact_duplicates
from dedupe.folder_compare import build_cluster_assignments, compare_folders
from dedupe.models import DedupeReport, ExactDuplicateGroup, FileRecord, NameHint, NormalizedName, VideoMatch
from dedupe.name_normalizer import (
    PROMPT_HASH,
    _coerce_normalized_results,
    _extract_json_object,
    _normalize_batch_with_claude,
    contextual_name,
    fallback_normalize,
    find_name_hints,
    normalize_names,
)
from dedupe.report import report_to_dict, write_html_report, write_json_report
from dedupe.scanner import scan_folders
from dedupe.video_fingerprint import (
    GRID_CADENCE,
    SAMPLE_COUNT,
    calibrate_similarity,
    find_video_matches,
    fingerprint_video,
    fingerprint_video_grid,
    grid_aligned_similarity,
    grid_sample_count,
    ordered_sequence_similarity,
    video_durations_compatible,
)


class CoreTests(unittest.TestCase):
    def test_package_version_matches_project_metadata(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        self.assertEqual(__version__, metadata["project"]["version"])

    def test_cli_rejects_unsafe_numeric_limits(self) -> None:
        invalid_arguments = (
            ["--folders", ".", "--video-threshold", "-1"],
            ["--folders", ".", "--audio-threshold", "nan"],
            ["--folders", ".", "--workers", "0"],
            ["--folders", ".", "--workers", "65"],
            ["--folders", ".", "--max-video-duration-ratio", "1"],
            ["--folders", ".", "--max-video-duration-ratio", "nan"],
            ["--folders", ".", "--max-video-duration-delta", "-1"],
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parse_args(arguments)

    def test_cli_exposes_video_duration_gate_overrides(self) -> None:
        # F-29: these were previously accepted by find_video_matches but
        # unreachable from the CLI/config.
        config = parse_args(
            ["--folders", ".", "--max-video-duration-ratio", "2.5", "--max-video-duration-delta", "120"]
        )
        self.assertEqual(config.max_video_duration_ratio, 2.5)
        self.assertEqual(config.max_video_duration_delta, 120.0)

    def test_config_file_video_section_maps_duration_gate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "samesame.json"
            config_path.write_text(
                json.dumps({"folders": ["."], "video": {"duration_ratio": 1.8, "duration_delta": 300}}),
                encoding="utf-8",
            )
            config = parse_args(["--config", str(config_path)])
            self.assertEqual(config.max_video_duration_ratio, 1.8)
            self.assertEqual(config.max_video_duration_delta, 300.0)

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

    def test_fuzzy_name_hints_only_compare_within_matching_year_and_episode(self) -> None:
        # F-22: grouping by (year, episode) before the fuzzy comparison must
        # not change which pairs are considered - only how cheaply they're
        # found. "Show Name" and "Show Nam" (same year/episode) should hint;
        # "Show Name" at a different year should not, even though the title
        # alone is identical.
        root = Path("library").resolve()
        records = [
            FileRecord(root / "a.mkv", root, 1, 0, "a"),
            FileRecord(root / "b.mkv", root, 1, 0, "b"),
            FileRecord(root / "c.mkv", root, 1, 0, "c"),
        ]
        normalized = {
            records[0].path_key: NormalizedName("a", "Show Name", year=2020, episode=1),
            records[1].path_key: NormalizedName("b", "Show Nam", year=2020, episode=1),
            records[2].path_key: NormalizedName("c", "Show Name", year=2021, episode=1),
        }
        hints = find_name_hints(records, normalized, set(), set())

        self.assertEqual(len(hints), 1)
        self.assertEqual(set(hints[0].paths), {records[0].path_key, records[1].path_key})

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
        parsed = _extract_json_object(
            '```json\n{"results":[{"id":0,"core_title":"Cowboy Bebop","year":1998,"episode":1,"flags":["dub"]}]}\n```'
        )
        results = _coerce_normalized_results(["Cowboy.Bebop.E01.1998.DUB"], parsed["results"], "lmstudio")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].core_title, "Cowboy Bebop")
        self.assertEqual(results[0].source, "lmstudio")
        self.assertEqual(results[0].year, 1998)

    def test_anthropic_provider_falls_back_cleanly_without_the_ai_extra(self) -> None:
        # F-19: anthropic moved to an optional "ai" extra. Selecting the
        # anthropic provider without it installed must degrade to local
        # heuristics with one clear warning, not fail the scan.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [FileRecord(base / "Show.Name.01.mkv", base, 1, 0, "Show.Name.01.mkv")]
            with Cache(base / "cache.sqlite3") as cache:
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                    with patch("dedupe.name_normalizer._anthropic_available", return_value=False):
                        with self.assertLogs("dedupe.name_normalizer", level="WARNING") as captured:
                            normalized = normalize_names(records, cache, name_provider="anthropic")

            self.assertTrue(any("ai" in message and "not installed" in message for message in captured.output))
            self.assertEqual(normalized[records[0].path_key].source, "fallback")

    def test_claude_batch_backfills_ids_the_model_omitted(self) -> None:
        class FakeBlock:
            type = "tool_use"
            name = "normalize_titles"
            # Only id 0 is returned; id 1 is silently dropped, as a truncated
            # tool call can do in practice (F-03).
            input = {
                "results": [
                    {"id": 0, "core_title": "Cowboy Bebop", "year": 1998, "episode": 1, "flags": ["dub"]},
                ]
            }

        class FakeResponse:
            content = [FakeBlock()]

        class FakeMessages:
            def create(self, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        class FakeAnthropic:
            def __init__(self, api_key: str) -> None:
                self.messages = FakeMessages()

        batch = ["Cowboy.Bebop.E01.1998.DUB", "Trigun.E02.1998"]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("anthropic.Anthropic", FakeAnthropic):
                results = _normalize_batch_with_claude(batch)

        by_raw = {item.raw_name: item for item in results}
        self.assertEqual(set(by_raw), set(batch))
        self.assertEqual(by_raw["Cowboy.Bebop.E01.1998.DUB"].source, "claude")
        self.assertEqual(by_raw["Cowboy.Bebop.E01.1998.DUB"].core_title, "Cowboy Bebop")
        self.assertEqual(by_raw["Trigun.E02.1998"].source, "fallback")

    def test_process_run_suppresses_console_window_and_closes_stdin(self) -> None:
        # F-05/F-09: every ffmpeg/ffprobe call site now goes through
        # dedupe.process so none of them flash a console window under a
        # windowed launcher, and none can block on an inherited stdin.
        from dedupe import process as process_module

        with patch("dedupe.process.subprocess.run") as mock_run:
            mock_run.return_value = "ok"
            result = process_module.run(["echo", "hi"], capture_output=True)

        self.assertEqual(result, "ok")
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_process_run_does_not_override_an_explicit_stdin(self) -> None:
        from dedupe import process as process_module

        with patch("dedupe.process.subprocess.run") as mock_run:
            process_module.run(["echo", "hi"], stdin=subprocess.PIPE)

        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)

    def test_db_connect_enables_wal_and_busy_timeout(self) -> None:
        from dedupe.db import connect

        with tempfile.TemporaryDirectory() as tmp:
            connection = connect(Path(tmp) / "test.sqlite3")
            try:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(busy_timeout, 5000)
        self.assertEqual(synchronous, 1)  # NORMAL

    def test_db_connect_lets_a_concurrent_writer_wait_instead_of_locking(self) -> None:
        # F-11: operations.sqlite3 is written from the GUI thread (journal
        # dialog) and a background consolidation worker thread. Without
        # busy_timeout, the second writer used to fail immediately with
        # "database is locked" instead of waiting for the first to finish.
        import threading
        import time

        from dedupe.db import connect

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            setup = connect(path)
            setup.execute("CREATE TABLE t (v INTEGER)")
            setup.commit()
            setup.close()

            first = connect(path)
            try:
                first.execute("BEGIN IMMEDIATE")
                first.execute("INSERT INTO t VALUES (1)")

                results: list[str] = []

                def write_from_second() -> None:
                    second = connect(path)  # each connection is thread-affine
                    try:
                        second.execute("INSERT INTO t VALUES (2)")
                        second.commit()
                        results.append("done")
                    finally:
                        second.close()

                thread = threading.Thread(target=write_from_second)
                thread.start()
                time.sleep(0.2)
                first.commit()
                thread.join(timeout=5)

                self.assertEqual(results, ["done"])
            finally:
                first.close()

    def test_html_and_json_reports_group_chained_matches_into_one_cluster(self) -> None:
        # A~B and B~C should render as ONE 3-file cluster, not two separate
        # pairwise cases (F-23).
        report = DedupeReport(
            scanned_files=3,
            exact_duplicates=[],
            video_matches=[
                VideoMatch("left/a.mkv", "mid/b.mkv", 90.0, 0.1),
                VideoMatch("mid/b.mkv", "right/c.mkv", 88.0, 0.2),
            ],
            image_matches=[],
            audio_matches=[],
            folder_pairs=[],
            name_hints=[],
        )
        payload = report_to_dict(report)
        self.assertEqual(len(payload["content_clusters"]), 1)
        self.assertEqual(
            sorted(payload["content_clusters"][0]["paths"]), sorted(["left/a.mkv", "mid/b.mkv", "right/c.mkv"])
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            html_path = base / "report.html"
            json_path = base / "report.json"
            write_html_report(report, html_path)
            write_json_report(report, json_path)

            html_text = html_path.read_text(encoding="utf-8")
            self.assertEqual(html_text.count("<details"), 1)
            for path in ("left/a.mkv", "mid/b.mkv", "right/c.mkv"):
                self.assertIn(path, html_text)

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["content_clusters"]), 1)
            self.assertEqual(
                sorted(data["content_clusters"][0]["paths"]), sorted(["left/a.mkv", "mid/b.mkv", "right/c.mkv"])
            )

    def test_html_report_is_english_and_filterable(self) -> None:
        # F-20: one language throughout, plus a self-contained search/filter
        # box with a live per-section result count.
        report = DedupeReport(
            scanned_files=2,
            exact_duplicates=[ExactDuplicateGroup("hash", ["a.mkv", "b.mkv"], 10)],
            video_matches=[],
            image_matches=[],
            audio_matches=[],
            folder_pairs=[],
            name_hints=[NameHint("name:key", 91.0, ["c.mkv", "d.mkv"], "Title")],
        )

        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "report.html"
            write_html_report(report, html_path)
            html_text = html_path.read_text(encoding="utf-8")

        self.assertIn('<html lang="en">', html_text)
        for ukrainian_heading in (
            "Точні дублікати файлів",
            "Схожі відео",
            "Схожі зображення",
            "Схожі аудіозаписи",
            "Змішані докази",
            "Пари тек",
            "Підказки за назвою",
        ):
            self.assertNotIn(ukrainian_heading, html_text)
        for english_heading in ("Exact duplicates", "Similar videos", "Name hints"):
            self.assertIn(english_heading, html_text)

        self.assertIn('id="filter-input"', html_text)
        self.assertIn('data-section="exact"', html_text)
        self.assertIn('data-section="name"', html_text)
        # One exact group and one name hint were supplied - each section's
        # initial (unfiltered) count must reflect that.
        self.assertIn('<h2>Exact duplicates <span class="count" data-count>1</span></h2>', html_text)
        self.assertIn('<h2>Name hints <span class="count" data-count>1</span></h2>', html_text)
        self.assertIn('<h2>Similar videos <span class="count" data-count>0</span></h2>', html_text)

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
                columns = {row["name"] for row in cache.conn.execute("PRAGMA table_info(files)")}

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

    def test_cancelling_the_grid_stage_keeps_the_grids_already_built(self) -> None:
        # A-20: every fingerprinting stage used to commit exactly once, after
        # it finished. The alignment-grid build is the longest operation the
        # product performs - 26 minutes for the real library, reading every
        # byte of it - so a cancel at minute 25 discarded all of it, while
        # docs/STATUS.md advertised that cancellation "retains already
        # committed cache work". Measured before the fix: 0 grids survived.
        from unittest.mock import patch

        from dedupe.events import CancellationToken, ScanCancelled, ScanStage
        from dedupe.progress import progress_scope
        from dedupe.video_fingerprint import find_video_matches

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache_path = base / "cache.sqlite3"
            records = [
                FileRecord(base / f"ep{index:02d}.mkv", base, 100 + index, 1, f"ep{index:02d}") for index in range(40)
            ]
            for index, record in enumerate(records):
                record.duration = 600.0 + index
                record.fingerprint = [index] * 15

            cancellation = CancellationToken()
            built = 0

            def stub_grid(path: Path, duration: float, ffmpeg: str) -> list[int]:
                nonlocal built
                built += 1
                if built == 20:
                    cancellation.cancel()
                return [1, 2, 3, 4]

            with Cache(cache_path) as cache:
                with patch("dedupe.video_fingerprint.fingerprint_video_grid", stub_grid):
                    # Zero interval so every consumed record commits; the point
                    # under test is that the stage commits at all before it ends.
                    with patch("dedupe.cache.COMMIT_INTERVAL_SECONDS", 0.0):
                        with progress_scope(
                            stage=ScanStage.VIDEO_MATCHING,
                            callback=None,
                            cancellation=cancellation,
                            show_terminal=False,
                        ):
                            with self.assertRaises(ScanCancelled):
                                find_video_matches(
                                    records, cache, threshold=20.0, ffmpeg="ffmpeg", ffprobe="ffprobe", workers=1
                                )

                # Read through a second connection: only committed rows are
                # visible, which is exactly the property being asserted.
                probe = sqlite3.connect(cache_path)
                try:
                    survived = probe.execute(
                        "SELECT COUNT(*) FROM files WHERE grid_fingerprint IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    probe.close()

            self.assertGreater(survived, 0, "a cancelled grid stage must keep the grids it already paid for")

    def test_a_failed_grid_build_is_recorded_and_not_retried(self) -> None:
        # A-20: a file whose grid cannot be built used to leave the column
        # blank, so every later scan re-attempted it - a fresh full decode
        # under a 600 s timeout, forever.
        from unittest.mock import patch

        from dedupe.models import GRID_FINGERPRINT_FAILED
        from dedupe.video_fingerprint import find_video_matches

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache_path = base / "cache.sqlite3"
            record = FileRecord(base / "broken.mkv", base, 100, 1, "broken")
            record.duration = 600.0
            record.fingerprint = [0] * 15
            attempts = 0

            def failing_grid(path: Path, duration: float, ffmpeg: str) -> None:
                nonlocal attempts
                attempts += 1
                return None

            with Cache(cache_path) as cache:
                with patch("dedupe.video_fingerprint.fingerprint_video_grid", failing_grid):
                    find_video_matches([record], cache, threshold=20.0, ffmpeg="ffmpeg", ffprobe="ffprobe", workers=1)
                    self.assertEqual(attempts, 1)
                    self.assertEqual(record.grid_fingerprint_version, GRID_FINGERPRINT_FAILED)

                    # The marker is persisted, so a fresh scan of an unchanged
                    # file does not pay for the decode again.
                    hydrated = FileRecord(base / "broken.mkv", base, 100, 1, "broken")
                    self.assertTrue(cache.hydrate_if_current(hydrated))
                    self.assertEqual(hydrated.grid_fingerprint_version, GRID_FINGERPRINT_FAILED)
                    self.assertIsNone(hydrated.grid_fingerprint)

                    find_video_matches([hydrated], cache, threshold=20.0, ffmpeg="ffmpeg", ffprobe="ffprobe", workers=1)
                    self.assertEqual(attempts, 1, "a recorded grid failure must not be retried")

    def test_config_rejects_a_threshold_set_twice_with_different_values(self) -> None:
        # A-21: `matching.audio` and `audio.threshold` are two spellings of one
        # setting, and _flatten_config resolved them by document order. That is
        # how docs/CONFIG.md's own example shipped audio_threshold=88 under a
        # `matching` block that said 60 - the value A-14 measured as missing 16
        # of 20 confirmed duplicate pairs.
        from dedupe.cli import _flatten_config

        with self.assertRaises(SystemExit) as raised:
            _flatten_config({"matching": {"audio": 60}, "audio": {"threshold": 88}})
        self.assertIn("audio_threshold", str(raised.exception))

        # Agreeing duplicates are not an error - only a disagreement is.
        self.assertEqual(
            _flatten_config({"matching": {"audio": 60}, "audio": {"threshold": 60}})["audio_threshold"], 60
        )

    def test_user_facing_docs_do_not_quote_superseded_thresholds(self) -> None:
        # A-25: the same drift as A-21, but in prose rather than JSON.
        # docs/GUI_REFERENCE.md advertised "video 15, image 80, audio 88" long
        # after all three were re-derived, and docs/THRESHOLDS.md carried a
        # copy-pasteable benchmark command that passed them explicitly.
        #
        # Every superseded default is a number a user might paste into a
        # config and then wonder why matching went quiet, so they are banned
        # from the docs people are meant to copy from. The audit files are
        # deliberately excluded: their whole job is to record what the old
        # numbers were and why they moved.
        from dedupe.cli import DEFAULT_CONFIG

        superseded = {
            "15": "video, pre-A-02",
            "80": "image, pre-A-06",
            "85": "video, pre-F-24",
            "88": "audio, pre-A-14",
            "90": "image, pre-F-24",
            "94": "audio, pre-F-24",
        }
        current = {str(int(DEFAULT_CONFIG[f"{medium}_threshold"])) for medium in ("video", "image", "audio")}
        superseded = {value: why for value, why in superseded.items() if value not in current}

        docs = Path(__file__).resolve().parents[1]
        pages = [docs / "README.md", *(docs / "docs" / name for name in ("CONFIG.md", "USAGE.md", "GUI_REFERENCE.md"))]
        offenders = []
        for page in pages:
            for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
                if "threshold" not in line.lower():
                    continue
                for value, why in superseded.items():
                    if re.search(rf"`{value}`", line):
                        offenders.append(f"{page.name}:{number} quotes `{value}` ({why}) on a threshold line")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_documented_config_examples_use_the_shipped_thresholds(self) -> None:
        # The prose example in docs/CONFIG.md is copied by users; it drifted
        # from the shipped defaults once already (A-21). Parse every JSON block
        # in the documentation and check what it actually resolves to.
        from dedupe.cli import DEFAULT_CONFIG, _flatten_config

        text = (Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        self.assertTrue(blocks, "expected JSON examples in docs/CONFIG.md")
        checked = 0
        for index, block in enumerate(blocks):
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            flattened = _flatten_config(parsed)
            for key in ("video_threshold", "image_threshold", "audio_threshold"):
                if key in flattened:
                    checked += 1
                    self.assertEqual(
                        float(flattened[key]),
                        float(DEFAULT_CONFIG[key]),
                        f"docs/CONFIG.md JSON block {index} resolves {key} to {flattened[key]}, "
                        f"not the shipped default {DEFAULT_CONFIG[key]}",
                    )
        self.assertGreater(checked, 0)

    def test_cache_vacuum_removes_rows_for_missing_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            present = base / "present.mp4"
            present.write_bytes(b"still here")
            missing = base / "missing.mp4"
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_file(FileRecord(present, base, present.stat().st_size, present.stat().st_mtime, "present"))
                cache.upsert_file(FileRecord(missing, base, 1, 0, "missing"))
                cache.conn.commit()

                removed = cache.vacuum()

                self.assertEqual(removed, 1)
                remaining_paths = {row["path"] for row in cache.conn.execute("SELECT path FROM files")}
                self.assertEqual(remaining_paths, {str(present)})

    def test_cli_vacuum_cache_does_not_require_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.sqlite3"
            config = parse_args(["--vacuum-cache", "--cache", str(cache_path)])
            self.assertTrue(config.vacuum_cache)

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
                    cache.get_name(
                        "Show.Name.01", provider="anthropic", model="claude-haiku-4-5", prompt_hash=PROMPT_HASH
                    )
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
                records.append(
                    FileRecord(path=path, root=root, size=stat.st_size, mtime=stat.st_mtime, raw_name=path.stem)
                )

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                exact = find_exact_duplicates(records, cache, workers=1)
                self.assertEqual(len(exact), 1)
                self.assertEqual(len(exact[0].paths), 2)
                self.assertTrue(all(record.full_hash_algo for record in records if record.size == len(b"same content")))

                normalized = {record.raw_name: fallback_normalize(record.raw_name) for record in records}
                assignments, _clusters = build_cluster_assignments(records, exact, [], normalized)
                folder_pairs = compare_folders(records, assignments, threshold=50)
                self.assertEqual(len(folder_pairs), 1)
                self.assertEqual(folder_pairs[0].similarity, 50.0)
                self.assertEqual(folder_pairs[0].content_similarity, 50.0)
                self.assertEqual(folder_pairs[0].name_assisted_similarity, 50.0)

    def test_folder_comparison_finds_subfolders_under_a_single_root(self) -> None:
        # F-16: folders used to be keyed by the collection root, so scanning
        # one root - the common case - always produced zero folder pairs no
        # matter how many duplicate subfolders existed underneath it. The
        # default key is now each file's containing directory.
        root = Path("collection").resolve()
        season = root / "Show" / "Season 1"
        season_copy = root / "Show" / "Season 1 (copy)"
        records = [
            FileRecord(season / "ep01.mp4", root, 1, 0, "ep01"),
            FileRecord(season_copy / "ep01.mp4", root, 1, 0, "ep01"),
        ]
        exact = [ExactDuplicateGroup("hash", [str(season / "ep01.mp4"), str(season_copy / "ep01.mp4")], 1)]
        normalized = {record.raw_name: NormalizedName(record.raw_name, record.raw_name) for record in records}
        assignments, _clusters = build_cluster_assignments(records, exact, [], normalized)

        folder_pairs = compare_folders(records, assignments, threshold=0)

        self.assertEqual(len(folder_pairs), 1)
        self.assertEqual(folder_pairs[0].scope, "directory")
        self.assertEqual({folder_pairs[0].left, folder_pairs[0].right}, {str(season), str(season_copy)})
        self.assertEqual(folder_pairs[0].content_similarity, 100.0)

    def test_folder_comparison_root_rollup_still_available(self) -> None:
        # F-16: the old collection-root comparison is kept, opt-in, as a
        # coarser roll-up alongside the new directory-level default.
        left_root = Path("left").resolve()
        right_root = Path("right").resolve()
        records = [
            FileRecord(left_root / "ep01.mp4", left_root, 1, 0, "ep01"),
            FileRecord(right_root / "ep01.mp4", right_root, 1, 0, "ep01"),
        ]
        exact = [ExactDuplicateGroup("hash", [str(left_root / "ep01.mp4"), str(right_root / "ep01.mp4")], 1)]
        normalized = {record.raw_name: NormalizedName(record.raw_name, record.raw_name) for record in records}
        assignments, _clusters = build_cluster_assignments(records, exact, [], normalized)

        root_pairs = compare_folders(
            records, assignments, threshold=0, key=lambda record: record.root_key, scope="root"
        )

        self.assertEqual(len(root_pairs), 1)
        self.assertEqual(root_pairs[0].scope, "root")
        self.assertEqual({root_pairs[0].left, root_pairs[0].right}, {str(left_root), str(right_root)})

    def test_folder_comparison_skips_directories_with_no_shared_cluster(self) -> None:
        # F-16: comparing every directory pair is quadratic in directory
        # count, so unrelated directories must never become a candidate pair
        # - not just be filtered out after the fact by the threshold.
        root = Path("collection").resolve()
        matched_a = root / "A"
        matched_b = root / "A copy"
        unrelated = root / "Unrelated"
        records = [
            FileRecord(matched_a / "ep01.mp4", root, 1, 0, "ep01"),
            FileRecord(matched_b / "ep01.mp4", root, 1, 0, "ep01"),
            FileRecord(unrelated / "other.mp4", root, 2, 0, "other"),
        ]
        exact = [ExactDuplicateGroup("hash", [str(matched_a / "ep01.mp4"), str(matched_b / "ep01.mp4")], 1)]
        normalized = {record.raw_name: NormalizedName(record.raw_name, record.raw_name) for record in records}
        assignments, _clusters = build_cluster_assignments(records, exact, [], normalized)

        # threshold=0 would let a genuine 0%-overlap pair through if one were
        # ever generated, so this only passes if the prefilter itself never
        # produces "Unrelated" as a candidate.
        folder_pairs = compare_folders(records, assignments, threshold=0)

        self.assertEqual(len(folder_pairs), 1)
        self.assertEqual({folder_pairs[0].left, folder_pairs[0].right}, {str(matched_a), str(matched_b)})

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

    def test_grid_cadence_resolves_a_few_seconds_of_offset(self) -> None:
        # A-13: an integer-slot shift can only cancel offsets that are
        # multiples of the cadence, and two releases of one episode routinely
        # sit a handful of seconds apart. A coarse grid cannot see that at all.
        self.assertLessEqual(GRID_CADENCE, 4.0)
        self.assertEqual(grid_sample_count(1703.0), math.ceil(1703.0 / GRID_CADENCE))

    def test_grid_alignment_recovers_a_small_offset(self) -> None:
        # A-13's regression: a 6-second offset between two releases of the
        # same episode used to score as noise because it fell inside one slot.
        content = [index * 0x0101010101010101 for index in range(1, 120)]
        offset_slots = round(6.0 / GRID_CADENCE)
        shifted = [0xFFFFFFFFFFFFFFFF] * offset_slots + content
        self.assertGreaterEqual(grid_aligned_similarity(content, shifted), 99.0)

    def test_grid_alignment_recovers_a_shifted_duplicate(self) -> None:
        # A-01: the same content offset in time must align. `right` is `left`
        # shifted three slots later, with different content in the gap.
        content = [index * 0x0101010101010101 for index in range(1, 40)]
        right = [0xFFFFFFFFFFFFFFFF, 0xF0F0F0F0F0F0F0F0, 0x0F0F0F0F0F0F0F0F, *content]
        self.assertGreaterEqual(grid_aligned_similarity(content, right), 99.0)

    def test_grid_alignment_ignores_missing_samples_without_shifting(self) -> None:
        # A-01: a failed frame is a placeholder, not a gap - dropping it would
        # move every later sample off the absolute grid.
        content = [index * 0x0101010101010101 for index in range(1, 40)]
        damaged = list(content)
        damaged[5] = -1
        damaged[17] = -1
        self.assertGreaterEqual(grid_aligned_similarity(content, damaged), 99.0)

    def test_grid_alignment_does_not_invent_a_match(self) -> None:
        # The search maximises over many shifts, so it must not lift unrelated
        # content: with a 2s cadence there are ~180 candidate shifts to pick
        # the luckiest one from.
        import random

        random.seed(17)
        left = [random.getrandbits(64) for _ in range(120)]
        right = [random.getrandbits(64) for _ in range(120)]
        self.assertLessEqual(calibrate_similarity(grid_aligned_similarity(left, right)), 10.0)

    def test_grid_fingerprint_rejects_a_truncated_decode(self) -> None:
        # A-13: the grid is one decode pass now, so the failure mode is a
        # short read rather than individual missing frames.
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"\x00" * (32 * 32 * 3))
        with patch("dedupe.video_fingerprint.process.run", return_value=completed):
            self.assertIsNone(fingerprint_video_grid(Path("truncated.mkv"), 600.0, "ffmpeg"))

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
                # A-01: alignment reads a cached absolute-time grid instead of
                # re-fingerprinting the longer file per candidate offset.
                with patch("dedupe.video_fingerprint.fingerprint_video_grid", return_value=[0] * 60):
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

    def test_alignment_recheck_still_fires_at_a_low_threshold(self) -> None:
        # Regression for F-25: a score floor combined with a `< threshold`
        # upper bound made the recheck unreachable whenever threshold was low -
        # exactly the range users fall back to after a real duplicate goes
        # missing. A-01 removed the floor entirely (alignment is now cheap
        # enough to always run), so this must hold at any threshold.
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
                with patch("dedupe.video_fingerprint.fingerprint_video_grid", return_value=[0] * 60):
                    matches = find_video_matches(
                        records,
                        cache,
                        threshold=50,
                        ffmpeg="unused",
                        ffprobe="unused",
                        workers=1,
                    )

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].similarity, 100.0)

    def test_video_fingerprint_tolerates_one_missing_frame(self) -> None:
        # F-26: one failed sample (index 0 -> timestamp 6.0s for a 96s file, and
        # its retry at 6.5s) must not discard the other 14 good frames.
        from PIL import Image

        good_frame = Image.new("L", (4, 4), 128)

        def fake_extract(path: Path, timestamp: float, ffmpeg: str) -> Image.Image | None:
            return None if timestamp in (6.0, 6.5) else good_frame

        with patch("dedupe.video_fingerprint._extract_frame", side_effect=fake_extract):
            fingerprint = fingerprint_video(Path("video.mkv"), 96.0, "ffmpeg")

        self.assertIsNotNone(fingerprint)
        self.assertEqual(len(fingerprint), SAMPLE_COUNT - 1)

    def test_video_fingerprint_retries_a_failed_frame_once(self) -> None:
        from PIL import Image

        good_frame = Image.new("L", (4, 4), 128)

        def fake_extract(path: Path, timestamp: float, ffmpeg: str) -> Image.Image | None:
            return None if timestamp == 6.0 else good_frame  # only the first attempt fails

        with patch("dedupe.video_fingerprint._extract_frame", side_effect=fake_extract):
            fingerprint = fingerprint_video(Path("video.mkv"), 96.0, "ffmpeg")

        self.assertIsNotNone(fingerprint)
        self.assertEqual(len(fingerprint), SAMPLE_COUNT)

    def test_video_fingerprint_gives_up_below_minimum_coverage(self) -> None:
        with patch("dedupe.video_fingerprint._extract_frame", return_value=None):
            fingerprint = fingerprint_video(Path("video.mkv"), 96.0, "ffmpeg")

        self.assertIsNone(fingerprint)

    def test_video_matching_logs_a_summary_when_files_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [FileRecord(base / "broken.mp4", base, 100, 1, "broken", duration=60.0)]
            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                with patch("dedupe.video_fingerprint.fingerprint_video", return_value=None):
                    with self.assertLogs("dedupe.video_fingerprint", level="WARNING") as captured:
                        matches = find_video_matches(
                            records, cache, threshold=90, ffmpeg="unused", ffprobe="unused", workers=1
                        )

            self.assertEqual(matches, [])
            self.assertTrue(any("excluded from video matching" in message for message in captured.output))

    def test_video_matching_evaluates_each_compatible_pair_exactly_once(self) -> None:
        # F-01: the old bucket-window approach regenerated the same pair from
        # every duration bucket it fell in (measured up to 238x waste at
        # 1000 files). The sorted-sweep replacement must evaluate each
        # duration-compatible pair exactly once - no more, no less.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = [
                FileRecord(
                    base / f"video-{index}.mp4",
                    base,
                    100,
                    1,
                    f"video-{index}",
                    duration=60.0 + index,
                    fingerprint=[index << 8],
                )
                for index in range(100)
            ]
            expected_pairs = sum(
                1
                for i in range(len(records))
                for j in range(i + 1, len(records))
                if video_durations_compatible(records[i].duration, records[j].duration)  # type: ignore[arg-type]
            )
            self.assertGreater(expected_pairs, 0)

            with Cache(base / "cache.sqlite3") as cache:
                cache.upsert_files(records)
                # These records point at paths that do not exist; this test is
                # about pair generation, so skip grid building entirely rather
                # than let 100 files probe a non-existent ffmpeg.
                with (
                    patch("dedupe.video_fingerprint.fingerprint_video_grid", return_value=None),
                    patch("dedupe.video_fingerprint.ordered_sequence_similarity", return_value=0.0) as mock_similarity,
                ):
                    find_video_matches(records, cache, threshold=200, ffmpeg="unused", ffprobe="unused", workers=1)

            self.assertEqual(mock_similarity.call_count, expected_pairs)

    def test_large_video_group_keeps_all_near_duplicate_candidates(self) -> None:
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
        assignments, _clusters = build_cluster_assignments(records, exact, [], normalized)
        folder_pairs = compare_folders(records, assignments, threshold=0)

        self.assertEqual(len(folder_pairs), 1)
        self.assertEqual(folder_pairs[0].content_similarity, 25.0)


if __name__ == "__main__":
    unittest.main()
