"""Unit tests: orchestration — fallback, session refinement, write gate.

Uses fake agents (no model, no network). The fake mimics the OpenMinion
``Agent`` surface used by ReleaseScribe: context manager + ``run()``.
"""

import unittest

from openminion import ProviderError

from releasescribe import tools
from releasescribe.agent import ReleaseScribe, derive_session_id
from releasescribe.models import ReleaseNotes, ReviewVerdict
from releasescribe.providers import ProviderSpec
from tests.helpers import make_repo, tag

NOTES_A = ReleaseNotes(
    summary="First draft.", features=["Dark mode"], fixes=[], breaking=[]
)
NOTES_B = ReleaseNotes(
    summary="Shorter summary.", features=["Dark mode"], fixes=[], breaking=[]
)


class FakeResult:
    def __init__(self, output=None, text=""):
        self.output = output
        self.text = text
        self.session_id = "fake-session"
        self.thread_id = None


class FakeAgent:
    """Records every run call; returns canned outputs in order."""

    def __init__(self, outputs, fail_with=None):
        self.outputs = list(outputs)
        self.fail_with = fail_with
        self.runs: list[dict] = []

    def __enter__(self):
        if self.fail_with is not None:
            raise self.fail_with
        return self

    def __exit__(self, *exc):
        return False

    def run(self, message, timeout_seconds=None, approval_callback=None,
            session_id=None, thread_id=None):
        self.runs.append(
            {"message": message, "approval_callback": approval_callback}
        )
        if self.fail_with is not None:
            raise self.fail_with
        output = self.outputs.pop(0) if self.outputs else None
        text = output.model_dump_json() if output is not None else "ok"
        return FakeResult(output=output, text=text)


def _chain():
    return (
        ProviderSpec(
            name="primary/fake", setup_id="openrouter", model="fake/a",
            base_url="https://example.invalid/v1",
            api_key_env="RS_FAKE_KEY_A",
            surrogate_credential="custom.fake-a", max_tokens=64,
        ),
        ProviderSpec(
            name="fallback/fake", setup_id="custom-openai-compatible",
            model="fake/b", base_url="https://example.invalid/v1",
            api_key_env="RS_FAKE_KEY_B",
            surrogate_credential="custom.fake-b", max_tokens=64,
        ),
    )


class TestSessionId(unittest.TestCase):
    def test_deterministic_per_repo_and_range(self):
        root = make_repo([("feat: x", "a.py", "x")])
        a = derive_session_id(root, "v0.1.0..HEAD")
        b = derive_session_id(root, "v0.1.0..HEAD")
        c = derive_session_id(root, "v0.1.0..HEAD~1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("releasescribe-"))


class TestDraftFallback(unittest.TestCase):
    def setUp(self):
        tools.clear_approvals()
        self.root = make_repo([("feat: x", "a.py", "x")])
        tag(self.root, "v0.1.0", "HEAD")

    def tearDown(self):
        tools.clear_approvals()

    def test_primary_failure_falls_back(self):
        calls = []

        def factory(spec, **kwargs):
            calls.append(spec.name)
            if spec.name == "primary/fake":
                raise ProviderError("primary down", code="PROVIDER_ERROR")
            return FakeAgent([NOTES_A])

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=factory,
        )
        # Credentials: pretend both env vars are set so no surrogate is needed.
        import os
        os.environ["RS_FAKE_KEY_A"] = "x"
        os.environ["RS_FAKE_KEY_B"] = "x"
        try:
            notes = scribe.draft()
        finally:
            del os.environ["RS_FAKE_KEY_A"]
            del os.environ["RS_FAKE_KEY_B"]
        self.assertEqual(notes, NOTES_A)
        self.assertEqual(scribe.provider_used, "fallback/fake")
        self.assertEqual(calls, ["primary/fake", "fallback/fake"])
        self.assertTrue(
            scribe.attempts[0]["outcome"].startswith("failed: ProviderError")
        )
        self.assertEqual(scribe.attempts[1]["outcome"], "draft ok")

    def test_invalid_json_retried_once_in_session(self):
        """A validation failure gets one in-session retry before fallback."""
        from openminion import AgentOutputValidationError

        runs = {"n": 0}

        class RetryAgent(FakeAgent):
            def run(self, message, timeout_seconds=None, **kw):
                runs["n"] += 1
                if runs["n"] == 1:
                    raise AgentOutputValidationError(
                        "not json", raw_text="**Features...**"
                    )
                return FakeResult(output=NOTES_A, text=NOTES_A.model_dump_json())

        agent = RetryAgent([])
        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: agent,
        )
        import os
        os.environ["RS_FAKE_KEY_A"] = "x"
        os.environ["RS_FAKE_KEY_B"] = "x"
        try:
            notes = scribe.draft()
        finally:
            del os.environ["RS_FAKE_KEY_A"]
            del os.environ["RS_FAKE_KEY_B"]
        self.assertEqual(notes, NOTES_A)
        self.assertEqual(runs["n"], 2)
        self.assertEqual(
            scribe.attempts[0]["outcome"], "invalid typed output, retrying"
        )

    def test_all_providers_fail_raises(self):
        def factory(spec, **kwargs):
            raise ProviderError("all down", code="PROVIDER_ERROR")

        import os
        os.environ["RS_FAKE_KEY_A"] = "x"
        os.environ["RS_FAKE_KEY_B"] = "x"
        try:
            scribe = ReleaseScribe(
                self.root, "v0.1.0..HEAD", provider_chain=_chain(),
                agent_factory=factory,
            )
            with self.assertRaises(RuntimeError):
                scribe.draft()
        finally:
            del os.environ["RS_FAKE_KEY_A"]
            del os.environ["RS_FAKE_KEY_B"]


