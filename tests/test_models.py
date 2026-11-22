"""Unit tests: typed output (ReleaseNotes / ReviewVerdict validation)."""

import unittest

from pydantic import ValidationError

from releasescribe.models import ReleaseNotes, ReviewVerdict


class TestReleaseNotes(unittest.TestCase):
    def test_valid_minimal(self):
        notes = ReleaseNotes(summary="Release.")
        self.assertEqual(notes.features, [])
        self.assertEqual(notes.fixes, [])
        self.assertEqual(notes.breaking, [])
        self.assertEqual(notes.notes, [])

    def test_valid_full(self):
        notes = ReleaseNotes.model_validate_json(
            '{"summary": "S.", "features": ["a"], "fixes": [], '
            '"breaking": ["b"], "notes": []}'
        )
        self.assertEqual(notes.features, ["a"])
        self.assertEqual(notes.breaking, ["b"])

    def test_missing_summary_rejected(self):
        with self.assertRaises(ValidationError):
            ReleaseNotes.model_validate({"features": ["a"]})

    def test_wrong_types_rejected(self):
        with self.assertRaises(ValidationError):
            ReleaseNotes.model_validate(
                {"summary": "S.", "features": "not-a-list"}
            )

    def test_non_json_text_rejected(self):
        with self.assertRaises(ValidationError):
            ReleaseNotes.model_validate_json("just some prose, no JSON")


class TestReviewVerdict(unittest.TestCase):
    def test_ok_verdict(self):
        verdict = ReviewVerdict.model_validate_json(
            '{"verdict": "ok", "issues": []}'
        )
        self.assertEqual(verdict.verdict, "ok")

    def test_issues_verdict(self):
        verdict = ReviewVerdict(
            verdict="issues_found", issues=["'Teleport' is not in any commit"]
        )
        self.assertEqual(len(verdict.issues), 1)


if __name__ == "__main__":
    unittest.main()
