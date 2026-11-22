"""Unit tests: git operations (read_commits, render, write, validation)."""

import unittest
from pathlib import Path

from releasescribe import gitops
from releasescribe.models import ReleaseNotes
from tests.helpers import make_repo, tag


class TestReadCommits(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(
            [
                ("feat: add login", "app.py", "# app"),
                ("fix: correct total", "cart.py", "total = 1"),
                ("feat: add dark mode", "theme.py", "DARK = True"),
            ]
        )
        tag(self.root, "v0.1.0", "HEAD~2")

    def test_reads_range_in_order(self):
        commits = gitops.read_commits(self.root, "v0.1.0..HEAD")
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0].subject, "feat: add dark mode")
        self.assertEqual(commits[1].subject, "fix: correct total")
        self.assertEqual(len(commits[0].sha), 40)
        self.assertEqual(commits[0].author, "Test")

    def test_reads_single_ref(self):
        commits = gitops.read_commits(self.root, "HEAD~1..HEAD")
        self.assertEqual(len(commits), 1)

    def test_empty_range(self):
        commits = gitops.read_commits(self.root, "HEAD..HEAD")
        self.assertEqual(commits, [])

    def test_unknown_range_raises(self):
        with self.assertRaises(ValueError):
            gitops.read_commits(self.root, "v9.9.9..HEAD")

    def test_range_injection_rejected(self):
        for bad in ["HEAD; rm -rf /", "--all", "HEAD | cat", "v1..HEAD\nMALICIOUS"]:
            with self.assertRaises(ValueError, msg=bad):
                gitops.read_commits(self.root, bad)

    def test_not_a_repo_raises(self):
        with self.assertRaises(ValueError):
            gitops.read_commits(Path("/tmp"), "HEAD")


class TestRenderChangelog(unittest.TestCase):
    def test_renders_all_sections(self):
        notes = ReleaseNotes(
            summary="A big release.",
            features=["Dark mode toggle"],
            fixes=["Correct total"],
            breaking=["Removed /v1 endpoint"],
            notes=["Docs updated"],
        )
        text = gitops.render_changelog(notes, "v0.2.0")
        self.assertIn("## v0.2.0", text)
        self.assertIn("A big release.", text)
        self.assertIn("### Features", text)
        self.assertIn("- Dark mode toggle", text)
        self.assertIn("### Fixes", text)
        self.assertIn("### Breaking changes", text)
        self.assertIn("- Removed /v1 endpoint", text)

    def test_empty_sections_omitted(self):
        notes = ReleaseNotes(summary="Small fix release.", fixes=["One fix"])
        text = gitops.render_changelog(notes, "Unreleased")
        self.assertNotIn("### Features", text)
        self.assertNotIn("### Breaking changes", text)
        self.assertIn("### Fixes", text)


class TestWriteChangelogFile(unittest.TestCase):
    def setUp(self):
        self.root = make_repo([("feat: x", "a.py", "x")])

    def test_writes_at_repo_root(self):
        target = gitops.write_changelog_file(self.root, "## v1\n\nHi.\n")
        self.assertEqual(target, self.root / "CHANGELOG.md")
        self.assertEqual(target.read_text(), "## v1\n\nHi.\n")

    def test_empty_content_refused(self):
        with self.assertRaises(ValueError):
            gitops.write_changelog_file(self.root, "   \n")


if __name__ == "__main__":
    unittest.main()
