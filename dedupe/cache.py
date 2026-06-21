from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import FileRecord, NormalizedName


class Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                partial_hash TEXT,
                full_hash TEXT,
                duration REAL,
                fingerprint TEXT,
                raw_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS name_cache (
                raw_name TEXT PRIMARY KEY,
                core_title TEXT,
                year INTEGER,
                episode INTEGER,
                flags TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
            CREATE INDEX IF NOT EXISTS idx_files_duration ON files(duration);
            """
        )
        self.conn.commit()

    def hydrate_if_current(self, record: FileRecord) -> bool:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?",
            (record.path_key,),
        ).fetchone()
        if row is None:
            return False
        if int(row["size"]) != record.size or abs(float(row["mtime"]) - record.mtime) > 0.000001:
            return False
        record.partial_hash = row["partial_hash"]
        record.full_hash = row["full_hash"]
        record.duration = row["duration"]
        record.fingerprint = json.loads(row["fingerprint"]) if row["fingerprint"] else None
        record.raw_name = row["raw_name"]
        return True

    def upsert_file(self, record: FileRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO files(path, size, mtime, partial_hash, full_hash, duration, fingerprint, raw_name)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime = excluded.mtime,
                partial_hash = excluded.partial_hash,
                full_hash = excluded.full_hash,
                duration = excluded.duration,
                fingerprint = excluded.fingerprint,
                raw_name = excluded.raw_name
            """,
            (
                record.path_key,
                record.size,
                record.mtime,
                record.partial_hash,
                record.full_hash,
                record.duration,
                json.dumps(record.fingerprint) if record.fingerprint is not None else None,
                record.raw_name,
            ),
        )

    def upsert_files(self, records: Iterable[FileRecord]) -> None:
        for record in records:
            self.upsert_file(record)
        self.conn.commit()

    def get_name(self, raw_name: str) -> NormalizedName | None:
        row = self.conn.execute(
            "SELECT * FROM name_cache WHERE raw_name = ?",
            (raw_name,),
        ).fetchone()
        if row is None:
            return None
        return NormalizedName(
            raw_name=raw_name,
            core_title=row["core_title"] or raw_name,
            year=row["year"],
            episode=row["episode"],
            flags=json.loads(row["flags"]) if row["flags"] else [],
            source="cache",
        )

    def upsert_name(self, name: NormalizedName) -> None:
        self.conn.execute(
            """
            INSERT INTO name_cache(raw_name, core_title, year, episode, flags)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(raw_name) DO UPDATE SET
                core_title = excluded.core_title,
                year = excluded.year,
                episode = excluded.episode,
                flags = excluded.flags
            """,
            (
                name.raw_name,
                name.core_title,
                name.year,
                name.episode,
                json.dumps(name.flags, ensure_ascii=False),
            ),
        )

    def upsert_names(self, names: Iterable[NormalizedName]) -> None:
        for name in names:
            self.upsert_name(name)
        self.conn.commit()

