"""Read-only git access. Invariant 1: Reble reads git, never runs git.

`git_sync: false` must make reble 100% git-ignorant — callers must not
invoke these functions when git_sync is disabled.
"""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path


def _git(args: list[str], repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # Only the trailing newline: stripping all whitespace would eat the
    # leading status column of `status --porcelain` output.
    return result.stdout.rstrip("\n") or None


@cache
def repo_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return Path(result.stdout.strip())


def current_branch(repo: Path) -> str | None:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def head_commit(repo: Path) -> str | None:
    return _git(["rev-parse", "HEAD"], repo)


def base_commit(repo: Path, base_branches: tuple[str, ...] = ("main", "master")) -> str | None:
    """Merge-base of HEAD with the first existing base branch (read-only)."""
    for base in base_branches:
        commit = _git(["merge-base", "HEAD", base], repo)
        if commit:
            return commit
    return None


def file_at(repo: Path, commit: str, path: str) -> str | None:
    """File contents at a commit; None if the file did not exist."""
    return _git(["show", f"{commit}:{path}"], repo)


def changed_files(repo: Path, commit: str) -> list[str]:
    """Working tree + staged changes vs a commit (read-only)."""
    out = _git(["diff", "--name-only", commit], repo)
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def uncommitted_files(repo: Path) -> list[str]:
    out = _git(["status", "--porcelain"], repo)
    if not out:
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]
