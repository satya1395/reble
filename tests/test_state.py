"""State backend: SQLite local, Postgres remote, validation, migration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reble.state import BranchState, State, StateStore


def _tmp_store(tmp_path: Path) -> StateStore:
    store = StateStore(store="local", reble_dir=tmp_path / ".reble")
    store.validate()
    return store


def test_validate_creates_schema(tmp_path):
    _tmp_store(tmp_path)
    assert (tmp_path / ".reble" / "state.db").exists()


def test_validate_bad_backend(tmp_path):
    from reble.errors import ConfigError

    with pytest.raises(ConfigError, match="local requires"):
        StateStore(store="local", reble_dir=None)
    with pytest.raises(ConfigError, match="postgres requires"):
        StateStore(store="postgres", uri=None)
    with pytest.raises(ConfigError, match="Unknown state store"):
        StateStore(store="redis")


def test_validate_unreachable_postgres():
    from reble.errors import ConfigError

    store = StateStore(
        store="postgres", uri="postgresql://nobody:nopass@localhost:1/nonexistent"
    )
    with pytest.raises(ConfigError):
        store.validate()


def test_cross_process_continuity(tmp_path):
    """Two fresh StateStore instances share state — the Airflow-container scenario."""
    store1 = _tmp_store(tmp_path)
    state = State(branches={"cs1": BranchState(git_branch="cs1", data_branch="db1")})
    store1.save(state)

    # fresh instance, same backend — sees the same state
    store2 = StateStore(store="local", reble_dir=tmp_path / ".reble")
    loaded = store2.load()
    assert loaded.branches["cs1"].data_branch == "db1"


def test_migration_from_legacy_state_json(tmp_path):
    """A pre-0.5 state.json is auto-imported into SQLite and renamed."""
    reble_dir = tmp_path / ".reble"
    reble_dir.mkdir()
    legacy_data = {
        "branches": {
            "old-changeset": {
                "git_branch": "old",
                "data_branch": "old_branch",
                "model_hashes": {"m1": "h1"},
                "pins": {},
            }
        }
    }
    (reble_dir / "state.json").write_text(json.dumps(legacy_data))

    store = StateStore(store="local", reble_dir=reble_dir)
    store.validate()  # triggers migration

    # legacy file renamed, not deleted
    assert (reble_dir / "state.json.migrated").exists()
    assert not (reble_dir / "state.json").exists()

    # data available through the new backend
    loaded = store.load()
    assert loaded.branches["old-changeset"].data_branch == "old_branch"
    assert loaded.branches["old-changeset"].model_hashes == {"m1": "h1"}


def test_no_migration_when_no_legacy(tmp_path):
    _tmp_store(tmp_path)
    assert not (tmp_path / ".reble" / "state.json.migrated").exists()


def test_row_scoped_upsert(tmp_path):
    """Different change-sets write independently — no whole-document conflict."""
    store = _tmp_store(tmp_path)
    state_a = State(branches={"cs-a": BranchState(git_branch="a", data_branch="db-a")})
    store.save(state_a)

    # a second save with a different change-set doesn't clobber the first
    state_b = State(branches={"cs-b": BranchState(git_branch="b", data_branch="db-b")})
    store.save(state_b)

    loaded = store.load()
    assert "cs-a" in loaded.branches
    assert "cs-b" in loaded.branches


def test_promote_record_lifecycle(tmp_path):
    store = _tmp_store(tmp_path)
    assert store.load_promote_record("branch-x") is None
    store.save_promote_record({"branch": "branch-x", "tables": {"t1": {"status": "promoted"}}})
    rec = store.load_promote_record("branch-x")
    assert rec["tables"]["t1"]["status"] == "promoted"
    store.delete_promote_record("branch-x")
    assert store.load_promote_record("branch-x") is None


def test_run_manifest_roundtrip(tmp_path):
    store = _tmp_store(tmp_path)
    store.save_run_manifest("run-1", "cs1", {
        "results": [
            {"model": "stg", "ast_hash": "abc", "status": "ran"},
            {"model": "mart", "ast_hash": "def", "status": "ran"},
        ]
    })
    hashes = store.load_run_hashes("run-1")
    assert hashes == {"stg": "abc", "mart": "def"}
    assert store.load_run_hashes("nonexistent") == {}


def test_schema_readable_and_inspectable(tmp_path):
    """The dbt manifest lesson: state is an ecosystem artifact, not opaque."""
    import sqlite3

    store = _tmp_store(tmp_path)
    store.save(State(branches={"x": BranchState(git_branch="x", data_branch="y")}))
    store.save_run_manifest("r", "x", {"results": []})

    # raw SQL access works — tools can query without our code
    conn = sqlite3.connect(str(tmp_path / ".reble" / "state.db"))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'reble_%'"
    ).fetchall()
    assert {t[0] for t in tables} == {
        "reble_branch_state", "reble_promote_records", "reble_run_manifests"
    }
    # payloads are readable JSON, not opaque blobs
    raw = conn.execute("SELECT state FROM reble_branch_state").fetchone()[0]
    parsed = json.loads(raw)
    assert parsed["data_branch"] == "y"
    conn.close()


# ------------------------------------------------------------------ postgres


@pytest.mark.skipif(
    not os.environ.get("REBLE_PG_TEST_URI"),
    reason="set REBLE_PG_TEST_URI to run Postgres integration tests",
)
class TestPostgresBackend:
    """Full lifecycle against a real Postgres via docker."""

    @pytest.fixture()
    def pg(self):
        uri = os.environ["REBLE_PG_TEST_URI"]
        store = StateStore(store="postgres", uri=uri)
        store.validate()
        yield store

    def test_full_roundtrip(self, pg):
        state = State(branches={"pg-cs": BranchState(git_branch="g", data_branch="pg-db")})
        pg.save(state)
        assert pg.load().branches["pg-cs"].data_branch == "pg-db"

        pg.save_run_manifest("pg-run", "pg-cs", {"results": [{"model": "m", "ast_hash": "h"}]})
        assert pg.load_run_hashes("pg-run") == {"m": "h"}

        pg.save_promote_record({"branch": "pg-db", "tables": {}})
        assert pg.load_promote_record("pg-db")["branch"] == "pg-db"
        pg.delete_promote_record("pg-db")
        assert pg.load_promote_record("pg-db") is None

    def test_concurrent_change_sets(self, pg):
        """Different change-sets write independently — the shared-state scenario."""
        store2 = StateStore(store="postgres", uri=os.environ["REBLE_PG_TEST_URI"])
        store2.validate()

        pg.save(State(branches={"a": BranchState(git_branch="a", data_branch="da")}))
        store2.save(State(branches={"b": BranchState(git_branch="b", data_branch="db")}))

        # both visible from either instance
        assert "a" in pg.load().branches
        assert "b" in store2.load().branches
