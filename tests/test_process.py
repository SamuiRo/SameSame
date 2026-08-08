from __future__ import annotations

import locale
import subprocess
import sys
import unittest
from unittest.mock import patch

from dedupe import process

_CYRILLIC_STDOUT_COMMAND = [
    sys.executable,
    "-c",
    "import sys; sys.stdout.buffer.write('видео_тест\\n'.encode('utf-8'))",
]


class ProcessTextEncodingDefaultsTests(unittest.TestCase):
    """ffprobe/ffmpeg always emit UTF-8. Without a forced encoding, text mode
    falls back to locale.getpreferredencoding(), which on a non-UTF-8 system
    locale (e.g. Windows cp1252/cp1251) can't decode every Cyrillic filename
    ffprobe echoes back in its JSON `format.filename` field."""

    def test_run_adds_utf8_encoding_when_text_mode_is_requested(self) -> None:
        with patch("dedupe.process.subprocess.run") as mock_run:
            process.run(["ffprobe"], text=True)
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")

    def test_run_leaves_binary_mode_untouched(self) -> None:
        with patch("dedupe.process.subprocess.run") as mock_run:
            process.run(["ffprobe"])
        _, kwargs = mock_run.call_args
        self.assertNotIn("encoding", kwargs)
        self.assertNotIn("errors", kwargs)

    def test_run_does_not_override_an_explicit_encoding(self) -> None:
        with patch("dedupe.process.subprocess.run") as mock_run:
            process.run(["ffprobe"], text=True, encoding="cp1251")
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["encoding"], "cp1251")

    def test_popen_adds_utf8_encoding_when_text_mode_is_requested(self) -> None:
        with patch("dedupe.process.subprocess.Popen") as mock_popen:
            process.popen(["ffmpeg"], text=True)
        _, kwargs = mock_popen.call_args
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")


@unittest.skipUnless(
    locale.getpreferredencoding(False) != "utf-8",
    "only reproduces the silent-corruption symptom on a non-UTF-8 system locale",
)
class ProcessTextEncodingBehaviorTests(unittest.TestCase):
    """Demonstrates the actual failure this fixes: subprocess decodes text on a
    background thread, so a decode error there never reaches the caller as an
    exception - it just leaves .stdout as None, which later surfaces as an
    unrelated-looking `TypeError` from `json.loads(None)`."""

    def test_uncorrected_subprocess_run_silently_loses_stdout(self) -> None:
        completed = subprocess.run(_CYRILLIC_STDOUT_COMMAND, capture_output=True, text=True, timeout=10)
        self.assertIsNone(completed.stdout)

    def test_process_run_decodes_cyrillic_output_correctly(self) -> None:
        completed = process.run(_CYRILLIC_STDOUT_COMMAND, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.stdout.strip(), "видео_тест")


if __name__ == "__main__":
    unittest.main()
