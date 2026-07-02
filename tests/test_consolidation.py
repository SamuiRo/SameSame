from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dedupe.consolidation import (
    BatchStatus,
    ConsolidationPlanner,
    ConsolidationService,
    FolderMapping,
    PlanStatus,
    validate_folder_name,
    validate_relative_destination,
)


class ConsolidationPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ConsolidationPlanner()

    def test_suggestions_remove_wrappers_and_merge_base_title_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "[F0005] Amakano"
            english = root / "folder1" / "Amakano"
            translated = root / "folder2" / "Сладкая подружка Amakano (2016г.)"
            sequel = root / "folder2" / "Amakano Full Moon Night"
            for folder in (english, translated, sequel):
                folder.mkdir(parents=True)
                (folder / "episode.mp4").write_bytes(folder.name.encode("utf-8"))

            self.assertEqual(self.planner.suggest_final_name(root), "Amakano")
            mappings = self.planner.suggested_mappings(root, "Amakano")
            by_source = {mapping.source: mapping for mapping in mappings}
            self.assertEqual(by_source[english.resolve()].relative_destination, Path())
            self.assertEqual(by_source[translated.resolve()].relative_destination, Path())
            self.assertEqual(by_source[sequel.resolve()].relative_destination, Path("Amakano Full Moon Night"))

    def test_numeric_title_is_not_mistaken_for_an_episode_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "[F0002] 3 Piece"
            source = root / "folder2" / "3 Piece The Animation"
            source.mkdir(parents=True)
            (source / "01.mp4").write_bytes(b"episode")

            self.assertEqual(self.planner.suggest_final_name(root), "3 Piece")
            mappings = self.planner.suggested_mappings(root, "3 Piece")
            self.assertEqual(mappings[0].relative_destination, Path())

    def test_preview_shows_existing_and_internal_destination_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Title"
            left = root / "folder1" / "Title"
            right = root / "folder2" / "Title"
            left.mkdir(parents=True)
            right.mkdir(parents=True)
            (left / "01.mkv").write_bytes(b"left")
            (right / "01.mkv").write_bytes(b"right")
            (right / "02.mkv").write_bytes(b"two")
            (root / "02.mkv").write_bytes(b"existing")

            mappings = [
                FolderMapping(left, Path(), 1),
                FolderMapping(right, Path(), 2),
            ]
            plan = self.planner.build_plan(root, "Title", mappings)

            statuses = {move.source: move.status for move in plan.moves}
            self.assertEqual(statuses[(left / "01.mkv").resolve()], PlanStatus.CONFLICT)
            self.assertEqual(statuses[(right / "01.mkv").resolve()], PlanStatus.CONFLICT)
            self.assertEqual(statuses[(right / "02.mkv").resolve()], PlanStatus.CONFLICT)

    def test_destination_validation_rejects_escaping_and_invalid_names(self) -> None:
        with self.assertRaises(ValueError):
            validate_relative_destination("../outside")
        with self.assertRaises(ValueError):
            validate_relative_destination("C:/outside")
        with self.assertRaises(ValueError):
            validate_folder_name("bad:name")


class ConsolidationServiceTests(unittest.TestCase):
    def test_execute_is_verified_journaled_and_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "[F0001] Title"
            left = root / "folder1" / "Title"
            right = root / "folder2" / "Translated Title"
            left.mkdir(parents=True)
            right.mkdir(parents=True)
            first = left / "01.mkv"
            second = right / "02.mkv"
            first.write_bytes(b"episode one")
            second.write_bytes(b"episode two")
            planner = ConsolidationPlanner()
            plan = planner.build_plan(
                root,
                "Title",
                [FolderMapping(left, Path(), 1), FolderMapping(right, Path(), 1)],
            )
            service = ConsolidationService(base / "journal.sqlite3")

            result = service.execute(plan)

            final_root = base / "Title"
            self.assertEqual(result.status, BatchStatus.COMPLETED)
            self.assertEqual(result.moved_count, 2)
            self.assertEqual((final_root / "01.mkv").read_bytes(), b"episode one")
            self.assertEqual((final_root / "02.mkv").read_bytes(), b"episode two")
            self.assertFalse(first.exists())
            self.assertFalse(root.exists())
            latest = service.latest_undoable_batch()
            self.assertIsNotNone(latest)
            self.assertEqual(latest.batch_id, result.batch_id)  # type: ignore[union-attr]

            undone = service.undo(result.batch_id)

            self.assertEqual(undone.status, BatchStatus.UNDONE)
            self.assertEqual(first.read_bytes(), b"episode one")
            self.assertEqual(second.read_bytes(), b"episode two")
            self.assertTrue(root.is_dir())
            self.assertFalse(final_root.exists())
            self.assertIsNone(service.latest_undoable_batch())

    def test_changed_file_after_preview_fails_without_moving_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "Title"
            source = root / "folder1" / "Title"
            source.mkdir(parents=True)
            first = source / "01.mkv"
            second = source / "02.mkv"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            planner = ConsolidationPlanner()
            plan = planner.build_plan(root, "Title", [FolderMapping(source, Path(), 2)])
            second.write_bytes(b"changed after preview")
            service = ConsolidationService(base / "journal.sqlite3")

            result = service.execute(plan)

            self.assertEqual(result.status, BatchStatus.ROLLED_BACK)
            self.assertEqual(first.read_bytes(), b"one")
            self.assertEqual(second.read_bytes(), b"changed after preview")
            self.assertFalse((root / "01.mkv").exists())


if __name__ == "__main__":
    unittest.main()
