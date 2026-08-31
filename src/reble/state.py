"""Branch manifests and current-branch pointer, in a project-local SQLite db.

The state db is deliberately separate from the Iceberg catalog db: the catalog is
the (potentially shared) source of truth for table data; .reble/state.db is this
checkout's view of branches. In team mode the same schema moves to Postgres.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from reble.errors import BranchError

MAIN = "main"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    name        TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,      -- JSON list of table idents (writable)
    pins        TEXT NOT NULL,      -- JSON {table ident: snapshot_id}
    base        TEXT NOT NULL,      -- JSON {table ident: snapshot_id at creation}
    created_at  REAL NOT NULL,
    ttl_days    INTEGER NOT NULL,
    open_scope  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    branch TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published (
    ctx TEXT NOT NULL,      -- 'main' or a branch name
    tbl TEXT NOT NULL,
    fp  TEXT NOT NULL,      -- sqlmesh snapshot identifier last published there
    PRIMARY KEY (ctx, tbl)
);
"""


@dataclass
class BranchManifest:
    name: str
    scope: list[str]                 # writable tables (iceberg refs named after branch)
    pins: dict[str, int]             # read-only tables -> pinned snapshot id
    base: dict[str, int]             # scoped tables -> main snapshot at creation (for clean/dirty)
    created_at: float
    ttl_days: int
    open_scope: bool = False    # branch-first: scope grows at run time
    # git provenance (F14): what code produced this branch's data. Recorded at
    # create and refreshed on every run; None for non-git projects.
    git_branch: str | None = None
    git_sha: str | None = None
    git_dirty: bool = False
    last_run_at: float | None = None
    last_run_sha: str | None = None


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        for ddl in (   # additive migrations for older state dbs
            "ALTER TABLE branches ADD COLUMN open_scope INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE branches ADD COLUMN git_branch TEXT",
            "ALTER TABLE branches ADD COLUMN git_sha TEXT",
            "ALTER TABLE branches ADD COLUMN git_dirty INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE branches ADD COLUMN last_run_at REAL",
            "ALTER TABLE branches ADD COLUMN last_run_sha TEXT",
        ):
            try:
                self._db.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self._db.execute(
            "INSERT OR IGNORE INTO current (id, branch) VALUES (1, ?)", (MAIN,)
        )
        self._db.commit()

    # -- current branch pointer ------------------------------------------------
    def current_branch(self) -> str:
        return self._db.execute("SELECT branch FROM current WHERE id = 1").fetchone()[0]

    def set_current(self, name: str) -> None:
        if name != MAIN and self.get(name) is None:
            raise BranchError(f"branch {name!r} does not exist")
        self._db.execute("UPDATE current SET branch = ? WHERE id = 1", (name,))
        self._db.commit()

    # -- manifests -------------------------------------------------------------
    def add(self, m: BranchManifest) -> None:
        if m.name == MAIN:
            raise BranchError("'main' is not a creatable branch name")
        try:
            self._db.execute(
                "INSERT INTO branches (name, scope, pins, base, created_at, "
                "ttl_days, open_scope, git_branch, git_sha, git_dirty, "
                "last_run_at, last_run_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.name, json.dumps(m.scope), json.dumps(m.pins),
                 json.dumps(m.base), m.created_at, m.ttl_days,
                 int(m.open_scope), m.git_branch, m.git_sha, int(m.git_dirty),
                 m.last_run_at, m.last_run_sha),
            )
        except sqlite3.IntegrityError:
            raise BranchError(f"branch {m.name!r} already exists") from None
        self._db.commit()

    def get(self, name: str) -> BranchManifest | None:
        row = self._db.execute(
            "SELECT name, scope, pins, base, created_at, ttl_days, open_scope, "
            "git_branch, git_sha, git_dirty, last_run_at, last_run_sha "
            "FROM branches WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return BranchManifest(row[0], json.loads(row[1]), json.loads(row[2]),
                              json.loads(row[3]), row[4], row[5], bool(row[6]),
                              row[7], row[8], bool(row[9]), row[10], row[11])

    def list(self) -> list[BranchManifest]:
        return [m for r in self._db.execute("SELECT name FROM branches ORDER BY created_at")
                if (m := self.get(r[0]))]

    def add_to_scope(self, name: str, table: str) -> None:
        m = self.get(name)
        if m is None:
            raise BranchError(f"branch {name!r} does not exist")
        if table not in m.scope:
            m.scope = sorted([*m.scope, table])
            self._db.execute("UPDATE branches SET scope = ? WHERE name = ?",
                             (json.dumps(m.scope), name))
            self._db.commit()

    def update_base(self, name: str, table: str, snapshot_id: int) -> None:
        m = self.get(name)
        if m is None:
            raise BranchError(f"branch {name!r} does not exist")
        m.base[table] = snapshot_id
        self._db.execute("UPDATE branches SET base = ? WHERE name = ?",
                         (json.dumps(m.base), name))
        self._db.commit()

    def record_run(self, name: str, at: float, sha: str | None,
                   dirty: bool = False) -> None:
        """Provenance: refresh what code this branch's data reflects."""
        self._db.execute(
            "UPDATE branches SET last_run_at = ?, last_run_sha = ?, "
            "git_sha = COALESCE(?, git_sha), git_dirty = ? WHERE name = ?",
            (at, sha, sha, int(dirty), name))
        self._db.commit()

    def remove(self, name: str) -> None:
        if self.current_branch() == name:
            self.set_current(MAIN)
        self._db.execute("DELETE FROM branches WHERE name = ?", (name,))
        self._db.execute("DELETE FROM published WHERE ctx = ?", (name,))
        self._db.commit()

    # -- publish fingerprints (which sqlmesh snapshot each table last got) -----
    def published_fp(self, ctx: str, tbl: str) -> str | None:
        row = self._db.execute(
            "SELECT fp FROM published WHERE ctx = ? AND tbl = ?", (ctx, tbl)
        ).fetchone()
        return row[0] if row else None

    def set_published_fp(self, ctx: str, tbl: str, fp: str) -> None:
        self._db.execute(
            "INSERT INTO published VALUES (?,?,?) "
            "ON CONFLICT (ctx, tbl) DO UPDATE SET fp = excluded.fp",
            (ctx, tbl, fp))
        self._db.commit()

    def carry_published_to_main(self, branch: str, tables: list[str]) -> None:
        """On promote: the branch's published versions ARE main's now."""
        for t in tables:
            fp = self.published_fp(branch, t)
            if fp is not None:
                self.set_published_fp(MAIN, t, fp)

    def expired(self, now: float | None = None) -> list[BranchManifest]:
        now = now or time.time()
        return [m for m in self.list()
                if now - m.created_at > m.ttl_days * 86400]
