from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe.actions import FileActionService, OperationStatus
from dedupe.transcode.models import JobStatus, TranscodeResult, ValidationResult
from dedupe.transcode.promotion import promote_transcode


def completed_result(source: Path, output: Path) -> TranscodeResult:
    return TranscodeResult(
        job_id="job",
        status=JobStatus.COMPLETED,
        input_path=source,
        output_path=output,
        log_path=None,
        preset_id="anime_x265_balanced",
        input_size=source.stat().st_size,
        output_size=output.stat().st_size,
        validation=ValidationResult(True),
        output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        input_modified_at=source.stat().st_mtime,
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )


class TranscodePromotionTests(unittest.TestCase):
    def test_mkv_source_is_quarantined_before_validated_output_is_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mkv"
            output = root / "episode.encoded.mkv"
            source.write_bytes(b"original-media")
            output.write_bytes(b"validated-encoded-media")
            journal = root / "journal.sqlite3"
            quarantine = root / "quarantine"

            promoted = promote_transcode(
                completed_result(source, output),
                journal_path=journal,
                quarantine_root=quarantine,
                collection_root=root,
            )

            self.assertTrue(promoted.success, promoted.message)
            self.assertEqual(source.read_bytes(), b"validated-encoded-media")
            self.assertFalse(output.exists())
            records = FileActionService(journal, quarantine).recent_operations()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, OperationStatus.COMPLETED)
            self.assertEqual(records[0].destination.read_bytes(), b"original-media")  # type: ignore[union-attr]

    def test_non_mkv_target_conflict_fails_without_quarantining_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mp4"
            output = root / "episode.encoded.mkv"
            conflict = root / "episode.mkv"
            source.write_bytes(b"original")
            output.write_bytes(b"encoded")
            conflict.write_bytes(b"existing")

            promoted = promote_transcode(
                completed_result(source, output),
                journal_path=root / "journal.sqlite3",
                quarantine_root=root / "quarantine",
            )

            self.assertFalse(promoted.success)
            self.assertTrue(source.exists())
            self.assertEqual(conflict.read_bytes(), b"existing")
            self.assertFalse((root / "journal.sqlite3").exists())

    def test_failed_promotion_restores_quarantined_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mkv"
            output = root / "episode.encoded.mkv"
            source.write_bytes(b"original")
            output.write_bytes(b"encoded")
            journal = root / "journal.sqlite3"
            quarantine = root / "quarantine"

            with patch("dedupe.transcode.promotion._move_encoded", side_effect=OSError("simulated promotion failure")):
                promoted = promote_transcode(
                    completed_result(source, output),
                    journal_path=journal,
                    quarantine_root=quarantine,
                    collection_root=root,
                )

            self.assertFalse(promoted.success)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(output.read_bytes(), b"encoded")
            records = FileActionService(journal, quarantine).recent_operations()
            self.assertEqual(len(records), 2)
            self.assertTrue(all(record.status == OperationStatus.COMPLETED for record in records))

    def test_changed_encoded_output_is_rejected_before_source_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mkv"
            output = root / "episode.encoded.mkv"
            source.write_bytes(b"original")
            output.write_bytes(b"validated")
            result = completed_result(source, output)
            output.write_bytes(b"tampered!")

            promoted = promote_transcode(
                result,
                journal_path=root / "journal.sqlite3",
                quarantine_root=root / "quarantine",
            )

            self.assertFalse(promoted.success)
            self.assertIn("changed after validation", promoted.message)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertFalse((root / "journal.sqlite3").exists())

    def test_changed_source_is_rejected_before_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mkv"
            output = root / "episode.encoded.mkv"
            source.write_bytes(b"original")
            output.write_bytes(b"validated")
            result = completed_result(source, output)
            source.write_bytes(b"modified")

            promoted = promote_transcode(
                result,
                journal_path=root / "journal.sqlite3",
                quarantine_root=root / "quarantine",
            )

            self.assertFalse(promoted.success)
            self.assertIn("Source changed", promoted.message)
            self.assertEqual(source.read_bytes(), b"modified")
            self.assertFalse((root / "journal.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
