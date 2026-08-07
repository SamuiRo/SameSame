from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dedupe.actions import FileAction, FileActionService, OperationStatus
from dedupe.actions.journal import OperationJournal
from dedupe.actions.service import _retry_sharing_violation
from dedupe.models import FileRecord


class FileActionTests(unittest.TestCase):
    @patch("dedupe.actions.service.time.sleep")
    def test_windows_sharing_violation_is_retried_but_other_errors_are_not(self, sleep_mock: Mock) -> None:
        operation = Mock()
        sharing_error = OSError("in use")
        sharing_error.winerror = 32  # type: ignore[attr-defined]
        operation.side_effect = [sharing_error, sharing_error, None]

        _retry_sharing_violation(operation)

        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        denied = Mock(side_effect=PermissionError("denied"))
        with self.assertRaises(PermissionError):
            _retry_sharing_violation(denied)
        denied.assert_called_once()

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

    @patch("dedupe.actions.service.verify_content")
    def test_quarantine_verification_failure_rolls_source_back(self, verify_mock: Mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "collection" / "episode.mkv"
            source.parent.mkdir()
            source.write_bytes(b"content")
            verify_mock.side_effect = [RuntimeError("temporary read failure"), None]

            outcome = self._service(base).perform(self._record(source, base), FileAction.QUARANTINE)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            self.assertTrue(source.exists())
            self.assertIn("source restored", outcome.message)

    def test_restore_verification_failure_rolls_file_back_to_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "collection" / "episode.mkv"
            source.parent.mkdir()
            source.write_bytes(b"content")
            service = self._service(base)
            quarantined = service.perform(self._record(source, base), FileAction.QUARANTINE)
            self.assertEqual(quarantined.status, OperationStatus.COMPLETED)

            with patch("dedupe.actions.service.verify_content") as verify_mock:
                verify_mock.side_effect = [None, RuntimeError("temporary read failure"), None]
                restored = service.restore(quarantined.operation_id)

            self.assertEqual(restored.status, OperationStatus.FAILED)
            self.assertFalse(source.exists())
            self.assertTrue(quarantined.destination.exists())  # type: ignore[union-attr]
            self.assertIn("source restored", restored.message)

    def test_keeper_comparison_blocks_nonidentical_exact_batch_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            keeper = base / "keeper.mkv"
            candidate = base / "candidate.mkv"
            keeper.write_bytes(b"keeper content")
            candidate.write_bytes(b"different content")
            service = self._service(base)

            outcome = service.perform_if_matches(
                self._record(candidate, base),
                FileAction.QUARANTINE,
                self._record(keeper, base),
                group_id="exact:test",
            )

            self.assertEqual(outcome.status, OperationStatus.SKIPPED)
            self.assertTrue(candidate.exists())
            self.assertTrue(keeper.exists())
            self.assertIn("SHA-256", outcome.message)

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
            service = FileActionService(
                base / "journal.sqlite3", base / "quarantine", recycle=recycle, allow_unsafe_recycle=True
            )
            request = service.prepare(self._record(source, base), FileAction.RECYCLE)

            outcome = service.execute(request)

            recycle.assert_called_once_with(str(source.resolve()))
            self.assertEqual(outcome.status, OperationStatus.COMPLETED)
            self.assertFalse(outcome.reversible)

    def test_os_recycle_is_blocked_by_default_when_recovery_cannot_be_guaranteed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")

            outcome = self._service(base).perform(self._record(source, base), FileAction.RECYCLE)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            self.assertTrue(source.exists())
            self.assertIn("blocked by Safe mode", outcome.message)

    def test_emptying_quarantine_frees_the_files_and_ends_their_restorability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            collection = base / "collection"
            collection.mkdir()
            service = self._service(base)
            quarantined = []
            for index in range(3):
                source = collection / "season" / f"episode {index}.mkv"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"episode payload" * (index + 1))
                quarantined.append(service.perform(self._record(source, collection), FileAction.QUARANTINE))
            expected = sum(len(b"episode payload" * (index + 1)) for index in range(3))
            self.assertEqual(service.quarantine_contents(), (3, expected))

            purge = service.purge_quarantine()

            self.assertEqual(purge.removed, 3)
            self.assertEqual(purge.freed_bytes, expected)
            self.assertEqual(purge.failures, ())
            self.assertEqual(service.quarantine_contents(), (0, 0))
            for outcome in quarantined:
                self.assertFalse(outcome.destination.exists())  # type: ignore[union-attr]
                with self.assertRaises(ValueError):
                    service.restore(outcome.operation_id)
            records = {record.operation_id: record for record in service.recent_operations()}
            for outcome in quarantined:
                record = records[outcome.operation_id]
                self.assertFalse(record.reversible)
                self.assertIsNotNone(record.identity, "the pre-delete identity stays on record")
            self.assertEqual(
                [path for path in (base / "quarantine").rglob("*")],
                [],
                "emptied subfolders must not be left behind",
            )

    def test_emptying_quarantine_refuses_a_destination_outside_the_quarantine_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            library = base / "library"
            library.mkdir()
            bystander = library / "untouchable.mkv"
            bystander.write_bytes(b"a file the journal must never reach")
            service = self._service(base)
            source = library / "episode.mkv"
            source.write_bytes(b"episode payload")
            outcome = service.perform(self._record(source, library), FileAction.QUARANTINE)
            # Rewrite the journal to point at a file outside the quarantine root,
            # standing in for a stale row or a hand-edited database.
            with OperationJournal(service.journal_path) as journal:
                journal.connection.execute(
                    "UPDATE operations SET destination = ? WHERE operation_id = ?",
                    (str(bystander), outcome.operation_id),
                )
                journal.connection.commit()

            purge = service.purge_quarantine()

            self.assertEqual(purge.removed, 0)
            self.assertEqual(len(purge.failures), 1)
            self.assertIn("Refused to delete outside", purge.failures[0])
            self.assertTrue(bystander.is_file(), "a path outside quarantine must survive untouched")

    def test_permanent_delete_is_blocked_unless_it_was_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")

            outcome = self._service(base).perform(self._record(source, base), FileAction.DELETE)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            self.assertTrue(source.exists(), "a blocked delete must leave the file alone")
            self.assertIn("not explicitly enabled", outcome.message)

    def test_permanent_delete_erases_the_file_and_journals_it_as_irreversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")
            service = FileActionService(base / "journal.sqlite3", base / "quarantine", allow_permanent_delete=True)

            outcome = service.perform(self._record(source, base), FileAction.DELETE)

            self.assertEqual(outcome.status, OperationStatus.COMPLETED)
            self.assertFalse(source.exists())
            self.assertFalse(outcome.reversible)
            self.assertIsNone(outcome.destination, "a deleted file has nowhere to come back from")
            record = service.recent_operations()[0]
            self.assertEqual(record.action, FileAction.DELETE)
            self.assertIsNotNone(record.identity, "the pre-delete identity must stay on record")
            with self.assertRaises(ValueError):
                service.restore(outcome.operation_id)

    def test_permanent_delete_refuses_a_file_that_changed_since_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "episode.mkv"
            source.write_bytes(b"content")
            service = FileActionService(base / "journal.sqlite3", base / "quarantine", allow_permanent_delete=True)
            request = service.prepare(self._record(source, base), FileAction.DELETE)
            source.write_bytes(b"different content entirely")

            outcome = service.execute(request)

            self.assertEqual(outcome.status, OperationStatus.FAILED)
            self.assertTrue(source.exists(), "an unrecognised file must never be erased")


if __name__ == "__main__":
    unittest.main()
