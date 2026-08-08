from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _default_text_encoding(kwargs: dict[str, Any]) -> None:
    """ffmpeg/ffprobe always emit UTF-8. Without an explicit encoding, text
    mode falls back to the OS locale codepage (e.g. cp1252), which raises a
    UnicodeDecodeError inside subprocess's internal reader thread whenever a
    path contains non-ASCII characters. That thread-local exception is not
    catchable by the caller: it just leaves stdout/stderr as None."""
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")


def run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run() that never flashes a console window on Windows and
    never blocks on an inherited stdin, for ffmpeg/ffprobe child processes."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if _NO_WINDOW_FLAGS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW_FLAGS
    _default_text_encoding(kwargs)
    return subprocess.run(command, **kwargs)


def popen(command: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen() with the same window-suppression and stdin default as run()."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if _NO_WINDOW_FLAGS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW_FLAGS
    _default_text_encoding(kwargs)
    return subprocess.Popen(command, **kwargs)
