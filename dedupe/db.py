from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    """A sqlite3 connection with WAL journaling, NORMAL synchronous, and a
    busy timeout so concurrent readers/writers within one process (e.g. the
    journal dialog on the GUI thread and a consolidation worker on a
    background thread, both against operations.sqlite3) wait for each other
    instead of one failing immediately with "database is locked"."""
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection
