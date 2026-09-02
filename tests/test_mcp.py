"""MCP tool surface: the agent loop through the same core the CLI uses."""

from __future__ import annotations

import asyncio

import pyarrow as pa
import pytest
from pyiceberg.catalog import load_catalog


@pytest.fixture()
def mcp_env(agent_project, monkeypatch):
    """REBLE_PROJECT_DIR pointed at a git-less standalone project."""
    monkeypatch.setenv("REBLE_PROJECT_DIR", str(agent_project))
    monkeypatch.delenv("REBLE_CHANGE_SET", raising=False)

    _seed(agent_project)
    return agent_project


def _seed(project):
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


def test_agent_loop_generated_changeset(mcp_env):
    from reble.mcp_server import (
        reble_branch_discard,
        reble_diff,
        reble_promote,
        reble_run,
        reble_status,
    )

    # 1. run without a change-set: one is generated and returned
    result = reble_run(models=["stg_orders", "mart_orders"])
    assert result["ok"] is True
    changeset = result["changeset"]
    assert changeset.startswith("mcp-")
    assert result["branch"]["changeset"] == changeset

    # 2. status resolves via the returned change-set
    status = reble_status(change_set=changeset)
    assert status["ok"] is True
    assert status["data"]["data"]["scope"]  # branch registered with scope

    # 3. diff shows the branch's work
    diff = reble_diff(tables=["mart_orders"], change_set=changeset)
    assert diff["ok"] is True
    assert diff["data"]["tables"][0]["added"] == 2  # amount > 15 keeps 2 rows

    # 4. promote; verify main got the data
    promote = reble_promote(change_set=changeset)
    assert promote["ok"] is True
    cat = load_catalog(
        "reble",
        type="sql",
        uri=f"sqlite:///{mcp_env}/catalog.db",
        warehouse=f"file://{mcp_env}/warehouse",
    )
    rows = cat.load_table("analytics.mart_orders").scan().to_arrow().to_pylist()
    assert {r["order_id"] for r in rows} == {2, 3}

    # 5. discard cleans up the (now-promoted) branch refs
    discard = reble_branch_discard(status["data"]["data"]["branch"])
    assert discard["ok"] is True


def test_structured_error_codes(mcp_env):
    import pyarrow as pa

    from reble.mcp_server import reble_promote, reble_run, reble_status

    result = reble_run(models=["stg_orders", "mart_orders"])
    changeset = result["changeset"]

    # drift on an input pin → status reports code 3
    cat = load_catalog(
        "reble",
        type="sql",
        uri=f"sqlite:///{mcp_env}/catalog.db",
        warehouse=f"file://{mcp_env}/warehouse",
    )
    cat.load_table("analytics.raw_events").append(
        pa.table({"order_id": [4], "amount": [40.0]})
    )
    status = reble_status(change_set=changeset)
    assert status["ok"] is False
    assert status["error"]["code"] == 3

    # ff_only promote under drift → code 4
    blocked = reble_promote(ff_only=True, change_set=changeset)
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == 4

    # without an explicit change-set, git_sync:false projects resolve to the
    # 'local' default — status succeeds (code-2 is now only a git_sync:true
    # misconfiguration; the local default made standalone projects runnable)
    default = reble_status()
    assert default["ok"] is True
    assert default["branch"]["changeset"] == "local"


def test_server_smoke():
    """FastMCP-class server builds; all tools registered with expected names."""
    from reble.mcp_server import server

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "reble_run", "reble_diff", "reble_status", "reble_promote",
        "reble_branch_create", "reble_branch_list", "reble_branch_show",
        "reble_branch_discard", "reble_gc",
    }
    # descriptions carry the agent-facing semantics
    run_tool = next(t for t in tools if t.name == "reble_run")
    assert "change-set" in run_tool.description
    # structured output schema inferred from type hints
    assert "change_set" in run_tool.input_schema.get("properties", {})


def test_tool_call_through_server(mcp_env):
    """End-to-end through the SDK's call path (not just the function)."""
    import json

    from reble.mcp_server import server

    result = asyncio.run(server.call_tool("reble_run", {"models": ["stg_orders"]}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["changeset"].startswith("mcp-")
