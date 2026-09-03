"""Machine-local state under .reble/ (spec section 2).

state.json — git-branch ↔ data-branch mapping, branch epochs, promote progress.
Everything here is machine-local and gitignored; two engineers on the same git
branch get separate data branches on catalog name collision.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Pin:
    table: str  # full relation name, e.g. raw.stg_orders
    tag: str  # Iceberg tag name
    snapshot_id: int
    base_snapshot_id: int  # main head at branch epoch — the drift reference


@dataclass
class BranchState:
    git_branch: str
    data_branch: str
    base_ref: str = "main"
    base_commit: str | None = None
    epoch: float = field(default_factory=time.time)  # branch creation moment (invariant 5)
    created_at: str = ""
    user_suffix: str | None = None
    # How the state key was derived: "git" (git_sync adapter), "explicit"
    # (--change-set flag), "env" (REBLE_CHANGE_SET). Additive field.
    key_source: str = "git"
    model_hashes: dict[str, str] = field(default_factory=dict)  # last-run AST hashes
    scope: list[str] = field(default_factory=list)
    pins: dict[str, Pin] = field(default_factory=dict)  # relation -> Pin
    base_heads: dict[str, int] = field(default_factory=dict)  # scope table -> main head at last run
    last_run_id: str | None = None
    promote_in_progress: bool = False


@dataclass
class State:
    branches: dict[str, BranchState] = field(default_factory=dict)  # key: git branch name


class StateStore:
    def __init__(self, reble_dir: Path):
        self.reble_dir = reble_dir
        self.path = reble_dir / "state.json"

    def load(self) -> State:
        if not self.path.exists():
            return State()
        raw = json.loads(self.path.read_text())
        branches: dict[str, BranchState] = {}
        for key, b in raw.get("branches", {}).items():
            pins = {t: Pin(**p) for t, p in b.pop("pins", {}).items()}
            branches[key] = BranchState(pins=pins, **b)
        return State(branches=branches)

    def save(self, state: State) -> None:
        """Atomic write (tmp + rename) so a crash mid-save can never leave
        a truncated state.json — the file readers see is always complete."""
        import os
        import tempfile

        self.reble_dir.mkdir(parents=True, exist_ok=True)
        data = {"branches": {k: asdict(v) for k, v in state.branches.items()}}
        fd, tmp = tempfile.mkstemp(dir=self.reble_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp, self.path)  # atomic on POSIX and Windows
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
