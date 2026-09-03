---
title: "Exit codes and JSON output"
description: "The machine interface: exit codes, the --json envelope, and NDJSON event streams."
---
Everything a script, CI job, or agent needs to drive Reble without parsing
human output. Exit codes and the envelope are a stability contract:
`SPEC.md` is the normative source, and this page is the working reference.

## Exit codes

Every command returns one of these. They are meaningful, not just
zero-or-not, and CI should branch on them.

| Code | Name | Means | What to do |
| --- | --- | --- | --- |
| `0` | OK | The verb succeeded. | Continue. |
| `1` | ERROR | Something failed that has no more specific code — a model raised, the catalog rejected a write. | Read `errors[]` in the envelope. |
| `2` | CONFIG | `reble.yml` is invalid, an interpolated `${VAR}` is unset, the catalog is unreachable, or flags conflict (`--refresh` with `--models`). | Fix the config or the environment. Never a retry. |
| `3` | DRIFT | A pinned input no longer equals current `main`. Production moved since this branch was built. | Not a failure. Re-run to re-pin, or promote and let it re-run for you. As a PR gate, this is the signal to rebuild before merging. |
| `4` | PROMOTE_BLOCKED | `promote --ff-only` found drift and refused rather than re-running. | Drop `--ff-only` to re-run and re-diff, or investigate what moved. |
| `5` | EMPTY_SCOPE | Nothing to do: no edits, or `--refresh` found no moved inputs. | Usually success. A quiet night returns this. |
| `6` | LINEAGE | The model graph could not be built — unparseable SQL, or a cycle. | Fix the SQL. |
| `7` | MISSING_KEY | A diff needs a row key and the model has none, with `diff.on_missing_key: error`. | Add `-- key:` to the model, or set `on_missing_key: hash`. |
| `130` | INTERRUPTED | Ctrl-C. | Re-run. Verbs resume; nothing is half-applied. |

A worked gate:

```bash
reble status
case $? in
  0) echo "clean — diff is current" ;;
  3) echo "upstream moved; rebuilding" && reble run && reble diff ;;
  *) echo "real failure" && exit 1 ;;
esac
```

## The `--json` envelope

`--json` on any command replaces human output with one JSON object on stdout.
The shape is stable: fields are added within a minor version, never removed
or repurposed.

```json
{
  "reble": "0.6.1",
  "command": "status",
  "ok": true,
  "branch": {
    "git": null,
    "data": "local",
    "changeset": "local"
  },
  "data": { "...": "command-specific" },
  "warnings": [],
  "errors": []
}
```

| Field | Meaning |
| --- | --- |
| `reble` | The version that produced this envelope. |
| `command` | The verb, e.g. `run`, `diff`, `status`, `promote`. |
| `ok` | Whether the verb succeeded. Read the exit code too — `ok: true` with exit `3` is a clean run that found drift. |
| `branch.git` | The git branch, or `null` when not derived from git. |
| `branch.data` | The Iceberg data branch the work landed on. |
| `branch.changeset` | The change-set id — the state key. |
| `data` | Command-specific payload. |
| `warnings` | Non-fatal notes, e.g. a model with no diff key. |
| `errors` | Failure messages. Populated when `ok` is `false`. |

Failures still emit an envelope. A run where one model raised returns the run
payload *and* the error, so you can see which models finished.

## Event streams

`run` and `diff` accept `--events`, which streams newline-delimited JSON on
stdout as work happens. `--events` implies `--json`; the final envelope prints
last, after the event lines.

Every record carries `reble`, `events` (the schema version), `command`,
`event`, and `ts`, plus event-specific fields.

```
{"reble":"0.6.1","events":"1","command":"run","event":"run.begin","ts":1788472916.362,"branch":"local","changeset":"local","edited":["mart_orders","report_daily","stg_orders"],"downstream":[],"pinned_inputs":["raw_events"]}
{"reble":"0.6.1","events":"1","command":"run","event":"model.start","ts":1788472916.400,"model":"stg_orders","kind":"table"}
{"reble":"0.6.1","events":"1","command":"run","event":"model.end","ts":1788472916.508,"model":"stg_orders","status":"ran","kind":"table","rows_written":3,"duration_ms":107,"error":null}
```

| Command | Events |
| --- | --- |
| `run` | `run.begin`, `model.start`, `model.end`, `run.end` |
| `diff` | `diff.table.begin`, `diff.table.end` |

`model.end` carries `status`, which is `ran`, `skipped`, or `error`. A
record's fields are additive within a major version of the `events` schema,
so unknown fields should be ignored rather than treated as an error.

## Configuration from the environment

Every config path has an environment override, using double underscores
between sections:

```bash
REBLE_COMPUTE_POLICY__PREFER=spark reble run
REBLE_CHANGE_SET=agent-42 reble status
REBLE_PROFILE=ci reble run --refresh
```

Precedence, highest first: CLI flag → `REBLE_*` env var → profile →
`reble.yml` → built-in default. See
[Configuration](/reble/config/#precedence-and-interpolation).

## Agents

MCP hosts drive the same verbs through the same core, with exit codes
surfacing as structured `error.code`. See
[MCP and agents](/reble/mcp/).
