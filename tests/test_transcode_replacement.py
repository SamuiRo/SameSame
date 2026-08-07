from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dedupe.actions import FileAction, FileActionService
from dedupe.transcode.models import JobStatus, MediaInfo, StreamInfo, TranscodeResult, ValidationResult
from dedupe.transcode.replacement import replace_source_with_transcode


def media(path: Path, *, size: int, duration: float = 60.0) -> MediaInfo:
    return MediaInfo(
        path=path,
        size=size,
        duration=duration,
        format_name="matroska",
        streams=(StreamInfo(0, "video", "hevc"), StreamInfo(1, "audio", "aac")),
    )


class ReplacementTests(unittest.TestCase):
    @staticmethod
    def _library(root: Path) -> Path:
        """Keep the journal and quarantine out of the folder under inspection."""
        library = root / "library"
        library.mkdir(exist_ok=True)
        return library

    def _scenario(self, root: Path, source_name: str) -> TranscodeResult:
        """A completed job whose encode is staged beside a larger source."""
        source = self._library(root) / source_name
        encoded = source.with_name(f"{Path(source_name).stem}.samesame-new.mkv")
        log = encoded.with_suffix(encoded.suffix + ".ffmpeg.log")
        source.write_bytes(b"the original video, which is longer" * 10)
        encoded.write_bytes(b"the encoded video")
        log.write_text("ffmpeg diagnostics", encoding="utf-8")
        source_stat = source.stat()
        encoded_stat = encoded.stat()
        return TranscodeResult(
            job_id="job",
            status=JobStatus.COMPLETED,
            input_path=source,
            output_path=encoded,
            log_path=log,
            preset_id="anime_x265_balanced",
            input_size=source_stat.st_size,
            output_size=encoded_stat.st_size,
            validation=ValidationResult(True, output_info=media(encoded, size=encoded_stat.st_size)),
            input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            input_modified_at=source_stat.st_mtime,
            output_sha256=hashlib.sha256(encoded.read_bytes()).hexdigest(),
            output_modified_at=encoded_stat.st_mtime,
        )

    def _arguments(self, root: Path) -> dict[str, object]:
        return {
            "journal_path": root / "journal.sqlite3",
            "quarantine_root": root / "quarantine",
            "collection_root": self._library(root),
        }

    def test_an_mkv_source_is_replaced_by_its_encode_under_the_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._scenario(root, "Episode 01.mkv")
            expected = result.output_path.read_bytes()

            outcome = replace_source_with_transcode(
                result, cleanup_action=FileAction.QUARANTINE, **self._arguments(root)
            )

            self.assertTrue(outcome.success, outcome.message)
            self.assertEqual(outcome.target_path, self._library(root) / "Episode 01.mkv")
            self.assertEqual(outcome.target_path.read_bytes(), expected, "the encode must occupy the source's path")
            self.assertFalse(result.output_path.exists(), "no staging file may survive")
            self.assertEqual(sorted(p.name for p in self._library(root).glob("*.mkv")), ["Episode 01.mkv"])
            self.assertIsNotNone(outcome.cleanup)
            self.assertTrue(outcome.cleanup.destination.exists())  # type: ignore[union-attr]
            # The library must end up looking untouched apart from the size: no
            # staging leftovers, and no log named after a file that is now gone.
            self.assertEqual(
                sorted(p.name for p in self._library(root).iterdir() if p.is_file()),
                ["Episode 01.mkv"],
                "a replaced file must leave nothing beside it",
            )

    def test_a_non_mkv_source_keeps_its_name_and_gains_the_mkv_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._scenario(root, "Episode 01.avi")

            outcome = replace_source_with_transcode(
                result, cleanup_action=FileAction.DELETE, allow_permanent_delete=True, **self._arguments(root)
            )

            self.assertTrue(outcome.success, outcome.message)
            self.assertEqual(outcome.target_path, self._library(root) / "Episode 01.mkv")
            self.assertTrue(outcome.target_path.is_file())
            self.assertFalse((self._library(root) / "Episode 01.avi").exists(), "the source must be gone")
            self.assertFalse(result.output_path.exists())

    def test_a_blocked_cleanup_leaves_both_files_and_never_claims_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._scenario(root, "Episode 01.mkv")
            source_bytes = result.input_path.read_bytes()

            # Permanent delete without the opt-in fails inside the action service.
            outcome = replace_source_with_transcode(result, cleanup_action=FileAction.DELETE, **self._arguments(root))

            self.assertFalse(outcome.success)
            self.assertFalse(outcome.needs_attention)
            self.assertEqual(result.input_path.read_bytes(), source_bytes, "the source must be untouched")
            self.assertTrue(result.output_path.is_file(), "the encode must stay where it was staged")

    def test_a_failed_move_after_quarantine_restores_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._scenario(root, "Episode 01.mkv")
            source_bytes = result.input_path.read_bytes()

            with patch("dedupe.transcode.replacement._move_encoded", side_effect=OSError("disk went away")):
                outcome = replace_source_with_transcode(
                    result, cleanup_action=FileAction.QUARANTINE, **self._arguments(root)
                )

            self.assertFalse(outcome.success)
            self.assertFalse(outcome.needs_attention, "a restored original is not a situation needing attention")
            self.assertIn("restored", outcome.message)
            self.assertTrue(result.input_path.is_file(), "the original must come back")
            self.assertEqual(result.input_path.read_bytes(), source_bytes)
            self.assertTrue(result.output_path.is_file(), "the validated encode must still exist")
            self.assertTrue(
                result.log_path and result.log_path.is_file(),
                "a failed replacement keeps its diagnostics",
            )

    def test_a_failed_move_after_permanent_delete_is_flagged_for_attention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._scenario(root, "Episode 01.mkv")

            with patch("dedupe.transcode.replacement._move_encoded", side_effect=OSError("disk went away")):
                outcome = replace_source_with_transcode(
                    result,
                    cleanup_action=FileAction.DELETE,
                    allow_permanent_delete=True,
                    **self._arguments(root),
                )

            self.assertFalse(outcome.success)
            self.assertTrue(outcome.needs_attention, "an unrecoverable source loss must be surfaced")
            self.assertIn(str(result.output_path), outcome.message)
            self.assertTrue(result.output_path.is_file(), "the only surviving copy must be named in the message")

    def test_only_a_completed_validated_job_can_replace_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = self._scenario(root, "Episode 01.mkv")
            for status in (JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.CANCELLED):
                with self.subTest(status=status):
                    outcome = replace_source_with_transcode(
                        replace(completed, status=status),
                        cleanup_action=FileAction.QUARANTINE,
                        **self._arguments(root),
                    )
                    self.assertFalse(outcome.success)
                    self.assertTrue(completed.input_path.is_file())
            self.assertEqual(
                FileActionService(root / "journal.sqlite3", root / "quarantine").recent_operations(),
                [],
                "a refused replacement must not journal anything",
            )


if __name__ == "__main__":
    unittest.main()
