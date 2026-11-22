#!/usr/bin/env python3
"""ReleaseScribe CLI — draft release notes with OpenMinion, write only on approval.

Usage:
    python app.py <repo-path> <range> [--version-label LABEL] [--provider NAME]
               [--no-reviewer] [--transcript FILE]

Example:
    python app.py ./fixtures/sample-repo/repo v0.1.0..HEAD

The agent reads the commits in <range>, drafts structured release notes,
shows the draft, and asks the operator what to do. CHANGELOG.md is written
ONLY after the operator explicitly approves the exact rendered draft
(`approve`), and the write still passes through OpenMinion's approval
callback. Answer `refine <text>` to ask for changes in the same session,
or `quit` to stop without writing anything.

API keys come from the environment only (OPENROUTER_API_KEY /
OPENAI_COMPATIBLE_API_KEY). Never paste a key into this file, the repo, or
the transcript. On this machine the Secure Vault surrogate is picked up
automatically when the env var is unset; see releasescribe/providers.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from releasescribe import agent as agent_mod
from releasescribe import gitops, providers, tools
from releasescribe.agent import ReleaseScribe
from releasescribe.models import ReleaseNotes

try:
    from openminion import AgentOutputValidationError
except ImportError:  # pragma: no cover - SDK is a hard requirement
    AgentOutputValidationError = Exception  # type: ignore[assignment]


def derive_version_label(commit_range: str) -> str:
    """Version label for the CHANGELOG heading from the range end."""
    end = commit_range.split("..")[-1]
    if end in ("HEAD", "") or len(end) == 40:
        return "Unreleased"
    return end


class Transcript:
    """Append-only run log (never contains keys or surrogates)."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None

    def log(self, text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{stamp}] {text}"
        print(line)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def show_draft(notes: ReleaseNotes, version_label: str) -> str:
    """Print the rendered draft and return its Markdown."""
    content = gitops.render_changelog(notes, version_label)
    print()
    print("=" * 60)
    print("DRAFT CHANGELOG ENTRY")
    print("=" * 60)
    print(content)
    print("=" * 60)
    return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="releasescribe",
        description="Draft release notes from git history; write CHANGELOG.md "
        "only after operator approval.",
    )
    parser.add_argument("repo", help="Path to the local git repository.")
    parser.add_argument(
        "range", help='Commit range, e.g. "v0.1.0..HEAD".'
    )
    parser.add_argument(
        "--version-label",
        default=None,
        help="Heading for the CHANGELOG entry (default: range end or "
        '"Unreleased").',
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Use only one provider rung (substring of its name, e.g. "
        '"openrouter" or "100milionseek"). Default: try the whole chain.',
    )
    parser.add_argument(
        "--no-reviewer", action="store_true",
        help="Skip the reviewer subagent.",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="Append a timestamped run log (no secrets) to this file.",
    )
    args = parser.parse_args(argv)

    transcript = Transcript(args.transcript)
    providers.sanitize_no_proxy()

    chain = providers.PROVIDER_CHAIN
    if args.provider:
        chain = tuple(s for s in chain if args.provider.lower() in s.name.lower())
        if not chain:
            print(f"error: no provider matches {args.provider!r}", file=sys.stderr)
            return 1

    version_label = args.version_label or derive_version_label(args.range)
    transcript.log(
        f"ReleaseScribe start: repo={args.repo} range={args.range} "
        f"version={version_label}"
    )

    try:
        scribe = ReleaseScribe(
            args.repo,
            args.range,
            use_reviewer=not args.no_reviewer,
            provider_chain=chain,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        transcript.log(f"session_id={scribe.session_id}")
        commits = gitops.read_commits(scribe.repo_path, scribe.commit_range)
        transcript.log(f"commits in range: {len(commits)}")
        for commit in commits:
            transcript.log(f"  {commit.sha[:8]} {commit.subject}")

        transcript.log("drafting release notes (model turn)...")
        notes = scribe.draft()
        transcript.log(f"draft ready via provider: {scribe.provider_used}")

        if scribe.use_reviewer:
            transcript.log("reviewer subagent checking draft vs raw commits...")
            verdict = scribe.review(notes)
            transcript.log(f"reviewer verdict: {verdict.verdict}")
            for issue in verdict.issues:
                transcript.log(f"  issue: {issue}")
            if verdict.verdict != "ok":
                print("Reviewer flagged issues (see transcript). "
                      "You can still refine or approve.")

        content = show_draft(notes, version_label)

        while True:
            try:
                answer = input(
                    "Approve this draft? [approve / refine <text> / quit] "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted without writing anything.")
                return 2
            if answer.lower() == "quit":
                transcript.log("operator quit without writing.")
                print("Quit without writing CHANGELOG.md.")
                return 2
            if answer.lower() == "approve":
                break
            if answer.lower().startswith("refine "):
                feedback = answer[len("refine "):].strip()
                if not feedback:
                    print("Give refinement text after 'refine'.")
                    continue
                transcript.log(f"refining in-session: {feedback}")
                try:
                    notes = scribe.refine(feedback, current=notes)
                except AgentOutputValidationError as exc:
                    transcript.log(
                        "refine turn did not return valid release notes "
                        f"(kept previous draft): {str(exc)[:200]}"
                    )
                    print("The refine turn did not return valid notes; "
                          "keeping the previous draft. Try again, or approve.")
                    continue
                transcript.log("refined draft ready.")
                content = show_draft(notes, version_label)
                continue
            print("Answer 'approve', 'refine <text>', or 'quit'.")

        tools.approve_content(content)
        transcript.log("operator approved the exact draft above.")
        transcript.log("writing CHANGELOG.md (approved write turn)...")
        confirmation = scribe.write(notes, version_label)
        transcript.log(f"write turn: {confirmation}")
        # Post-write verification: the model's closing text is not evidence.
        # In one live run the model claimed the content field had been empty
        # and that it "filled it in from inferred changes" — while the file
        # actually contained the approved draft byte-for-byte. Trust the
        # file, not the narration: compare it to the approved content.
        on_disk = gitops.read_changelog_file(scribe.repo_path)
        if on_disk is None:
            transcript.log("VERIFICATION FAILED: CHANGELOG.md does not exist "
                           "after the write turn.")
            print("ERROR: CHANGELOG.md was not created. See transcript.")
            return 1
        if on_disk.strip() != content.strip():
            transcript.log("VERIFICATION FAILED: CHANGELOG.md on disk does "
                           "NOT match the approved draft. Do not trust it.")
            print("ERROR: CHANGELOG.md on disk differs from the approved "
                  "draft. See transcript.")
            return 1
        transcript.log("VERIFIED: CHANGELOG.md on disk matches the approved "
                       "draft byte-for-byte.")
        transcript.log("CHANGELOG.md written.")
        print()
        print(f"Done. CHANGELOG.md updated in {scribe.repo_path}")
        print(f"Provider used: {scribe.provider_used}")
        print("Provider attempts:")
        for attempt in scribe.attempts:
            print(f"  - {attempt['provider']}: {attempt['outcome']}")
        print("SDK approval_callback invocations during the write turn:")
        if scribe.approval_callbacks_seen:
            for seen in scribe.approval_callbacks_seen:
                print(f"  - tool={seen['tool']} approved={seen['approved']}")
        else:
            print("  - none (the runtime policy did not consult the callback; "
                  "the in-process content gate enforced approval instead)")
        return 0
    except (RuntimeError, PermissionError) as exc:
        transcript.log(f"FAILED: {type(exc).__name__}: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - provider/SDK failures, report cleanly
        transcript.log(f"FAILED: {type(exc).__name__}: {str(exc)[:300]}")
        print(f"error: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1
    finally:
        try:
            scribe.close()
        except NameError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
