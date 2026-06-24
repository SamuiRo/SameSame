from __future__ import annotations

import json
import sqlite3
import time
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
                partial_hash_algo TEXT,
                full_hash TEXT,
                full_hash_algo TEXT,
                duration REAL,
                fingerprint TEXT,
                image_fingerprint TEXT,
                raw_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS name_cache (
                raw_name TEXT PRIMARY KEY,
                core_title TEXT,
                year INTEGER,
                episode INTEGER,
                flags TEXT,
                provider TEXT,
                model TEXT,
                prompt_hash TEXT,
                created_at REAL
            );

            CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
            CREATE INDEX IF NOT EXISTS idx_files_duration ON files(duration);
            """
        )
        self._ensure_column("files", "partial_hash_algo", "TEXT")
        self._ensure_column("files", "full_hash_algo", "TEXT")
        self._ensure_column("files", "image_fingerprint", "TEXT")
        self._ensure_column("name_cache", "provider", "TEXT")
        self._ensure_column("name_cache", "model", "TEXT")
        self._ensure_column("name_cache", "prompt_hash", "TEXT")
        self._ensure_column("name_cache", "created_at", "REAL")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_type: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

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
        record.partial_hash_algo = row["partial_hash_algo"]
        record.full_hash = row["full_hash"]
        record.full_hash_algo = row["full_hash_algo"]
        record.duration = row["duration"]
        record.fingerprint = json.loads(row["fingerprint"]) if row["fingerprint"] else None
        record.image_fingerprint = json.loads(row["image_fingerprint"]) if row["image_fingerprint"] else None
        record.raw_name = row["raw_name"]
        return True

    def upsert_file(self, record: FileRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO files(
                path, size, mtime, partial_hash, partial_hash_algo, full_hash,
                full_hash_algo, duration, fingerprint, image_fingerprint, raw_name
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime = excluded.mtime,
                partial_hash = excluded.partial_hash,
                partial_hash_algo = excluded.partial_hash_algo,
                full_hash = excluded.full_hash,
                full_hash_algo = excluded.full_hash_algo,
                duration = excluded.duration,
                fingerprint = excluded.fingerprint,
                image_fingerprint = excluded.image_fingerprint,
                raw_name = excluded.raw_name
            """,
            (
                record.path_key,
                record.size,
                record.mtime,
                record.partial_hash,
                record.partial_hash_algo,
                record.full_hash,
                record.full_hash_algo,
                record.duration,
                json.dumps(record.fingerprint) if record.fingerprint is not None else None,
                json.dumps(record.image_fingerprint) if record.image_fingerprint is not None else None,
                record.raw_name,
            ),
        )

    def upsert_files(self, records: Iterable[FileRecord]) -> None:
        for record in records:
            self.upsert_file(record)
        self.conn.commit()

    def clear_hashes(self, records: Iterable[FileRecord]) -> None:
        paths = [record.path_key for record in records]
        for record in records:
            record.partial_hash = None
            record.partial_hash_algo = None
            record.full_hash = None
            record.full_hash_algo = None
        self.conn.executemany(
            "UPDATE files SET partial_hash = NULL, partial_hash_algo = NULL, full_hash = NULL, full_hash_algo = NULL WHERE path = ?",
            [(path,) for path in paths],
        )
        self.conn.commit()

    def clear_video(self, records: Iterable[FileRecord]) -> None:
        paths = [record.path_key for record in records]
        for record in records:
            record.duration = None
            record.fingerprint = None
        self.conn.executemany(
            "UPDATE files SET duration = NULL, fingerprint = NULL WHERE path = ?",
            [(path,) for path in paths],
        )
        self.conn.commit()

    def clear_images(self, records: Iterable[FileRecord]) -> None:
        paths = [record.path_key for record in records]
        for record in records:
            record.image_fingerprint = None
        self.conn.executemany(
            "UPDATE files SET image_fingerprint = NULL WHERE path = ?",
            [(path,) for path in paths],
        )
        self.conn.commit()

    def get_name(
        self,
        raw_name: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
    ) -> NormalizedName | None:
        row = self.conn.execute(
            "SELECT * FROM name_cache WHERE raw_name = ?",
            (raw_name,),
        ).fetchone()
        if row is None:
            return None
        if provider is not None and row["provider"] != provider:
            return None
        if model is not None and row["model"] != model:
            return None
        if prompt_hash is not None and row["prompt_hash"] != prompt_hash:
            return None
        return NormalizedName(
            raw_name=raw_name,
            core_title=row["core_title"] or raw_name,
            year=row["year"],
            episode=row["episode"],
            flags=json.loads(row["flags"]) if row["flags"] else [],
            source="cache",
        )

    def upsert_name(
        self,
        name: NormalizedName,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO name_cache(raw_name, core_title, year, episode, flags, provider, model, prompt_hash, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw_name) DO UPDATE SET
                core_title = excluded.core_title,
                year = excluded.year,
                episode = excluded.episode,
                flags = excluded.flags,
                provider = excluded.provider,
                model = excluded.model,
                prompt_hash = excluded.prompt_hash,
                created_at = excluded.created_at
            """,
            (
                name.raw_name,
                name.core_title,
                name.year,
                name.episode,
                json.dumps(name.flags, ensure_ascii=False),
                provider or name.source,
                model,
                prompt_hash,
                time.time(),
            ),
        )

    def upsert_names(
        self,
        names: Iterable[NormalizedName],
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
    ) -> None:
        for name in names:
            self.upsert_name(name, provider=provider, model=model, prompt_hash=prompt_hash)
        self.conn.commit()

    def clear_names(self, raw_names: Iterable[str]) -> None:
        self.conn.executemany("DELETE FROM name_cache WHERE raw_name = ?", [(raw_name,) for raw_name in raw_names])
        self.conn.commit()

    def stats(self) -> dict[str, object]:
        file_counts = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN partial_hash IS NOT NULL THEN 1 ELSE 0 END), 0) AS partial_hashed,
                COALESCE(SUM(CASE WHEN full_hash IS NOT NULL THEN 1 ELSE 0 END), 0) AS full_hashed,
                COALESCE(SUM(CASE WHEN duration IS NOT NULL THEN 1 ELSE 0 END), 0) AS durations,
                COALESCE(SUM(CASE WHEN fingerprint IS NOT NULL THEN 1 ELSE 0 END), 0) AS fingerprints,
                COALESCE(SUM(CASE WHEN image_fingerprint IS NOT NULL THEN 1 ELSE 0 END), 0) AS image_fingerprints
            FROM files
            """
        ).fetchone()
        hash_algorithms = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT full_hash_algo AS algorithm, COUNT(*) AS files
                FROM files
                WHERE full_hash_algo IS NOT NULL
                GROUP BY full_hash_algo
                ORDER BY files DESC
                """
            )
        ]
        name_providers = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT provider, model, prompt_hash, COUNT(*) AS names
                FROM name_cache
                GROUP BY provider, model, prompt_hash
                ORDER BY names DESC
                """
            )
        ]
        return {
            "path": str(self.path),
            "files": dict(file_counts) if file_counts else {},
            "hash_algorithms": hash_algorithms,
            "name_providers": name_providers,
        }
