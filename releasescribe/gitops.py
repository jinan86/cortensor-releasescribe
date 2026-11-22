"""Pure git operations for ReleaseScribe.

These functions have no OpenMinion dependency, so they are unit-testable
without a model. The ``@tool``-decorated wrappers live in ``tools.py``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import CommitEntry, ReleaseNotes

# A range is either a single ref or ``A..B``. Refs may contain word chars,
# dots, slashes, tildes, carets and @ (e.g. v0.1.0, HEAD~3, main). Anything
# else (spaces, shell metacharacters, leading dashes) is rejected so a
# caller can never smuggle a git flag or shell fragment through the range.
_RANGE_RE = re.compile(r"^[\w./~^@][\w./~^@-]*(\.\.[\w./~^@][\w./~^@-]*)?$")

# git log format: unit separator between fields, record separator between
# commits. Both are control characters that cannot appear in a subject line.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_LOG_FORMAT = _FIELD_SEP.join(["%H", "%s", "%b", "%an", "%ad"]) + _RECORD_SEP


def validate_range(commit_range: str) -> str:
    """Return the range unchanged, or raise ValueError when it is unsafe."""
    if not isinstance(commit_range, str) or not _RANGE_RE.match(commit_range):
        raise ValueError(f"invalid commit range: {commit_range!r}")
    return commit_range


def resolve_repo(repo_path: str | Path) -> Path:
    """Return the repo root for a local path, or raise ValueError."""
    path = Path(repo_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a directory: {repo_path}")
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ValueError(f"not a git repository: {repo_path}")
    return Path(proc.stdout.strip())


def read_commits(repo_path: str | Path, commit_range: str) -> list[CommitEntry]:
    """Return the commits in ``commit_range`` (e.g. ``v0.1.0..HEAD``).

    Read-only and idempotent: runs ``git log`` only, never writes.
    """
    root = resolve_repo(repo_path)
    validate_range(commit_range)
    proc = subprocess.run(
        ["git", "-C", str(root), "log", commit_range, f"--format={_LOG_FORMAT}",
         "--date=short"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git log failed for range {commit_range!r}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    commits: list[CommitEntry] = []
    for record in proc.stdout.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) != 5:
            continue
        sha, subject, body, author, date = fields
        commits.append(
            CommitEntry(
                sha=sha.strip(),
                subject=subject.strip(),
                body=body.strip(),
                author=author.strip(),
                date=date.strip(),
            )
        )
    return commits


def render_changelog(notes: ReleaseNotes, version_label: str) -> str:
    """Render structured notes as a Markdown CHANGELOG entry."""
    lines = [f"## {version_label}", "", notes.summary.strip(), ""]

    def _section(title: str, items: list[str]) -> None:
        if not items:
            return
        lines.append(f"### {title}")
        lines.append("")
        for item in items:
            lines.append(f"- {item.strip()}")
        lines.append("")

    _section("Features", notes.features)
    _section("Fixes", notes.fixes)
    _section("Breaking changes", notes.breaking)
    _section("Notes", notes.notes)
    return "\n".join(lines).rstrip() + "\n"


def write_changelog_file(repo_path: str | Path, content: str) -> Path:
    """Write ``content`` to CHANGELOG.md at the repo root.

    Always replaces the whole file with the rendered release entry (the
    agent drafts one release at a time). Refuses empty content.
    """
    if not content or not content.strip():
        raise ValueError("refusing to write an empty CHANGELOG.md")
    root = resolve_repo(repo_path)
    target = root / "CHANGELOG.md"
    target.write_text(content, encoding="utf-8")
    return target


def read_changelog_file(repo_path: str | Path) -> str | None:
    """Return the current CHANGELOG.md content, or None when absent."""
    root = resolve_repo(repo_path)
    target = root / "CHANGELOG.md"
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")
