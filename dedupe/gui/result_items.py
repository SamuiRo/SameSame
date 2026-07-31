from __future__ import annotations

from dataclasses import dataclass

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


def build_review_items(report: DedupeReport) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    exact_by_paths = {frozenset(group.paths): group for group in report.exact_duplicates}
    clusters = resolve_content_clusters(report)
    counters = {"exact": 0, "video": 0, "image": 0, "audio": 0, "content": 0}
    for cluster in clusters:
        counters[cluster.level] += 1
        confidence = 100.0 if cluster.level == "exact" else cluster.confidence
        items.append(
            ReviewItem(
                category=cluster.level,
                title=f"{_LEVEL_LABELS[cluster.level]} {counters[cluster.level]} · {len(cluster.paths)} files"
                + ("" if cluster.level == "exact" else f" · {confidence:.2f}% min confidence"),
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
