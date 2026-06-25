from __future__ import annotations

import unittest

from dedupe.thresholds import LabeledScore, evaluate_threshold, recommend_threshold


class ThresholdTests(unittest.TestCase):
    def test_evaluate_threshold_counts_false_results(self) -> None:
        scores = [
            LabeledScore("positive-pass", True, 95),
            LabeledScore("positive-fail", True, 85),
            LabeledScore("negative-pass", False, 92),
            LabeledScore("negative-fail", False, 40),
        ]
        metrics = evaluate_threshold(scores, 90)
        self.assertEqual(metrics.true_positives, 1)
        self.assertEqual(metrics.false_negatives, 1)
        self.assertEqual(metrics.false_positives, 1)
        self.assertEqual(metrics.true_negatives, 1)

    def test_recommend_threshold_prioritizes_avoiding_false_positives(self) -> None:
        scores = [
            LabeledScore("strong-positive", True, 98),
            LabeledScore("weak-positive", True, 88),
            LabeledScore("hard-negative", False, 89),
            LabeledScore("easy-negative", False, 35),
        ]
        recommended = recommend_threshold(scores, 90)
        self.assertEqual(recommended.threshold, 90)
        self.assertEqual(recommended.false_positives, 0)
        self.assertEqual(recommended.false_negatives, 1)


if __name__ == "__main__":
    unittest.main()
