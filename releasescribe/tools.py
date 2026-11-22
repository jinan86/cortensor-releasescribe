"""OpenMinion tools for ReleaseScribe.

Two tools, with deliberately different scopes:

* ``read_commits`` — READ_ONLY + idempotent. The model may call it freely;
  it only runs ``git log``.
* ``write_changelog`` — WRITE_SAFE, never idempotent in the policy sense:
  it writes CHANGELOG.md. It is guarded twice:

  1. In-process gate (this module): the exact content must have been
     approved by the operator in this process via :func:`approve_content`.
     Without that, the tool raises ``PermissionError`` and writes nothing.
     This is what the unit tests assert.
  2. OpenMinion policy gate: the CLI passes an ``approval_callback`` to
     ``Agent.run`` that only returns True for this tool when the same
     in-process approval exists. If the SDK's policy never consults the
     callback for this scope, gate (1) still blocks the write.

The tool functions never auto-approve. Approval state lives only in this
process and is never written to disk.
"""

from __future__ import annotations

import hashlib
import json

from openminion import tool

from . import gitops
from .models import CommitEntry

_APPROVED_HASHES: set[str] = set()


def _content_hash(content: str) -> str:
    """Hash of the normalised content (trailing whitespace ignored)."""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def approve_content(content: str) -> str:
    """Record operator approval for this exact changelog content.

    Returns the approval token (the content hash). The token is not a
    secret; it only identifies which content was approved.
    """
    token = _content_hash(content)
    _APPROVED_HASHES.add(token)
    return token


def content_approved(content: str) -> bool:
    """True when this exact content was approved in this process."""
    return _content_hash(content) in _APPROVED_HASHES


def clear_approvals() -> None:
    """Forget all approvals (used by tests)."""
    _APPROVED_HASHES.clear()


def read_commits_impl(repo_path: str, commit_range: str) -> str:
    """Read commits and return them as a JSON list (tool implementation)."""
    commits: list[CommitEntry] = gitops.read_commits(repo_path, commit_range)
    return json.dumps([c.model_dump() for c in commits], ensure_ascii=False)


def write_changelog_impl(repo_path: str, content: str) -> str:
    """Write CHANGELOG.md, but only for operator-approved content.

    Raises PermissionError when the content was not approved in this
    process — the tool never writes on the model's say-so alone.
    """
    if not content_approved(content):
        raise PermissionError(
            "write_changelog refused: this exact content was not approved "
            "by the operator in this session. Show the draft and ask for "
            "approval first."
        )
    target = gitops.write_changelog_file(repo_path, content)
    return f"Wrote {target} ({len(content)} bytes)."


@tool(min_scope="READ_ONLY", idempotent=True)
def read_commits(repo_path: str, commit_range: str) -> str:
    """Read the git commits in a range (e.g. "v0.1.0..HEAD") as JSON.

    Read-only: runs `git log` only and never modifies the repository.
    Returns a JSON list of {sha, subject, body, author, date}.
    """
    return read_commits_impl(repo_path, commit_range)


@tool(min_scope="WRITE_SAFE", idempotent=False)
def write_changelog(repo_path: str, content: str) -> str:
    """Write the given Markdown content to CHANGELOG.md at the repo root.

    WRITE scope: the runtime must ask the operator for approval before this
    tool runs, and the tool itself additionally refuses unless the exact
    content was approved in this process. Never call with content the
    operator has not seen and approved.
    """
    return write_changelog_impl(repo_path, content)
