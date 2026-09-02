"""Event streams: ordered NDJSON events + final envelope on one stream."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from reble import __version__
from reble.cli import app

runner = CliRunner()


def _records(result):
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    records = [json.loads(l) for l in lines]
    events = [r for r in records if "event" in r]
    envelope = [r for r in records if "event" not in r]
    return events, envelope


def test_run_events_order_and_schema(project, seeded_catalog):
    result = runner.invoke(app, ["run", "--events", "--json"])
    assert result.exit_code == 0, result.stdout

    events, envelopes = _records(result)
    assert len(envelopes) == 1  # final envelope prints last, exactly once
    env = envelopes[0]
    assert env["reble"] == __version__
    assert env["command"] == "run"

    names = [e["event"] for e in events]
    assert names[0] == "run.begin"
    assert names[-1] == "run.end"
    assert names.count("model.start") == 3
    assert names.count("model.end") == 3
    # dependency order: stg before mart before report
    started = [e["model"] for e in events if e["event"] == "model.start"]
    assert started.index("stg_orders") < started.index("mart_orders")
    assert started.index("mart_orders") < started.index("report_daily")

    for e in events:
        assert e["events"] == "1"
        assert e["command"] == "run"
        assert "ts" in e
    assert events[0]["changeset"] == "fix-orders"
    assert events[0]["branch"] == "fix_orders"
    assert events[-1]["ok"] is True
    assert events[-1]["run_id"]


def test_run_events_skipped_models(project, seeded_catalog):
    runner.invoke(app, ["run"])
    result = runner.invoke(app, ["run", "--events", "--json"])
    assert result.exit_code == 0, result.stdout
    events, _ = _records(result)
    ends = {e["model"]: e["status"] for e in events if e["event"] == "model.end"}
    assert set(ends.values()) == {"skipped"}
    # skips still emit model.end but no model.start
    assert not [e for e in events if e["event"] == "model.start"]


def test_diff_events(project, seeded_catalog):
    runner.invoke(app, ["run"])
    result = runner.invoke(app, ["diff", "--events", "--json"])
    assert result.exit_code == 0, result.stdout
    events, envelopes = _records(result)
    names = [e["event"] for e in events]
    assert names.count("diff.table.begin") == 3
    assert names.count("diff.table.end") == 3
    ends = [e for e in events if e["event"] == "diff.table.end"]
    for e in ends:
        assert {"added", "removed", "changed"} <= set(e)
    assert envelopes[-1]["command"] == "diff"
