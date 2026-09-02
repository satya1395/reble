# Reble CLI Specification — v0.2

Normative for v0.2.x. Anything not specified here is undefined behavior —
pick a behavior and write it down in [`DECISIONS.md`](DECISIONS.md).

This document incorporates the standalone-direction supersessions (no dbt
dependency, plain-SQL model contract, SQLGlot-only lineage) and the v0.2
deltas from the vision design notes (§11): change-set keying, event streams, and
provenance. Where the original chat-era spec was dbt- or git-framed, this
document is the authority.

## 1. Load-bearing invariants

These ten rules override any local convenience. Every PR that violates one
must justify itself in the description.

1. Reble reads git, never runs git. `git_sync: false` must make reble 100%
   git-ignorant.
2. Work is keyed by change-set. The change-set id is the primary state key;
   a git branch is one derivation adapter (`git_sync`), alongside an explicit
   `--change-set` / `REBLE_CHANGE_SET` id. The change-set ↔ data-branch
   mapping is recorded locally in `.reble/state.json`.
3. Scope = AST-changed models ∪ downstream closure. Cosmetic edits
   (whitespace, comments, casing) hash identically on the canonical SQLGlot
   AST and never trigger a run.
4. Upstream inputs are pinned by Iceberg tags created at run time (tags block
   `expire_snapshots`). Reads inside the branch resolve through tags, never
   raw snapshot IDs.
5. Branch-first with an empty scope is legal. Scope grows at first run; reads
   resolve as of the branch epoch (creation moment).
6. Incremental models always full-refresh inside branches. Announced in
   output, never silent.
7. Fast-forward promote is legal only when every scope table's pinned base
   still equals current main. Otherwise promote forces a scoped re-run and
   emits a fresh, promote-time diff. PR diffs are advisory; promote diffs are
   authoritative.
8. On vanilla catalogs, promotion is per-table fast-forwards with recorded,
   re-entrant state. Cross-table atomicity is a Reble-catalog feature and is
   labeled as such in output.
9. No three-way merges. Ever. `promote` (fast-forward or re-run) or
   `discard`. There is no third option.
10. Every read command takes `--json` with a stable envelope and documented
    exit codes. Bots are first-class users.

## 2. On-disk layout

```
reble.yml            # project config — versioned in git
.reble/              # machine-local state — gitignored (init adds the entry)
  state.json         # change-set ↔ data-branch mapping (keyed by change-set id;
                     #   each entry records key_source: git | explicit | env),
                     #   branch epochs, pins, base heads
  runs/<id>/         # run manifests: scope, pins, engine, timings, results
                     #   (results carry the model SQL bundle — provenance)
  promote.json       # re-entrant promote record (per-table fast-forward status)
```

`state.json` and everything under `.reble/` is machine-local. Change-set ids
are sanitized into data-branch names; a collision with another writer's
branch auto-suffixes (`<name>__<user-suffix>`), never silently shared.
`--branch` on any verb resumes an existing data branch under the current
change-set.

## 3. reble.yml schema

```yaml
version: 1
warehouse:
  catalog:
    type: glue              # glue | polaris | nessie | hive | rest | reble
    # type-specific keys, e.g.:
    # polaris: { uri: ..., credential: ${POLARIS_TOKEN} }
    # rest:    { uri: ..., warehouse: ... }
  default_base: main        # ref data branches fork from
  namespace: analytics      # optional Iceberg namespace for model tables
lineage:
  models_path: models       # models/**/*.sql, one file = one model
  dialect: duckdb           # SQLGlot dialect: AST hashing + lineage parsing
branching:
  git_sync: true            # false → reble never reads git; use `reble branch create`
  name_sanitization:        # git branch name → Iceberg ref name
    replace: { "/": "__", " ": "_" }
  pin_inputs: true
  tag_prefix: reble_pin__
  ttl_days: 14
diff:
  keys:                     # explicit keys; else the model's `key:` header
    analytics.orders: [order_id]
  on_missing_key: hash      # hash (full-row compare + warning) | error
  max_rows_dumped: 1000     # applies to table AND json output; --full overridable
engines:
  duckdb: {}                # local dev + CI
  spark:
    provider: emr-serverless   # emr-serverless | glue | local | k8s
    application_id: ${EMR_APP_ID}
    execution_role: ${EMR_ROLE_ARN}
compute_policy:
  prefer: duckdb            # duckdb | spark
profiles:                   # optional; flag: --profile ci
  ci:
    compute_policy: { prefer: spark }
```

Config precedence: CLI flag → env var (`REBLE_*`) → profile → `reble.yml` →
built-in default.

Secrets: `${VAR}` interpolation from environment only. Never write secrets
to `reble.yml`; init refuses to save them if it sees them.

## 4. Model contract

Plain SQL files. One file = one model. File stem = model name. Semantics in
a minimal header comment block:

```sql
-- model: mart_orders      (optional; defaults to file name)
-- kind: table | view | incremental
-- key: order_id           (diff key; required for incremental)
select ...
```

- Lineage edges: a parsed table reference matching a registry model name.
- Upstream inputs: any table reference that is not a model — pinned via
  Iceberg tags at run time.
- Unparseable SQL → exit 6.
- Diff keys: `key:` header or `diff.keys` in reble.yml; `on_missing_key`
  behavior unchanged.

## 5. Commands

Global flags on every command: `--config PATH`, `--profile NAME`, `--json`,
`--no-color`, `--quiet`.

