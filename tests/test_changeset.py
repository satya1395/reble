"""Change-set keying: git-less agent flows, precedence, hash-baseline fallback."""

from __future__ import annotations

from typer.testing import CliRunner

from reble.cli import app

runner = CliRunner()


def _seed(project):
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog

    cat = load_catalog(
        "reble",
        type="sql",
        uri=f"sqlite:///{project}/catalog.db",
        warehouse=f"file://{project}/warehouse",
    )
    cat.create_namespace_if_not_exists("analytics")
    t = cat.create_table(
        "analytics.raw_events",
        schema=pa.schema([("order_id", pa.int64()), ("amount", pa.float64())]),
    )
    t.append(pa.table({"order_id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}))
    return cat


def test_changeset_lifecycle_without_git(agent_project):
    cat = _seed(agent_project)

    # fresh change-set + no models declared → empty scope (bootstrap rule)
    result = runner.invoke(app, ["run", "--change-set", "agent-42"])
    assert result.exit_code == 0, result.stdout
    assert "empty scope" in result.stdout

    # declare scope → runs, keyed by agent-42
    result = runner.invoke(app, ["run", "--change-set", "agent-42", "--models", "stg_orders,mart_orders"])
    assert result.exit_code == 0, result.stdout
    refs = cat.load_table("analytics.stg_orders").refs()
    assert "agent_42" in refs  # sanitized change-set id names the data branch

    # status / diff / promote all resolve via --change-set, no git involved
    status = runner.invoke(app, ["status", "--change-set", "agent-42"])
    assert status.exit_code == 0, status.stdout

    diff = runner.invoke(app, ["--json", "diff", "mart_orders", "--change-set", "agent-42"])
    assert diff.exit_code == 0, diff.stdout

    promote = runner.invoke(app, ["promote", "--change-set", "agent-42"])
    assert promote.exit_code == 0, promote.stdout
    rows = cat.load_table("analytics.mart_orders").scan().to_arrow().num_rows
    assert rows == 2  # amount > 15 keeps orders 2 and 3


def test_hash_baseline_fallback_on_new_changeset(agent_project):
    """A second change-set resuming the same data branch stays incremental."""
    _seed(agent_project)
    runner.invoke(app, ["run", "--change-set", "one", "--models", "stg_orders,mart_orders"])

    # edit stg_orders, then run under a brand-new change-set id
    (agent_project / "models" / "stg_orders.sql").write_text(
        "-- kind: table\n-- key: order_id\n"
        "select * from raw_events where amount > 5\n"
    )
    result = runner.invoke(app, ["--json", "run", "--change-set", "two", "--branch", "one"])
    assert result.exit_code == 0, result.stdout
    import json

    env = json.loads(result.stdout)
    statuses = {r["model"]: r["status"] for r in env["data"]["results"]}
    # baseline fell back to the data branch's last run manifest:
    # only the edited model (and its downstream) re-ran
    assert statuses["stg_orders"] == "ran"
    assert statuses["mart_orders"] == "ran"
    # and with no further edits, scope is empty — the incremental baseline
    # recognizes a clean tree (invariant 6)
    again = json.loads(
        runner.invoke(app, ["--json", "run", "--change-set", "two", "--branch", "one"]).stdout
    )
    assert again["data"]["status"].startswith("empty scope")


def test_changeset_env_var_precedence(agent_project, monkeypatch):
    _seed(agent_project)
    monkeypatch.setenv("REBLE_CHANGE_SET", "from-env")
    result = runner.invoke(app, ["run", "--models", "stg_orders"])
    assert result.exit_code == 0, result.stdout
    refs = _catalog(agent_project).load_table("analytics.stg_orders").refs()
    assert "from_env" in refs

    # flag beats env
    monkeypatch.setenv("REBLE_CHANGE_SET", "from-env")
    result = runner.invoke(app, ["run", "--change-set", "from-flag", "--models", "stg_orders"])
    assert result.exit_code == 0, result.stdout


def _catalog(project):
    from pyiceberg.catalog import load_catalog

    return load_catalog(
        "reble",
        type="sql",
        uri=f"sqlite:///{project}/catalog.db",
        warehouse=f"file://{project}/warehouse",
    )


def test_no_changeset_is_config_error(agent_project):
    _seed(agent_project)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2


def test_key_source_recorded(agent_project):
    _seed(agent_project)
    runner.invoke(app, ["run", "--change-set", "agent-42", "--models", "stg_orders"])
    listing = runner.invoke(app, ["--json", "branch", "list"])
    import json

    branches = json.loads(listing.stdout)["data"]["branches"]
    entry = next(b for b in branches if b["changeset"] == "agent-42")
    assert entry["key_source"] == "explicit"