class TestRefineAndWrite(unittest.TestCase):
    def setUp(self):
        tools.clear_approvals()
        self.root = make_repo([("feat: x", "a.py", "x")])
        tag(self.root, "v0.1.0", "HEAD")
        import os
        os.environ["RS_FAKE_KEY_A"] = "x"
        os.environ["RS_FAKE_KEY_B"] = "x"
        self._env = True

    def tearDown(self):
        import os
        os.environ.pop("RS_FAKE_KEY_A", None)
        os.environ.pop("RS_FAKE_KEY_B", None)
        tools.clear_approvals()

    def _scribe_with_draft(self, agent):
        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: agent,
        )
        notes = scribe.draft()
        return scribe, notes

    def test_refine_runs_in_same_session(self):
        agent = FakeAgent([NOTES_A, NOTES_B])
        scribe, _ = self._scribe_with_draft(agent)
        refined = scribe.refine("perpendek summary")
        self.assertEqual(refined, NOTES_B)
        self.assertEqual(len(agent.runs), 2)
        self.assertIn("perpendek summary", agent.runs[1]["message"])
        # Both turns used the same factory/spec (same session id upstream).
        self.assertEqual(scribe.session_id, derive_session_id(
            self.root, "v0.1.0..HEAD"))

    def test_write_without_operator_approval_refused(self):
        agent = FakeAgent([NOTES_A])
        scribe, notes = self._scribe_with_draft(agent)
        with self.assertRaises(PermissionError):
            scribe.write(notes, "v0.2.0")
        self.assertFalse((self.root / "CHANGELOG.md").exists())

    def test_write_with_approval_writes_exact_content(self):
        agent = FakeAgent([NOTES_A])

        def factory(spec, **kwargs):
            return agent

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=factory,
        )
        notes = scribe.draft()
        content = scribe.render(notes, "v0.2.0")
        tools.approve_content(content)
        # The fake agent's run() must invoke the approval callback the way
        # the real runtime would for the write tool, then perform the write
        # through the real tool implementation.
        real_run = agent.run

        def run_with_approval(message, timeout_seconds=None,
                              approval_callback=None, **kw):
            if approval_callback is not None:
                approved = approval_callback(
                    "write_changelog",
                    {"repo_path": str(self.root), "content": content},
                    "approval-1",
                )
                assert approved, "callback must approve the approved content"
                # Simulate the runtime executing the tool after approval.
                tools.write_changelog_impl(str(self.root), content)
            return FakeResult(text="Wrote CHANGELOG.md")

        agent.run = run_with_approval
        confirmation = scribe.write(notes, "v0.2.0")
        self.assertIn("Wrote", confirmation)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(), content)

    def test_approval_callback_rejects_tampered_content(self):
        agent = FakeAgent([NOTES_A])
        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: agent,
        )
        notes = scribe.draft()
        content = scribe.render(notes, "v0.2.0")
        tools.approve_content(content)

        captured = {}

        def run_capture(message, timeout_seconds=None,
                        approval_callback=None, **kw):
            captured["cb"] = approval_callback
            return FakeResult(text="ok")

        agent.run = run_capture
        scribe.write(notes, "v0.2.0")
        cb = captured["cb"]
        self.assertIsNotNone(cb)
        self.assertTrue(cb("write_changelog",
                           {"content": content}, "a1"))
        self.assertFalse(cb("write_changelog",
                            {"content": content + "TAMPERED"}, "a1"))
        self.assertFalse(cb("read_commits", {"content": content}, "a1"))


