"""State backend: SQLite local (default), Postgres remote (DECISIONS §24).

One SQL implementation via SQLAlchemy — already a pyiceberg dependency, so
the local SQLite path adds zero new packages. `reble[postgres]` adds
psycopg for the network backend.

Schema is SaaS-forward: the run-manifests table carries the columns the
hosted layer will need (changeset grouping, timestamps), and JSON payloads
stay readable and documented in SPEC.md for ecosystem tools.

Local default is `.reble/state.db`. On first validate, a legacy
`state.json` (pre-0.5) is auto-imported and renamed `.migrated`.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ConfigError

# ------------------------------------------------------------------ data model


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
    # (--change-set flag), "env" (REBLE_CHANGE_SET), "default" (local).
    key_source: str = "git"
    model_hashes: dict[str, str] = field(default_factory=dict)  # last-run AST hashes
    scope: list[str] = field(default_factory=list)
    pins: dict[str, Pin] = field(default_factory=dict)  # relation -> Pin
    base_heads: dict[str, int] = field(default_factory=dict)  # scope table -> main head at last run
    last_run_id: str | None = None
    promote_in_progress: bool = False


@dataclass
class State:
    branches: dict[str, BranchState] = field(default_factory=dict)  # key: change-set id


# ------------------------------------------------------------------ store


def _state_to_json(bs: BranchState) -> str:
    return json.dumps(asdict(bs), default=str)


def _json_to_state(changeset: str, payload) -> BranchState:
    # Postgres JSONB returns a dict (already parsed); SQLite TEXT returns str
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    pins = {t: Pin(**p) for t, p in (raw.pop("pins", {}) or {}).items()}
    return BranchState(pins=pins, **raw)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reble_branch_state (
    changeset   TEXT PRIMARY KEY,
    data_branch TEXT NOT NULL,
    state       {json_type} NOT NULL,
    updated_at  {timestamp_type}
);
CREATE TABLE IF NOT EXISTS reble_promote_records (
    data_branch TEXT PRIMARY KEY,
    record      {json_type} NOT NULL,
    updated_at  {timestamp_type}
);
CREATE TABLE IF NOT EXISTS reble_run_manifests (
    run_id     TEXT PRIMARY KEY,
    changeset  TEXT,
    manifest   {json_type} NOT NULL,
    created_at {timestamp_type}
);
"""

_SQLITE_SCHEMA = _SCHEMA.format(json_type="TEXT", timestamp_type="TEXT DEFAULT (datetime('now'))")
_PG_SCHEMA = _SCHEMA.format(json_type="JSONB", timestamp_type="TIMESTAMPTZ NOT NULL DEFAULT now()")


