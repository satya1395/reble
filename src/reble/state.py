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
    ttl_days    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    branch TEXT NOT NULL
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


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
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
                "INSERT INTO branches VALUES (?,?,?,?,?,?)",
                (m.name, json.dumps(m.scope), json.dumps(m.pins),
                 json.dumps(m.base), m.created_at, m.ttl_days),
            )
        except sqlite3.IntegrityError:
            raise BranchError(f"branch {m.name!r} already exists") from None
        self._db.commit()

    def get(self, name: str) -> BranchManifest | None:
        row = self._db.execute(
            "SELECT name, scope, pins, base, created_at, ttl_days "
            "FROM branches WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return BranchManifest(row[0], json.loads(row[1]), json.loads(row[2]),
                              json.loads(row[3]), row[4], row[5])

    def list(self) -> list[BranchManifest]:
        return [m for r in self._db.execute("SELECT name FROM branches ORDER BY created_at")
                if (m := self.get(r[0]))]

    def update_base(self, name: str, table: str, snapshot_id: int) -> None:
        m = self.get(name)
        if m is None:
            raise BranchError(f"branch {name!r} does not exist")
        m.base[table] = snapshot_id
        self._db.execute("UPDATE branches SET base = ? WHERE name = ?",
                         (json.dumps(m.base), name))
        self._db.commit()

    def remove(self, name: str) -> None:
        if self.current_branch() == name:
            self.set_current(MAIN)
        self._db.execute("DELETE FROM branches WHERE name = ?", (name,))
        self._db.commit()

    def expired(self, now: float | None = None) -> list[BranchManifest]:
        now = now or time.time()
        return [m for m in self.list()
                if now - m.created_at > m.ttl_days * 86400]