class TestReviewer(unittest.TestCase):
    def setUp(self):
        tools.clear_approvals()
        self.root = make_repo([("feat: x", "a.py", "x")])
        tag(self.root, "v0.1.0", "HEAD")
        import os
        os.environ["RS_FAKE_KEY_A"] = "x"
        os.environ["RS_FAKE_KEY_B"] = "x"

    def tearDown(self):
        import os
        os.environ.pop("RS_FAKE_KEY_A", None)
        os.environ.pop("RS_FAKE_KEY_B", None)
        tools.clear_approvals()

    def test_reviewer_verdict_passes_through(self):
        parent = FakeAgent([NOTES_A])
        reviewer = FakeAgent([ReviewVerdict(verdict="ok", issues=[])])

        def subagent_factory(parent_agent, **kwargs):
            self.assertEqual(kwargs.get("name"), "reviewer")
            return reviewer

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: parent,
            subagent_factory=subagent_factory,
        )
        notes = scribe.draft()
        verdict = scribe.review(notes)
        self.assertEqual(verdict.verdict, "ok")
        self.assertEqual(verdict.issues, [])

    def test_reviewer_failure_is_not_fatal(self):
        parent = FakeAgent([NOTES_A])

        def failing_subagent(parent_agent, **kwargs):
            raise RuntimeError("subagent exploded")

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: parent,
            subagent_factory=failing_subagent,
        )
        notes = scribe.draft()
        verdict = scribe.review(notes)
        self.assertEqual(verdict.verdict, "issues_found")
        self.assertIn("could not run", verdict.issues[0])

    def test_reviewer_plain_text_answer_retried_as_json(self):
        """Reviewer saying plain "ok" gets one JSON retry before failing."""
        from openminion import AgentOutputValidationError

        parent = FakeAgent([NOTES_A])
        runs = {"n": 0}

        class RetryReviewer(FakeAgent):
            def run(self, message, timeout_seconds=None, **kw):
                runs["n"] += 1
                if runs["n"] == 1:
                    raise AgentOutputValidationError(
                        "not json", raw_text="ok"
                    )
                return FakeResult(
                    output=ReviewVerdict(verdict="ok", issues=[]),
                    text='{"verdict": "ok", "issues": []}',
                )

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: parent,
            subagent_factory=lambda p, **kw: RetryReviewer([]),
        )
        notes = scribe.draft()
        verdict = scribe.review(notes)
        self.assertEqual(verdict.verdict, "ok")
        self.assertEqual(runs["n"], 2)

    def test_reviewer_bare_ok_raw_text_counts_as_pass(self):
        """A reviewer that only ever says 'ok' in prose still passes."""
        from openminion import AgentOutputValidationError

        parent = FakeAgent([NOTES_A])

        class OkReviewer(FakeAgent):
            def run(self, message, timeout_seconds=None, **kw):
                raise AgentOutputValidationError(
                    "not json", raw_text="releasescribe: ok"
                )

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: parent,
            subagent_factory=lambda p, **kw: OkReviewer([]),
        )
        notes = scribe.draft()
        verdict = scribe.review(notes)
        self.assertEqual(verdict.verdict, "ok")
        self.assertEqual(verdict.issues, [])

    def test_reviewer_garbled_raw_text_reported_not_trusted(self):
        """A non-JSON, non-'ok' reviewer reply is surfaced, not swallowed."""
        from openminion import AgentOutputValidationError

        parent = FakeAgent([NOTES_A])

        class GarbledReviewer(FakeAgent):
            def run(self, message, timeout_seconds=None, **kw):
                raise AgentOutputValidationError(
                    "not json",
                    raw_text='{"summary": "something else entirely"}',
                )

        scribe = ReleaseScribe(
            self.root, "v0.1.0..HEAD", provider_chain=_chain(),
            agent_factory=lambda spec, **kw: parent,
            subagent_factory=lambda p, **kw: GarbledReviewer([]),
        )
        notes = scribe.draft()
        verdict = scribe.review(notes)
        self.assertEqual(verdict.verdict, "issues_found")
        self.assertIn("did not return a structured verdict", verdict.issues[0])


if __name__ == "__main__":
    unittest.main()
