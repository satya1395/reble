---
title: "MCP and agents"
description: "Driving Reble from an AI agent over the Model Context Protocol."
---
Reble ships an MCP server that exposes the core verbs as agent tools. It is
the same core the CLI calls — an agent gets no privileged path, no extra
powers, and the same exit codes.

If you are wiring CI or a script rather than an agent, you want
[Exit codes and JSON output](/reble/exit-codes/) instead.

## Setup

```bash
pip install 'reble[mcp]'
```

Register the server with your MCP host:

```json
{
  "mcpServers": {
    "reble": {
      "command": "reble-mcp",
      "env": { "REBLE_PROJECT_DIR": "/path/to/project" }
    }
  }
}
```

| Variable | Meaning |
| --- | --- |
| `REBLE_PROJECT_DIR` | The project root — where `reble.yml` and `models/` live. Defaults to the working directory, which is rarely what a host provides. Set it. |
| `REBLE_PROFILE` | Apply a named profile from `reble.yml`. |

`reble mcp` runs the same server over stdio if you prefer the subcommand
form. It is the only long-lived Reble process, and only when you choose to
run it.

## The tools

Nine tools, one per verb.

| Tool | Does |
| --- | --- |
| `reble_run` | Materialize a scoped change on an isolated branch. Takes `models`, `depth`, `dry_run`, `change_set`, `branch`, `refresh`, `force`. |
| `reble_diff` | Row-level and schema diff of the branch's scope tables. |
| `reble_status` | Un-run edits, branch scope, drifted pins, age and expiry. |
| `reble_promote` | Per-table fast-forward of main to the branch heads. |
| `reble_branch_create` | Create an explicit zero-copy data branch. |
| `reble_branch_list` | Change-sets with their branches, key source, scope, age. |
| `reble_branch_show` | Catalog refs — branches and pin tags — matching a name. |
| `reble_branch_discard` | Drop a branch's refs and pin tags. The only alternative to promote. |
| `reble_gc` | Expire TTL'd branches and drop orphan pin tags. |

## The change-set protocol

An agent has no git branch to derive work from, so the change-set is the key
that holds a session together.

**Call `reble_run` first without a `change_set`.** It generates one, returns
it as `changeset`, and every later call passes it back:

```
reble_run(models=["stg_orders"])   → changeset: "mcp-3f9a21c4"
reble_diff(change_set="mcp-3f9a21c4")
reble_promote(change_set="mcp-3f9a21c4")
```

On a fresh change-set with no prior run, pass `models` explicitly — a fresh
branch starts with an empty scope, exactly as on the CLI. See
[the first build](/reble/models/#the-first-build).

## Errors

Failures come back as a structured object rather than a raised exception, so
the agent can branch on them:

```json
{
  "ok": false,
  "error": { "code": 3, "message": "pinned input drifted: analytics.raw_events" }
}
```

`error.code` is the exit code — `3` drift, `4` promote blocked, `5` empty
scope, `6` lineage, `7` missing diff key. The full table is in
[Exit codes and JSON output](/reble/exit-codes/#exit-codes). Partial payloads
survive: a run where one model failed returns the run data alongside the
error.

The tool docstrings are the agent-facing spec and are what your host shows
the model. They carry the same semantics as this page.

## What an agent cannot do

Nothing here bypasses the safety machinery. An agent's promote obeys the same
drift check, produces the same authoritative promote-time diff, and cannot
merge — because there is no merge. If production moved under an agent's
branch, its promote re-runs and re-diffs exactly as yours would.

## Next

- [Exit codes and JSON output](/reble/exit-codes/) — the same contract, for CI.
- [Running and scheduling](/reble/running/#how-work-is-keyed) — change-sets in depth.
