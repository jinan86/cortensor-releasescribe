"""Typed data models for ReleaseScribe.

``ReleaseNotes`` is the Pydantic typed output of the drafting agent
(OpenMinion ``output_type=``). ``CommitEntry`` is the structured form of one
``git log`` record passed to the model through the ``read_commits`` tool.
``ReviewVerdict`` is the typed output of the reviewer subagent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CommitEntry(BaseModel):
    """One commit, as returned by the read_commits tool."""

    sha: str
    subject: str
    body: str = ""
    author: str = ""
    date: str = ""


class ReleaseNotes(BaseModel):
    """Structured release notes drafted by the agent.

    Every item must be traceable to a commit in the requested range.
    The agent must not invent features, fixes, or breaking changes that no
    commit describes.
    """

    summary: str = Field(
        description="One or two sentences summarising this release."
    )
    features: list[str] = Field(
        default_factory=list,
        description="New user-visible features, one short bullet each.",
    )
    fixes: list[str] = Field(
        default_factory=list,
        description="Bug fixes, one short bullet each.",
    )
    breaking: list[str] = Field(
        default_factory=list,
        description="Breaking changes, one short bullet each. Empty when none.",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Anything else worth calling out (deps, docs, chores).",
    )


class ReviewVerdict(BaseModel):
    """Typed output of the reviewer subagent."""

    verdict: str = Field(
        description='"ok" when every draft item is backed by a commit, '
        '"issues_found" otherwise.'
    )
    issues: list[str] = Field(
        default_factory=list,
        description="One entry per draft item that is not backed by any commit, "
        "or any commit the draft missed entirely.",
    )
