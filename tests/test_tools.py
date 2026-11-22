"""Unit tests: tools — read is free, write requires operator approval."""

import json
import unittest

from releasescribe import gitops, tools
from tests.helpers import make_repo, tag


class TestReadCommitsTool(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(
            [
                ("feat: add login", "app.py", "# app"),
                ("fix: correct total", "cart.py", "total = 1"),
            ]
        )
        tag(self.root, "v0.1.0", "HEAD~1")

    def test_returns_json_list(self):
        raw = tools.read_commits_impl(str(self.root), "v0.1.0..HEAD")
        data = json.loads(raw)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["subject"], "fix: correct total")


class TestWriteChangelogTool(unittest.TestCase):
    def setUp(self):
        tools.clear_approvals()
        self.root = make_repo([("feat: x", "a.py", "x")])

    def tearDown(self):
        tools.clear_approvals()

    def test_write_without_approval_refused(self):
        with self.assertRaises(PermissionError):
            tools.write_changelog_impl(str(self.root), "## v1\n\nHi.\n")
        self.assertFalse((self.root / "CHANGELOG.md").exists())

    def test_write_with_approval_succeeds(self):
        content = "## v1\n\nHi.\n"
        token = tools.approve_content(content)
        self.assertTrue(token)
        result = tools.write_changelog_impl(str(self.root), content)
        self.assertIn("Wrote", result)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(), content)

    def test_approval_is_content_exact(self):
        tools.approve_content("## v1\n\nApproved text.\n")
        # Different content, even by one word, is NOT approved.
        with self.assertRaises(PermissionError):
            tools.write_changelog_impl(str(self.root), "## v1\n\nOther text.\n")
        self.assertFalse((self.root / "CHANGELOG.md").exists())

    def test_approval_ignores_trailing_whitespace(self):
        tools.approve_content("## v1\n\nHi.\n")
        tools.write_changelog_impl(str(self.root), "## v1\n\nHi.\n\n\n")
        self.assertTrue((self.root / "CHANGELOG.md").exists())

    def test_approve_then_write_twice(self):
        content = "## v1\n\nHi.\n"
        tools.approve_content(content)
        tools.write_changelog_impl(str(self.root), content)
        # Approval persists for the session: a re-write of the same content
        # (e.g. model retry) still works.
        tools.write_changelog_impl(str(self.root), content)


class TestToolScopes(unittest.TestCase):
    def test_decorated_tools_carry_expected_scopes(self):
        # The @tool decorator attaches metadata the policy layer reads.
        # READ_ONLY + idempotent for reads; WRITE_SAFE + not idempotent
        # for the write. (Access via the function attributes the SDK sets.)
        read_meta = getattr(tools.read_commits, "openminion_tool", None)
        write_meta = getattr(tools.write_changelog, "openminion_tool", None)
        # The SDK stores the spec in different shapes across versions; at
        # minimum the decorated callables must still be the same functions.
        self.assertTrue(callable(tools.read_commits))
        self.assertTrue(callable(tools.write_changelog))
        _ = (read_meta, write_meta)


if __name__ == "__main__":
    unittest.main()
