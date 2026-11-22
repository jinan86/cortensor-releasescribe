"""ReleaseScribe orchestration: draft -> review -> refine -> approved write.

The provider fallback chain lives in :mod:`providers`. This module owns the
OpenMinion usage: one ``Agent`` per turn, a stable ``session_id`` per
repo+range, typed output (``ReleaseNotes``), the reviewer subagent, and the
two approval gates for the write (SDK ``approval_callback`` + the
in-process content gate in :mod:`tools`).

For testability the OpenMinion ``Agent`` class is injected through
``agent_factory``; unit tests pass a fake. Production passes the real one.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from openminion import (
    Agent,
    AgentOutputValidationError,
    ProviderError,
    TurnTimeoutError,
    subagent,
)

from . import gitops, providers, tools
from .models import ReleaseNotes, ReviewVerdict
from .providers import ProviderSpec

DRAFT_INSTRUCTIONS = """You draft structured release notes from git history.

Rules:
- FIRST call the read_commits tool with the exact repo path and range given
  in the user message. Do not guess commits.
- Group the commits into features, fixes, breaking changes, and notes.
- EVERY bullet must be traceable to at least one commit you read. Never
  invent a feature, fix, or breaking change that no commit describes.
- If there are no breaking changes, leave `breaking` empty.
- Keep the summary to one or two sentences.
- OUTPUT FORMAT (mandatory): your final reply must be ONLY a single JSON
  object matching the ReleaseNotes schema, with keys summary, features,
  fixes, breaking, notes. No markdown headings, no bullet list, no code
  fences, no prose before or after the JSON object. A reply in any other
  format is a failure.
"""

JSON_RETRY_MESSAGE = (
    "Your last reply was not a valid JSON object for the ReleaseNotes "
    "schema, so it could not be used. Reply again with ONLY the JSON "
    "object (keys: summary, features, fixes, breaking, notes) and nothing "
    "else — no markdown, no fences, no prose."
)

REVIEWER_INSTRUCTIONS = """You review a draft of release notes against the raw git history.

Rules:
- Call the read_commits tool with the exact repo path and range given in
  the user message, and compare EVERY draft bullet against those commits.
- An item is a hallucination when no commit supports it. Also flag commits
  that clearly belong in the notes but the draft missed entirely.
- Return ONLY the structured verdict: "ok" when everything checks out,
  "issues_found" with one issue string per problem otherwise.
"""

WRITE_INSTRUCTIONS = """You write the approved CHANGELOG.md entry.

Rules:
- Call the write_changelog tool EXACTLY ONCE with the repo path and the
  exact Markdown content given in the user message.
- Do NOT alter, shorten, reword, or reformat the content in any way.
- Do NOT write anything else. After the tool confirms, reply with one
  short confirmation sentence.