### `reble init`

Writes `reble.yml`, adds `.reble/` to `.gitignore`, probes catalog
connectivity, detects `models/`.

`--catalog TYPE` `--engine TYPE` `--namespace NAME` `--yes`

Fails (exit 2) if catalog is unreachable. Prints a summary of what it
detected vs. assumed — assumptions are always labeled.

### `reble run`

The main verb. Resolves scope, creates/updates the data branch, pins inputs,
executes.

`--models m1,m2` · `--depth N` (cap cascade; deeper tables marked stale) ·
`--dry-run` · `--engine NAME` · `--change-set ID` · `--branch NAME` (resume
an existing data branch under this change-set) · `--events`

Change-set resolution precedence: `--change-set` flag → `REBLE_CHANGE_SET`
env → git branch (when `git_sync`). A model may skip execution only when its
AST hash is unchanged AND no in-scope parent executed this run.

Dry-run output (also the pre-flight block of a real run):

```
scope (edited)      2 models   stg_orders, int_orders_enriched
scope (downstream)  4 models   mart_orders, mart_finance_daily, ...
pinned inputs       7 tables   → tags reble_pin__fix_orders__<table>
full-refresh        1 model    fct_orders (incremental)
engine              duckdb (local)
WARN                mart_events has no diff key (on_missing_key: hash)
```

Run is idempotent per model (AST hash in run manifest — re-running skips
unchanged models).

### `reble diff [tables...]`

Row-level + schema diff of scope tables.

`--against base|main` (default base = branch point; main = advisory "what
would promote do") · `--schema-only` · `--rows N` · `--full` ·
`--change-set ID` · `--branch NAME` · `--events`

Output per table: `+added / -removed / ~changed` counts, key columns used,
sample rows, schema delta. Exit 7 if a table has no key and
`on_missing_key: error`.

### `reble status`

The "where was I?" answer. Read-only, CI-safe.

Sections: code (un-run AST changes, uncommitted files) · data (branch head,
last run) · pins (drifted tables) · lifecycle (age, expiry, base commit).

Exit 3 if drift detected — lets CI fail loudly.

### `reble promote`

`--yes` · `--ff-only` · `--dry-run`

Preflight: compare every pin and every scope table's base head vs. current
main.

Clean → per-table fast-forwards (single atomic commit on reble catalog;
output says which happened).

Drifted → announce, re-pin, re-run scope, emit authoritative promote diff,
then fast-forward. `--ff-only` refuses instead (exit 4).

Record `promote.json` for re-entrancy — an interrupted promote resumes,
never double-applies.

### `reble branch`

- `create <name> [--from REF]` — explicit branch (required when
  `git_sync: false`)
- `list` · `show <name>` — catalog refs + pin tags + state
- `discard <name> [--yes]` — drops branch refs and pin tags; refuses if
  promote was in progress

### `reble gc`

Expires TTL'd branches, drops orphan pin tags. `--before DURATION`
`--dry-run`. Orphan pin tags block snapshot expiration on prod — GC is a
correctness command, not hygiene.

### `reble estimate` (v0.2)

Rough local cost estimate. `--json`. Accurate estimation is a Cloud feature;
local estimate is honest about being rough.

## Event streams

`run` and `diff` accept `--events`: NDJSON on stdout — one JSON object per
line, followed by the final envelope printed as a single line. Every event
record carries `events` (schema version, `"1"`), `reble` (CLI version),
`command`, `event`, and `ts`; the final envelope carries no `event` field,
so one stream is self-describing. `--events` implies machine mode (human
text is suppressed).

Events: `run.begin` (branch, changeset, scope) · `model.start` ·
`model.end` (status, rows_written, duration_ms; skipped models emit
`model.end` without a preceding `model.start`) · `run.end` (ok, run_id) ·
`diff.table.begin` · `diff.table.end` (added/removed/changed).

In-process consumers (the editor) use the core callback API (`on_event`)
instead of parsing stdout. Additive changes only within a major version of
the events schema.

## Provenance

Every branch write records `reble.model`, `reble.ast_hash`,
`reble.branch`, `reble.run_id`, and `reble.changeset` in the snapshot's
summary properties. Summaries travel with the snapshot: after promote,
main's head snapshot still answers "which code produced this state" without
touching git. Zero-row seed snapshots (new models) carry `reble.seed`. The
full SQL bundle lives in the run manifest (`.reble/runs/<id>.json`), not the
catalog — snapshot summaries stay small.

## 6. JSON envelope

Every `--json` output uses one shape:

```json
{
  "reble": "0.2.0",
  "command": "status",
  "ok": true,
  "branch": { "git": "fix-orders", "data": "fix_orders", "changeset": "fix-orders" },
  "data": { },
  "warnings": ["mart_events: no diff key, full-hash compare"],
  "errors": []
}
```

Additive changes only within a minor version (the `branch.changeset` field
is one such addition). Breaking envelope changes bump the major version.

## 7. Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | runtime error |
| 2 | config / credentials / unreachable catalog |
| 3 | drift detected (status, CI use) |
| 4 | promote blocked (drift + `--ff-only`, or re-run failed) |
| 5 | empty scope (nothing to run) |
| 6 | lineage unresolved (unparseable SQL / unknown ref) |
| 7 | missing diff key under `on_missing_key: error` |
| 130 | interrupted (promote state persisted, resumable) |
