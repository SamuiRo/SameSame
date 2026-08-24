from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VIDEO_FINGERPRINT_VERSION = 3
# Absolute-time alignment grid (A-01). Versioned separately from the
# proportional fingerprint above because the two are sampled differently and
# invalidate for different reasons: bump this when the cadence or the sample
# layout changes, not when the proportional fingerprint does.
GRID_FINGERPRINT_VERSION = 2
# Stored in `grid_fingerprint_version` when a grid build was attempted and
# failed, so an unreadable file is not re-decoded on every subsequent scan -
# each attempt is a full decode under a 600 s timeout (A-20). Negating the
# current version means bumping the version also retries past failures, and a
# file whose size or mtime changes fails `hydrate_if_current` outright, so an
# edited or repaired file is retried regardless.
GRID_FINGERPRINT_FAILED = -GRID_FINGERPRINT_VERSION
# 1: one perceptual hash plus the average colour. 2: eight dihedral hashes of
# the border-trimmed image, plus its trimmed average colour (A-15). 3: the
# eight dihedral views are generated from one shrink-to-hash-grid pass instead
# of eight full-resolution rotations each independently resized, which
# measured 9x faster on a real 54k-image library and is the dominant per-file
# fingerprinting cost at that scale (A-35). Bit-identical for width-preserving
# views (0/180 degrees and mirroring); the 90/270-degree views of a non-square
# image can differ by up to 2 of 64 bits from the old value, a PIL
# resize/rotate rounding property rather than a logic difference - measured
# on 300 real images: 0 mismatches at 0/180, ~4% of 90/270 views affected, all
# by 1-2 bits. That is far inside every real matching margin (A-06, A-15).
IMAGE_FINGERPRINT_VERSION = 3


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
    fingerprint_version: int | None = None
    grid_fingerprint: list[int] | None = None
    grid_fingerprint_version: int | None = None
    image_fingerprint: list[int] | None = None
    image_fingerprint_version: int | None = None
    audio_fingerprint: list[int] | None = None

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
class AudioMatch:
    left: str
    right: str
    similarity: float
    duration_delta: float
    level: str = "audio"


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
class ClusterEvidence:
    left: str
    right: str
    similarity: float
    level: str


@dataclass(slots=True)
class ContentCluster:
    cluster_id: str
    level: str
    confidence: float
    paths: list[str]
    evidence: list[ClusterEvidence] = field(default_factory=list)


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
    scope: str = "directory"


@dataclass(slots=True)
class DedupeReport:
    scanned_files: int
    exact_duplicates: list[ExactDuplicateGroup]
    video_matches: list[VideoMatch]
    image_matches: list[ImageMatch]
    audio_matches: list[AudioMatch]
    folder_pairs: list[FolderPair]
    name_hints: list[NameHint]
    warnings: list[str] = field(default_factory=list)
    content_clusters: list[ContentCluster] = field(default_factory=list)
