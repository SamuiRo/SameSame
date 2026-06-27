from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from dedupe.actions import FileAction, FileActionService, OperationStatus
from dedupe.actions.journal import OperationJournal
from dedupe.models import FileRecord


class FileActionTests(unittest.TestCase):
    def _record(self, path: Path, root: Path) -> FileRecord:
        stat = path.stat()
        return FileRecord(path.resolve(), root.resolve(), stat.st_size, stat.st_mtime, path.stem)

    def _service(self, base: Path) -> FileActionService:
        return FileActionService(base / "journal.sqlite3", base / "quarantine")

    def test_quarantine_is_journaled_and_restorable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            collection = base / "collection"
            source = collection / "season" / "episode.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"episode data")
            service = self._service(base)

            request = service.prepare(self._record(source, collection), FileAction.QUARANTINE, group_id="exact:1")
            outcome = service.execute(request)

            self.assertEqual(outcome.status, OperationStatus.COMPLETED)
            self.assertTrue(outcome.reversible)
            self.assertFalse(source.exists())
            self.assertIsNotNone(outcome.destination)
            self.assertTrue(outcome.destination and outcome.destination.exists())
            with OperationJournal(service.journal_path) as journal:
                recorded = journal.get(outcome.operation_id)
            self.assertIsNotNone(recorded)
            self.assertEqual(recorded.status, OperationStatus.COMPLETED)  # type: ignore[union-attr]
            self.assertEqual(recorded.group_id, "exact:1")  # type: ignore[union-attr]

            restored = service.restore(outcome.operation_id)

            self.assertEqual(restored.status, OperationStatus.COMPLETED)
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"episode data")
            self.assertFalse(outcome.destination and outcome.destination.exists())

    def test_changed_file_fails_preflight_without_moving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"original")
            service = self._service(base)
            request = service.prepare(self._record(source, base), FileAction.QUARANTINE)
            source.write_bytes(b"changed content")

            outcome = service.execute(request)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            self.assertTrue(source.exists())
            self.assertIn("changed", outcome.message.casefold())

    def test_scan_to_preflight_failure_is_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"original")
            service = self._service(base)
            record = self._record(source, base)
            source.write_bytes(b"changed since scan")

            outcome = service.perform(record, FileAction.QUARANTINE)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            operations = service.recent_operations()
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0].status, OperationStatus.FAILED)
            self.assertTrue(source.exists())

    def test_quarantine_collision_allocates_a_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            collection = base / "collection"
            collection.mkdir()
            source = collection / "episode.mkv"
            service = self._service(base)

            source.write_bytes(b"first")
            first = service.execute(service.prepare(self._record(source, collection), FileAction.QUARANTINE))
            source.write_bytes(b"second")
            second = service.execute(service.prepare(self._record(source, collection), FileAction.QUARANTINE))

            self.assertEqual(first.status, OperationStatus.COMPLETED)
            self.assertEqual(second.status, OperationStatus.COMPLETED)
            self.assertNotEqual(first.destination, second.destination)
            self.assertEqual(second.destination.name, "episode (1).mkv")  # type: ignore[union-attr]

    def test_keep_and_ignore_only_record_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")
            record = self._record(source, base)
            service = self._service(base)

            keep = service.execute(service.prepare(record, FileAction.KEEP, group_id="video:1"))
            ignore = service.execute(service.prepare(record, FileAction.IGNORE, group_id="video:1"))

            self.assertEqual(keep.status, OperationStatus.COMPLETED)
            self.assertEqual(ignore.status, OperationStatus.COMPLETED)
            self.assertTrue(source.exists())
            self.assertEqual(len(service.recent_operations()), 2)

    def test_skipped_operation_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")
            record = self._record(source, base)
            service = self._service(base)

            outcome = service.record_skipped(record, FileAction.QUARANTINE, "Duplicate batch source")

            self.assertEqual(outcome.status, OperationStatus.SKIPPED)
            self.assertEqual(service.recent_operations()[0].status, OperationStatus.SKIPPED)

    def test_recycle_uses_os_recycle_integration_after_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")
            recycle = Mock()
            recycle.side_effect = lambda path: Path(path).unlink()
            service = FileActionService(base / "journal.sqlite3", base / "quarantine", recycle=recycle)
            request = service.prepare(self._record(source, base), FileAction.RECYCLE)

            outcome = service.execute(request)

            recycle.assert_called_once_with(str(source.resolve()))
            self.assertEqual(outcome.status, OperationStatus.COMPLETED)
            self.assertFalse(outcome.reversible)


if __name__ == "__main__":
    unittest.main()
