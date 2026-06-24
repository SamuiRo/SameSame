from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FileRecord:
    path: Path
    root: Path
    size: int
    mtime: float
    raw_name: str
    partial_hash: str | None = None
    partial_hash_algo: str | None = None
    full_hash: str | None = None
    full_hash_algo: str | None = None
    duration: float | None = None
    fingerprint: list[int] | None = None
    image_fingerprint: list[int] | None = None

    @property
    def path_key(self) -> str:
        return str(self.path)

    @property
    def root_key(self) -> str:
        return str(self.root)


@dataclass(slots=True)
class NormalizedName:
    raw_name: str
    core_title: str
    year: int | None = None
    episode: int | None = None
    flags: list[str] = field(default_factory=list)
    source: str = "cache"

    @property
    def cluster_key(self) -> tuple[str, int | None, int | None]:
        return (self.core_title.casefold().strip(), self.year, self.episode)


@dataclass(slots=True)
class ExactDuplicateGroup:
    hash_value: str
    paths: list[str]
    size: int
    similarity: float = 100.0


@dataclass(slots=True)
class VideoMatch:
    left: str
    right: str
    similarity: float
    duration_delta: float
    level: str = "video"


@dataclass(slots=True)
class ImageMatch:
    left: str
    right: str
    similarity: float
    level: str = "image"


@dataclass(slots=True)
class NameHint:
    key: str
    similarity: float
    paths: list[str]
    title: str
    year: int | None = None
    episode: int | None = None


@dataclass(slots=True)
class ClusterAssignment:
    cluster_id: str
    level: str
    confidence: float


@dataclass(slots=True)
class FolderPair:
    left: str
    right: str
    similarity: float
    content_similarity: float
    name_assisted_similarity: float
    matched: list[dict[str, Any]]
    left_only: list[str]
    right_only: list[str]


@dataclass(slots=True)
class DedupeReport:
    scanned_files: int
    exact_duplicates: list[ExactDuplicateGroup]
    video_matches: list[VideoMatch]
    image_matches: list[ImageMatch]
    folder_pairs: list[FolderPair]
    name_hints: list[NameHint]
    warnings: list[str] = field(default_factory=list)