"""

# A turn that produces a draft may take a while on a slow free model
# (Ernie reasons ~1000 tokens before answering).
DRAFT_TIMEOUT_SECONDS = 180.0
REFINE_TIMEOUT_SECONDS = 180.0
WRITE_TIMEOUT_SECONDS = 120.0


def derive_session_id(repo_path: str | Path, commit_range: str) -> str:
    """Stable session id per repo+range, so refinements share history."""
    root = str(Path(repo_path).expanduser().resolve())
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in commit_range)
    return f"releasescribe-{digest}-{slug}"


class ReleaseScribe:
    """One release-notes run for a repo + commit range."""

    def __init__(
        self,
        repo_path: str | Path,
        commit_range: str,
        *,
        use_reviewer: bool = True,
        provider_chain: tuple[ProviderSpec, ...] = providers.PROVIDER_CHAIN,
        agent_factory: Callable[..., Any] | None = None,
        subagent_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.repo_path = str(gitops.resolve_repo(repo_path))
        self.commit_range = gitops.validate_range(commit_range)
        self.use_reviewer = use_reviewer
        self.provider_chain = provider_chain
        self.agent_factory = agent_factory or self._real_agent_factory
        self.subagent_factory = subagent_factory or subagent
        self.session_id = derive_session_id(self.repo_path, self.commit_range)
        self.attempts: list[dict[str, str]] = []
        self.provider_used: str | None = None
        # Every invocation of the SDK approval_callback during a write turn.
        # Recorded so the run can report whether the runtime policy actually
        # consulted the callback (vs the in-process gate doing the work).
        self.approval_callbacks_seen: list[dict[str, object]] = []
        self._config_dirs: list[tempfile.TemporaryDirectory] = []
        self._config_paths: dict[str, str] = {}
        self._active_spec: ProviderSpec | None = None
        # Draft/refine agents are kept open for the run's lifetime, one per
        # provider spec. Session continuity in 0.1.22 only holds WITHIN one
        # Agent instance: a second Agent with the same session_id and the
        # same config dir does NOT see the first one's history (verified by
        # probe 22 Nov 2026 — see BUILDING_BLOCKS.md). So draft and refine
        # share one live instance; the write turn (different tools/output)
        # gets its own short-lived agent and is passed everything explicitly.
        self._agent_contexts: dict[str, Any] = {}
        self._agents: dict[str, Any] = {}

    # -- provider plumbing -------------------------------------------------

    def _config_path_for(self, spec: ProviderSpec) -> str:
        """One config dir per provider, shared across turns.

        Sharing the config dir shares the runtime state DB. That alone does
        NOT restore history across Agent instances (see the note above), but
        it keeps storage, tool registration, and identity consistent.
        """
        if spec.name not in self._config_paths:
            config_dir = tempfile.TemporaryDirectory(prefix="releasescribe-cfg-")
            self._config_dirs.append(config_dir)
            self._config_paths[spec.name] = str(
                providers.write_config_file(spec, config_dir.name)
            )
        return self._config_paths[spec.name]

    def _real_agent_factory(self, spec: ProviderSpec, **kwargs: Any) -> Agent:
        return Agent(
            config_path=self._config_path_for(spec),
            session_id=self.session_id,
            **kwargs,
        )

    def _draft_agent(self, spec: ProviderSpec) -> Any:
        """Return the live draft/refine agent for ``spec`` (creating it)."""
        if spec.name not in self._agents:
            ctx = self.agent_factory(
                spec,
                instructions=DRAFT_INSTRUCTIONS,
                tools=[tools.read_commits],
                forced_tools=["read_commits"],
                output_type=ReleaseNotes,
            )
            self._agents[spec.name] = ctx.__enter__()
            self._agent_contexts[spec.name] = ctx
        return self._agents[spec.name]

    def _evict_agent(self, spec: ProviderSpec) -> None:
        """Drop a cached agent (e.g. after a provider failure)."""
        self._agents.pop(spec.name, None)
        ctx = self._agent_contexts.pop(spec.name, None)
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _prepare_spec(self, spec: ProviderSpec) -> bool:
        """True when ``spec`` can be attempted (credentials available)."""
        if not providers.ensure_credentials(spec):
            self.attempts.append(
                {"provider": spec.name, "outcome": "skipped: no credentials"}
            )
            return False
        return True

    def close(self) -> None:
        for spec_name in list(self._agent_contexts):
            ctx = self._agent_contexts.pop(spec_name)
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        self._agents.clear()
        for tmp in self._config_dirs:
            tmp.cleanup()
        self._config_dirs.clear()
        self._config_paths.clear()

    # -- drafting ----------------------------------------------------------

    def draft(self) -> ReleaseNotes:
        """Draft release notes, trying each provider in turn on failure.

        A typed-output validation failure is retried once in the same
        session (the model is told its reply was not valid JSON) before
        moving to the next provider — free models often need the nudge.
        Raises RuntimeError when every provider in the chain fails.
        """
        message = (
            f"Draft release notes for the git repository at {self.repo_path} "
            f"for the commit range {self.commit_range!r}. "
            "Use the read_commits tool with exactly that repo path and range."
        )
        last_error: Exception | None = None
        for spec in self.provider_chain:
            if not self._prepare_spec(spec):
                continue
            try:
                agent = self._draft_agent(spec)
                try:
                    result = agent.run(
                        message, timeout_seconds=DRAFT_TIMEOUT_SECONDS
                    )
                except AgentOutputValidationError:
                    # One in-session retry: the model already has the
                    # commits in context, so this is cheap.
                    self.attempts.append(
                        {
                            "provider": spec.name,
                            "outcome": "invalid typed output, retrying",
                        }
                    )
                    result = agent.run(
                        JSON_RETRY_MESSAGE,
                        timeout_seconds=DRAFT_TIMEOUT_SECONDS,
                    )
            except (ProviderError, TurnTimeoutError) as exc:
                self._evict_agent(spec)
                self.attempts.append(
                    {
                        "provider": spec.name,
                        "outcome": f"failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]}",
                    }
                )
                last_error = exc
                continue
            except AgentOutputValidationError as exc:
                self.attempts.append(
                    {
                        "provider": spec.name,
                        "outcome": "failed: invalid typed output: "
                        f"{str(exc)[:200]}",
                    }
                )
                last_error = exc
                continue
            self.provider_used = spec.name
            self._active_spec = spec
            self.attempts.append({"provider": spec.name, "outcome": "draft ok"})
            notes = result.output
            if not isinstance(notes, ReleaseNotes):
                raise RuntimeError("draft turn did not return ReleaseNotes")
            return notes
        raise RuntimeError(
            "all providers failed to draft release notes"
            + (f": {last_error}" if last_error else "")
        )

    def refine(self, feedback: str, current: ReleaseNotes | None = None) -> ReleaseNotes:
        """Refine the draft in the same session (multi-turn continuity).

        The current draft is included in the message itself, so the turn
        works even if the runtime's session history is unavailable; the
        shared ``session_id`` additionally gives the model the earlier
        turns. A validation failure gets one in-session JSON retry.
        """
        if self._active_spec is None:
            raise RuntimeError("refine() called before a successful draft()")
        draft_json = (
            current.model_dump_json(indent=2)
            if current is not None
            else "(see the earlier turns in this session)"
        )
        message = (
            f"Refinement request: {feedback}\n\n"
            f"Current draft release notes JSON:\n{draft_json}\n\n"
            f"Repo: {self.repo_path}, range: {self.commit_range!r}. "
            "If you need the commits again, call read_commits with exactly "
            "that repo path and range. Update the release notes accordingly "
            "and return the FULL updated notes as ONLY a JSON object "
            "(keys: summary, features, fixes, breaking, notes). Keep every "
            "item backed by a commit in the range."
        )
        # Same live agent instance as draft(): this is what gives the refine
        # turn the earlier conversation. A fresh Agent would start blank
        # (see the note on _agents).
        agent = self._draft_agent(self._active_spec)
        try:
            result = agent.run(message, timeout_seconds=REFINE_TIMEOUT_SECONDS)
        except AgentOutputValidationError:
            result = agent.run(
                JSON_RETRY_MESSAGE,
                timeout_seconds=REFINE_TIMEOUT_SECONDS,
            )
        notes = result.output
        if not isinstance(notes, ReleaseNotes):
            raise RuntimeError("refine turn did not return ReleaseNotes")
        return notes

    # -- review (subagent, stretch) -----------------------------------------

    def review(self, notes: ReleaseNotes) -> ReviewVerdict:
        """Ask the reviewer subagent to check the draft against raw commits.

        Never fatal: a reviewer failure returns a verdict noting the review
        could not run, and the operator still sees the draft.
        """
        if self._active_spec is None:
            raise RuntimeError("review() called before a successful draft()")
        message = (
            f"Review this draft release notes JSON for the git repository at "
            f"{self.repo_path} for the commit range {self.commit_range!r}:\n\n"
            f"{notes.model_dump_json(indent=2)}\n\n"
            "Use the read_commits tool with exactly that repo path and range."
        )
        try:
            parent = self._draft_agent(self._active_spec)
            reviewer = self.subagent_factory(
                parent,
                instructions=REVIEWER_INSTRUCTIONS,
                tools=[tools.read_commits],
                output_type=ReviewVerdict,
                name="reviewer",
                timeout_seconds=120,
            )
            with reviewer:
                try:
                    result = reviewer.run(
                        message, timeout_seconds=REFINE_TIMEOUT_SECONDS
                    )
                except AgentOutputValidationError as exc:
                    # Free models often answer "ok" in prose, or echo a
                    # ReleaseNotes-shaped object (the parent's schema leaks
                    # into the child), instead of the ReviewVerdict schema.
                    # Retry once for JSON; if it still fails, read the raw
                    # text before giving up.
                    try:
                        result = reviewer.run(
                            JSON_RETRY_MESSAGE,
                            timeout_seconds=REFINE_TIMEOUT_SECONDS,
                        )
                    except AgentOutputValidationError as retry_exc:
                        return self._verdict_from_raw_text(retry_exc.raw_text)
                    if not isinstance(result.output, ReviewVerdict):
                        return self._verdict_from_raw_text(exc.raw_text)
            verdict = result.output
            if not isinstance(verdict, ReviewVerdict):
                raise RuntimeError("reviewer did not return a ReviewVerdict")
            return verdict
        except Exception as exc:  # noqa: BLE001 - reviewer is best-effort
            return ReviewVerdict(
                verdict="issues_found",
                issues=[f"reviewer could not run: {type(exc).__name__}: {exc}"],
            )

    @staticmethod
    def _verdict_from_raw_text(raw_text: str) -> ReviewVerdict:
        """Best-effort verdict from a non-JSON reviewer reply.

        A bare "ok" (the runtime prefixes replies with "agent: ") counts as
        a pass. Anything else is reported as an issue with the raw text, so
        the operator can judge it instead of silently trusting the draft.
        """
        text = (raw_text or "").strip()
        for prefix in ("releasescribe:", "reviewer:"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
        if text.lower().strip("`\"' ") == "ok":
            return ReviewVerdict(verdict="ok", issues=[])
        snippet = (raw_text or "")[:300]
        return ReviewVerdict(
            verdict="issues_found",
            issues=[
                "reviewer did not return a structured verdict; raw reply: "
                f"{snippet}"
            ],
        )

    # -- approved write ------------------------------------------------------

    def write(self, notes: ReleaseNotes, version_label: str) -> str:
        """Write CHANGELOG.md after operator approval.

        The caller (CLI) must have shown the draft to the operator and
        recorded approval via :func:`tools.approve_content` for the exact
        rendered content. This method additionally passes an SDK
        ``approval_callback`` that only approves the write_changelog tool
        when that in-process approval exists.
        """
        if self._active_spec is None:
            raise RuntimeError("write() called before a successful draft()")
        content = gitops.render_changelog(notes, version_label)
        if not tools.content_approved(content):
            raise PermissionError(
                "write() refused: the rendered changelog was not approved "
                "by the operator in this session."
            )
        message = (
            f"Write the approved CHANGELOG.md entry for the git repository "
            f"at {self.repo_path}.\n\n"
            f"Repo path: {self.repo_path}\n"
            f"Content (use EXACTLY this text):\n```markdown\n{content}```"
        )

        def approval_callback(tool_name: str, args: dict, approval_id: str) -> bool:
            # SDK policy gate: approve only the write tool, only for the
            # exact approved content, only in this session.
            approved = tool_name == "write_changelog" and tools.content_approved(
                str(args.get("content", ""))
            )
            self.approval_callbacks_seen.append(
                {"tool": tool_name, "approved": approved,
                 "approval_id": approval_id}
            )
            return approved

        last_error: Exception | None = None
        specs = [self._active_spec] + [
            s for s in self.provider_chain if s is not self._active_spec
        ]
        for spec in specs:
            if not self._prepare_spec(spec):
                continue
            try:
                with self.agent_factory(
                    spec,
                    instructions=WRITE_INSTRUCTIONS,
                    tools=[tools.write_changelog],
                    forced_tools=["write_changelog"],
                    output_type=None,
                ) as agent:
                    result = agent.run(
                        message,
                        timeout_seconds=WRITE_TIMEOUT_SECONDS,
                        approval_callback=approval_callback,
                    )
            except (ProviderError, TurnTimeoutError) as exc:
                self.attempts.append(
                    {
                        "provider": spec.name,
                        "outcome": f"write failed: {type(exc).__name__}",
                    }
                )
                last_error = exc
                continue
            self.attempts.append({"provider": spec.name, "outcome": "write ok"})
            return result.text
        raise RuntimeError(
            "all providers failed to perform the approved write"
            + (f": {last_error}" if last_error else "")
        )

    # -- helpers ---------------------------------------------------------------

    def render(self, notes: ReleaseNotes, version_label: str) -> str:
        """Render notes to Markdown (what the operator approves)."""
        return gitops.render_changelog(notes, version_label)

    def raw_commits_json(self) -> str:
        """Raw commits for the range, for transcripts / verification."""
        commits = gitops.read_commits(self.repo_path, self.commit_range)
        return json.dumps([c.model_dump() for c in commits], ensure_ascii=False)
