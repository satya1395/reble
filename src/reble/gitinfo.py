"""Read-only git awareness (F14: "reble follows git").

Reble never runs a git command that mutates anything: this module only asks
where the checkout stands (branch, commit, dirty), and every failure mode —
no git binary, not a repo, detached HEAD, timeout — degrades to "no info",
never to an error. Git supplies identity and provenance; change detection
stays fingerprint-based (models.py) so uncommitted edits are always seen.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_S = 3


@dataclass(frozen=True)
class GitInfo:
    branch: str | None       # None on detached HEAD
    sha: str | None
    dirty: bool

    @property
    def short_sha(self) -> str | None:
        return self.sha[:7] if self.sha else None


def _git(project_dir: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_info(project_dir: Path) -> GitInfo | None:
    """Current git state for the repo containing `project_dir`, or None when
    there is no repo (or no usable git)."""
    inside = _git(project_dir, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return None
    branch = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":          # detached
        branch = None
    sha = _git(project_dir, "rev-parse", "HEAD")   # None in a repo with no commits
    status = _git(project_dir, "status", "--porcelain", "-uno")
    dirty = bool(status)
    return GitInfo(branch=branch, sha=sha, dirty=dirty)


def branchable_name(info: GitInfo | None) -> str | None:
    """The git branch name a data branch may follow: a real feature branch,
    never main/master/detached."""
    if info is None or info.branch in (None, "main", "master"):
        return None
    return info.branch
