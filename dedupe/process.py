from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any

_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run() that never flashes a console window on Windows and
    never blocks on an inherited stdin, for ffmpeg/ffprobe child processes."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if _NO_WINDOW_FLAGS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW_FLAGS
    return subprocess.run(command, **kwargs)


def popen(command: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen() with the same window-suppression and stdin default as run()."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if _NO_WINDOW_FLAGS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW_FLAGS
    return subprocess.Popen(command, **kwargs)
