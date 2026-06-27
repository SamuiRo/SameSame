from __future__ import annotations

from dataclasses import dataclass

from ..models import DedupeReport

CATEGORY_LABELS = {
    "all": "All results",
    "exact": "Exact duplicates",
    "video": "Similar videos",
    "image": "Similar images",
    "audio": "Similar audio",
    "folder": "Folder pairs",
    "name": "Name hints",
}


@dataclass(frozen=True, slots=True)
class ReviewItem:
    category: str
    title: str
    paths: tuple[str, ...]
    evidence: str
    confidence: float


def build_review_items(report: DedupeReport) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for index, group in enumerate(report.exact_duplicates, start=1):
        items.append(
            ReviewItem(
                category="exact",
                title=f"Exact group {index} · {len(group.paths)} files",
                paths=tuple(group.paths),
                evidence=f"Full hash match · {group.size:,} bytes",
                confidence=group.similarity,
            )
        )
    for index, match in enumerate(report.video_matches, start=1):
        items.append(
            ReviewItem(
                category="video",
                title=f"Video match {index} · {match.similarity:.2f}%",
                paths=(match.left, match.right),
                evidence=f"Sequence-aligned video fingerprint · duration Δ {match.duration_delta:.3f}s",
                confidence=match.similarity,
            )
        )
    for index, match in enumerate(report.image_matches, start=1):
        items.append(
            ReviewItem(
                category="image",
                title=f"Image match {index} · {match.similarity:.2f}%",
                paths=(match.left, match.right),
                evidence="Perceptual structure and average-color fingerprint",
                confidence=match.similarity,
            )
        )
    for index, match in enumerate(report.audio_matches, start=1):
        items.append(
            ReviewItem(
                category="audio",
                title=f"Audio match {index} · {match.similarity:.2f}%",
                paths=(match.left, match.right),
                evidence=f"Chromaprint fingerprint · duration Δ {match.duration_delta:.3f}s",
                confidence=match.similarity,
            )
        )
    for index, pair in enumerate(report.folder_pairs, start=1):
        items.append(
            ReviewItem(
                category="folder",
                title=f"Folder pair {index} · {pair.content_similarity:.2f}% content",
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
