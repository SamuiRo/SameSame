from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..folder_compare import resolve_content_clusters
from ..models import ContentCluster, DedupeReport, ExactDuplicateGroup

CATEGORY_LABELS = {
    "all": "All results",
    "exact": "Exact duplicates",
    "video": "Similar videos",
    "image": "Similar images",
    "audio": "Similar audio",
    "content": "Mixed-evidence clusters",
    "folder": "Folder pairs",
    "name": "Name hints",
}

_LEVEL_LABELS = {
    "exact": "Exact group",
    "video": "Video cluster",
    "image": "Image cluster",
    "audio": "Audio cluster",
    "content": "Mixed-evidence cluster",
}


@dataclass(frozen=True, slots=True)
class ExactDuplicatePlan:
    """One exact group's keeper and the redundant copies beside it."""

    keeper: str
    removals: tuple[str, ...]
    size: int

    @property
    def reclaimed_bytes(self) -> int:
        return self.size * len(self.removals)


@dataclass(frozen=True, slots=True)
class ReviewItem:
    category: str
    title: str
    paths: tuple[str, ...]
    evidence: str
    confidence: float


_EVIDENCE_DESCRIPTIONS = {
    "video": "Sequence-aligned video fingerprint",
    "image": "Perceptual structure and average-color fingerprint",
    "audio": "Chromaprint fingerprint",
}


def _path_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _cluster_evidence(cluster: ContentCluster, exact_by_paths: dict[frozenset[str], ExactDuplicateGroup]) -> str:
    if cluster.level == "exact":
        group = exact_by_paths.get(frozenset(cluster.paths))
        size_text = f"{group.size:,} bytes" if group is not None else "byte-identical"
        return f"Full hash match · {size_text}"
    lines = [
        f"{_EVIDENCE_DESCRIPTIONS[pair.level]}: {_path_name(pair.left)} ↔ {_path_name(pair.right)} · {pair.similarity:.2f}%"
        for pair in cluster.evidence
    ]
    return "<br>".join(lines) if lines else "Content-backed match"


def exact_duplicate_keeper(paths: Sequence[str]) -> str:
    """Which copy of a byte-identical group to keep in place.

    The files are identical, so this only chooses a *location*. Shallowest
    first, because a copy sitting nearer the collection root is far more often
    the one the library is organised around than one buried in a subfolder;
    then shortest path, then lexicographic, so the choice is total and stable
    across runs rather than dependent on scan order (A-37).
    """
    return min(paths, key=lambda path: (path.replace("\\", "/").count("/"), len(path), path))


def plan_exact_duplicate_quarantine(report: DedupeReport) -> list[ExactDuplicatePlan]:
    """One keeper and its redundant copies for every exact group, in report order.

    Kept as a pure function over the report so the bulk action's arithmetic -
    which files, how many bytes - can be tested without constructing a Qt
    window, and so the confirmation dialog and the jobs it launches are
    computed from exactly the same list.
    """
    plans: list[ExactDuplicatePlan] = []
    for group in report.exact_duplicates:
        if len(group.paths) < 2:
            continue
        keeper = exact_duplicate_keeper(group.paths)
        removals = tuple(path for path in sorted(group.paths) if path != keeper)
        if removals:
            plans.append(ExactDuplicatePlan(keeper=keeper, removals=removals, size=group.size))
    return plans


def build_review_items(report: DedupeReport) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    exact_by_paths = {frozenset(group.paths): group for group in report.exact_duplicates}
    clusters = resolve_content_clusters(report)
    # Exact groups come from the report directly rather than from the clusters,
    # because content clustering is single-linkage over *all* evidence: a
    # byte-identical pair that is also perceptually similar to a third file is
    # absorbed into that larger cluster and stops being reachable under
    # "Exact duplicates" at all. On a real 54,100-image library only 265 of
    # 1,354 exact groups survived as exact-level clusters, so the category
    # that exists precisely to offer risk-free deletions was hiding 80% of
    # them (A-37). The clusters keep every other category; a file legitimately
    # appears in both, being both a byte-identical copy of one file and a
    # near-duplicate of others.
    counters = {"exact": 0, "video": 0, "image": 0, "audio": 0, "content": 0}
    for group in report.exact_duplicates:
        if len(group.paths) < 2:
            continue
        counters["exact"] += 1
        items.append(
            ReviewItem(
                category="exact",
                title=f"{_LEVEL_LABELS['exact']} {counters['exact']} · {len(group.paths)} files",
                paths=tuple(sorted(group.paths)),
                evidence=f"Full hash match · {group.size:,} bytes",
                confidence=100.0,
            )
        )
    for cluster in clusters:
        if cluster.level == "exact":
            continue
        counters[cluster.level] += 1
        confidence = cluster.confidence
        items.append(
            ReviewItem(
                category=cluster.level,
                title=f"{_LEVEL_LABELS[cluster.level]} {counters[cluster.level]} · {len(cluster.paths)} files"
                f" · {confidence:.2f}% min confidence",
                paths=tuple(cluster.paths),
                evidence=_cluster_evidence(cluster, exact_by_paths),
                confidence=confidence,
            )
        )
    for index, pair in enumerate(report.folder_pairs, start=1):
        items.append(
            ReviewItem(
                category="folder",
                title=f"Folder pair {index} ({pair.scope}) · {pair.content_similarity:.2f}% content",
                paths=(pair.left, pair.right),
                evidence=f"Name-assisted similarity {pair.name_assisted_similarity:.2f}%",
                confidence=pair.content_similarity,
            )
        )
    for index, hint in enumerate(report.name_hints, start=1):
        episode = f" · episode {hint.episode}" if hint.episode is not None else ""
        year = f" · {hint.year}" if hint.year is not None else ""
        items.append(
            ReviewItem(
                category="name",
                title=f"Name hint {index} · {hint.title}{year}{episode}",
                paths=tuple(hint.paths),
                evidence="Name-only hint; not deletion evidence",
                confidence=hint.similarity,
            )
        )
    return items


def category_counts(items: list[ReviewItem]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORY_LABELS}
    counts["all"] = len(items)
    for item in items:
        counts[item.category] += 1
    return counts
