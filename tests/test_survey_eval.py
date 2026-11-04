"""Survey scoring against the golden set, using hand-written results (no model calls)."""

import unittest
from pathlib import Path

from mel.evaluation import load_golden, score_surveys

GOLDEN = Path("eval/golden_holdout.jsonl")


def survey(*option_counts):
    return {
        "items": [
            {
                "prompt": f"Question {n}",
                "options": [{"value": f"o{i}", "label": f"O{i}"} for i in range(count)],
            }
            for n, count in enumerate(option_counts, 1)
        ]
    }


class SurveyEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = load_golden(GOLDEN)
        cls.cases = cases
        cls.with_survey = next(c for c in cases if c.expects_survey)
        cls.without_survey = next(c for c in cases if not c.expects_survey)

    def result(self, case, status, **extra):
        return {
            "insight_id": case.insight["insight_id"],
            "status": status,
            "elapsed_seconds": 2.0,
            "input_tokens": 100,
            "output_tokens": 10,
            "usage_complete": True,
            **extra,
        }

    def test_scores_decisions_shape_and_tokens(self):
        results = [
            self.result(self.with_survey, "accepted", survey=survey(3, 5)),
            self.result(self.without_survey, "accepted", survey=survey(3)),
        ]
        report = score_surveys(results, self.cases)
        self.assertEqual(report["decision_accuracy"], 0.5)
        self.assertEqual(report["options"]["max"], 5)
        self.assertEqual(report["options"]["share_at_most_4"], round(2 / 3, 3))
        self.assertEqual(report["items"]["mean"], 2)
        self.assertEqual(report["tokens"], {"complete": True, "input": 200, "output": 20})
        self.assertIn("golden_items", report["side_by_side"][0])

    def test_incomplete_usage_reports_no_token_totals(self):
        results = [self.result(self.without_survey, "skipped", usage_complete=False)]
        report = score_surveys(results, self.cases)
        self.assertEqual(report["tokens"], {"complete": False, "input": None, "output": None})
        self.assertEqual(report["decision_accuracy"], 1.0)

    def test_results_outside_the_golden_file_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing from the golden file"):
            score_surveys([{"insight_id": "I-unknown", "status": "skipped"}], self.cases)


if __name__ == "__main__":
    unittest.main()