class StateStore:
    """SQL-backed state store. One implementation, two engines.

    Local: SQLite at `.reble/state.db` (WAL mode — concurrent readers, safe
    same-machine multi-process). Remote: Postgres via a URI, for shared
    state across Airflow workers / CI runners.
    """

    def __init__(self, store: str = "local", uri: str | None = None, reble_dir: Path | None = None):
        self.backend = store
        self.reble_dir = reble_dir
        self._engine = None

        if store == "local":
            if reble_dir is None:
                raise ConfigError("state.store=local requires a project directory")
            self.db_path = reble_dir / "state.db"
            self._uri = f"sqlite:///{self.db_path}"
        elif store == "postgres":
            if not uri:
                raise ConfigError(
                    "state.store=postgres requires state.uri "
                    "(postgresql://user:pass@host:5432/db)"
                )
            self.db_path = None
            self._uri = uri
        else:
            raise ConfigError(f"Unknown state store '{store}' (expected local|postgres)")

    # ------------------------------------------------------------- engine

    def _connect(self):
        if self._engine is None:
            from sqlalchemy import create_engine

            kwargs = {}
            if self.backend == "local":
                self.reble_dir.mkdir(parents=True, exist_ok=True)
                kwargs["connect_args"] = {"timeout": 10}
            self._engine = create_engine(self._uri, **kwargs)
            if self.backend == "local":
                from sqlalchemy import event

                @event.listens_for(self._engine, "connect")
                def _set_wal(dbapi_conn, _):
                    dbapi_conn.execute("PRAGMA journal_mode=WAL")

        return self._engine

    # ----------------------------------------------------------- validate

    def validate(self) -> None:
        """Connect + ensure schema. Raises ConfigError (exit 2) if the backend
        is unreachable or schema creation fails. Call before any verb."""
        from sqlalchemy import text

        try:
            engine = self._connect()
            schema = _SQLITE_SCHEMA if self.backend == "local" else _PG_SCHEMA
            with engine.connect() as conn:
                for stmt in schema.split(";"):
                    if stmt.strip():
                        conn.execute(text(stmt))
                conn.commit()
        except ImportError as exc:
            raise ConfigError(
                f"state.store={self.backend} requires a driver not installed: {exc}. "
                f"Try: pip install 'reble[{self.backend}]'"
            ) from exc
        except Exception as exc:
            raise ConfigError(
                f"State backend unreachable ({self.backend}): {exc}", exit_code=2
            ) from exc

        self._migrate_legacy_json()

    def _migrate_legacy_json(self) -> None:
        """Import pre-0.5 state.json into the DB on first use, then rename."""
        if self.backend != "local" or self.reble_dir is None:
            return
        legacy = self.reble_dir / "state.json"
        if not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text())
        except (json.JSONDecodeError, OSError):
            return

        from sqlalchemy import text

        branches = raw.get("branches", {})
        if branches:
            engine = self._connect()
            with engine.begin() as conn:
                for changeset, bs in branches.items():
                    data_branch = bs.get("data_branch", "")
                    conn.execute(
                        text(
                            "INSERT OR REPLACE INTO reble_branch_state "
                            "(changeset, data_branch, state) VALUES (:c, :d, :s)"
                        ),
                        {"c": changeset, "d": data_branch, "s": json.dumps(bs, default=str)},
                    )
        legacy.rename(legacy.with_suffix(".json.migrated"))

    # ------------------------------------------------------------- state

    def load(self) -> State:
        from sqlalchemy import text

        engine = self._connect()
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT changeset, state FROM reble_branch_state")
            ).fetchall()
        branches = {}
        for changeset, payload in rows:
            try:
                branches[changeset] = _json_to_state(changeset, payload)
            except Exception:  # noqa: BLE001 — skip corrupted rows, don't crash
                continue
        return State(branches=branches)

    def save(self, state: State) -> None:
        from sqlalchemy import text

        engine = self._connect()
        upsert_pg = (
            "INSERT INTO reble_branch_state (changeset, data_branch, state) "
            "VALUES (:c, :d, :s) "
            "ON CONFLICT (changeset) DO UPDATE SET "
            "data_branch=:d, state=:s, updated_at=now()"
        )
        upsert_sqlite = (
            "INSERT OR REPLACE INTO reble_branch_state (changeset, data_branch, state) "
            "VALUES (:c, :d, :s)"
        )
        stmt = upsert_sqlite if self.backend == "local" else upsert_pg
        with engine.begin() as conn:
            for changeset, bs in state.branches.items():
                conn.execute(
                    text(stmt),
                    {"c": changeset, "d": bs.data_branch, "s": _state_to_json(bs)},
                )

    # ------------------------------------------------------ promote record

    def load_promote_record(self, data_branch: str) -> dict | None:
        from sqlalchemy import text

        engine = self._connect()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT record FROM reble_promote_records WHERE data_branch = :d"
                ),
                {"d": data_branch},
            ).fetchone()
        if row is None:
            return None
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def save_promote_record(self, record: dict) -> None:
        from sqlalchemy import text

        engine = self._connect()
        stmt = (
            "INSERT OR REPLACE INTO reble_promote_records (data_branch, record) "
            "VALUES (:d, :r)"
            if self.backend == "local"
            else
            "INSERT INTO reble_promote_records (data_branch, record) VALUES (:d, :r) "
            "ON CONFLICT (data_branch) DO UPDATE SET record=:r, updated_at=now()"
        )
        with engine.begin() as conn:
            conn.execute(text(stmt), {"d": record["branch"], "r": json.dumps(record, default=str)})

    def delete_promote_record(self, data_branch: str) -> None:
        from sqlalchemy import text

        engine = self._connect()
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM reble_promote_records WHERE data_branch = :d"),
                {"d": data_branch},
            )

    # ------------------------------------------------------ run manifests

    def save_run_manifest(self, run_id: str, changeset: str, manifest: dict) -> None:
        from sqlalchemy import text

        engine = self._connect()
        stmt = (
            "INSERT OR REPLACE INTO reble_run_manifests (run_id, changeset, manifest) "
            "VALUES (:r, :c, :m)"
            if self.backend == "local"
            else
            "INSERT INTO reble_run_manifests (run_id, changeset, manifest) "
            "VALUES (:r, :c, :m) "
            "ON CONFLICT (run_id) DO UPDATE SET manifest=:m"
        )
        with engine.begin() as conn:
            conn.execute(
                text(stmt),
                {"r": run_id, "c": changeset, "m": json.dumps(manifest, default=str)},
            )

    def load_run_hashes(self, run_id: str) -> dict[str, str]:
        from sqlalchemy import text

        engine = self._connect()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT manifest FROM reble_run_manifests WHERE run_id = :r"),
                {"r": run_id},
            ).fetchone()
        if row is None:
            return {}
        manifest = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {
            r["model"]: r["ast_hash"]
            for r in manifest.get("results", [])
            if r.get("ast_hash")
        }
