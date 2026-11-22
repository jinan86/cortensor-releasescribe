"""Shared test helpers: build a throwaway git repo with known commits."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def make_repo(commits: list[tuple[str, str, str]]) -> Path:
    """Create a temp git repo.

    ``commits`` is a list of (message, filename, content) tuples, one
    commit each, with fixed dates so SHAs are deterministic.
    Returns the repo path. The caller owns cleanup via the TemporaryDirectory.
    """
    tmp = tempfile.mkdtemp(prefix="rs-test-repo-")
    root = Path(tmp)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True
    )
    env = dict(os.environ)
    for i, (message, filename, content) in enumerate(commits):
        stamp = f"2026-01-0{i + 1}T10:00:00Z"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        (root / filename).write_text(content + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", filename], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", message],
            check=True,
            env=env,
        )
    return root


def tag(root: Path, name: str, rev: str = "HEAD") -> None:
    subprocess.run(["git", "-C", str(root), "tag", name, rev], check=True)
