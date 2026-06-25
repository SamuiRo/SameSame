from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LabeledScore:
    name: str
    expected_match: bool
    similarity: float


@dataclass(frozen=True, slots=True)
class ThresholdMetrics:
    threshold: float
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def weighted_errors(self) -> int:
        return self.false_positives * 5 + self.false_negatives


def evaluate_threshold(scores: Iterable[LabeledScore], threshold: float) -> ThresholdMetrics:
    true_positives = false_positives = true_negatives = false_negatives = 0
    for score in scores:
        predicted_match = score.similarity >= threshold
        if score.expected_match and predicted_match:
            true_positives += 1
        elif score.expected_match:
            false_negatives += 1
        elif predicted_match:
            false_positives += 1
        else:
            true_negatives += 1
    return ThresholdMetrics(
        threshold=round(threshold, 2),
        true_positives=true_positives,
        false_positives=false_positives,
        true_negatives=true_negatives,
        false_negatives=false_negatives,
    )


def recommend_threshold(scores: list[LabeledScore], current_threshold: float) -> ThresholdMetrics:
    if not scores:
        return evaluate_threshold([], current_threshold)
    candidates = {0.0, 100.0, current_threshold}
    for score in scores:
        candidates.add(max(0.0, min(100.0, score.similarity)))
        candidates.add(max(0.0, min(100.0, score.similarity + 0.01)))
    metrics = [evaluate_threshold(scores, threshold) for threshold in candidates]
    return min(
        metrics,
        key=lambda item: (
            item.weighted_errors,
            item.false_positives,
            item.false_negatives,
            abs(item.threshold - current_threshold),
            -item.threshold,
        ),
    )


def summarize_scores(scores: list[LabeledScore], current_threshold: float) -> dict[str, object]:
    positives = [score.similarity for score in scores if score.expected_match]
    negatives = [score.similarity for score in scores if not score.expected_match]
    current = evaluate_threshold(scores, current_threshold)
    recommended = recommend_threshold(scores, current_threshold)
    return {
        "pairs": [asdict(score) for score in scores],
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "min_positive": round(min(positives), 2) if positives else None,
        "max_negative": round(max(negatives), 2) if negatives else None,
        "current": asdict(current),
        "recommended": asdict(recommended),
        "weighting": "false positives count as 5 errors; false negatives count as 1",
    }
