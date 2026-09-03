---
title: "CLI reference"
description: "Every command, flag, envelope, and exit code."
---
Every command accepts the global flags `--config PATH`, `--profile NAME`,
`--json`, `--no-color`, `--quiet`. Machine consumers: `--json` emits the
stable envelope (SPEC §6), and `run`/`diff` accept `--events` for versioned
NDJSON progress. Exit codes are a contract (SPEC §8) — the common ones are
noted per command.

Normative detail, including the envelope shape, is in
[`SPEC.md`](https://github.com/satya1395/reble/blob/main/SPEC.md). The
[`reble.yml` schema](config.md) has its own page.

## Global flags

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Use this `reble.yml` instead of the one in the current directory. |
| `--profile NAME` | Apply a profile from `reble.yml` (or set `REBLE_PROFILE`). |
| `--json` | Emit the stable JSON envelope instead of text. Implied by `--events`. |
| `--no-color` | Strip ANSI colors (CI logs). |
| `--quiet` / `-q` | Suppress progress detail (per-command preflight, diff samples). |
| `--version` | Print the version — single-sourced with the package metadata. |

Environment: `REBLE_PROFILE` selects a profile; `REBLE_CHANGE_SET` supplies
a change-set id; `REBLE_<SECTION>__<KEY>` overrides any config path
(`REBLE_COMPUTE_POLICY__PREFER=spark`).

## reble init

Set up a project in the current directory: write `reble.yml`, gitignore
`.reble/`, check that the catalog is reachable, find `models/`. Run it
once per project.

```bash
reble init --catalog sql --namespace analytics
```

Reads as: *use a catalog that lives in a local file, and put model tables
in the `analytics` namespace.* Flags:

| Flag | Meaning |
| --- | --- |
| `--catalog TYPE` | Where tables are registered. Default `rest`. `sql` = local SQLite-backed, zero infra. `glue`, `hive`, `rest`, `polaris`, `nessie`, `reble` = infrastructure you already run — [which to pick](/reble/config/#which-catalog-type). |
| `--namespace NAME` | The schema-like prefix for model tables (`stg_orders` → `analytics.stg_orders`). |
| `--engine duckdb\|spark` | Which engine `compute_policy` prefers. Default `duckdb`. |
| `--yes` / `-y` | Skip confirmation. |

`init` does not write a `state:` block. For shared state (multiple workers,
CI runners, Airflow), add one to `reble.yml` yourself:

```yaml
state:
  store: postgres
  uri: ${REBLE_STATE_URI}
```

Requires `pip install 'reble[postgres]'`; validated at startup (exit 2
if unreachable). See [state](/reble/config/#state).

Exits `2` if the catalog is unreachable.

## reble run

The main verb: resolve scope, create/update the data branch, pin inputs,
execute.

```bash
reble run                        # scope inferred from your edits
reble run --models stg,mart      # explicit scope
reble run --refresh              # data-driven scope: upstream snapshots moved
reble run --force                # full rebuild, even when SQL is unchanged
reble run --depth 2              # cap the downstream cascade
reble run --dry-run              # preflight only, writes nothing
reble run --events               # NDJSON progress stream
reble run --engine spark         # one-off engine override
```

| Flag | Meaning |
| --- | --- |
| `--models a,b` | Explicit scope (plus downstream closure). Mutually exclusive with `--refresh`. |
| `--refresh` | Data-driven scope: models whose upstream snapshots moved since their last run. The nightly-refresh verb. |
| `--force` | Full rebuild — every model is in scope and re-runs even with unchanged SQL. The engine-switch and fresh-branch rebuild. |
| `--depth N` | Cap the downstream cascade; models cut off are reported as stale. |
| `--dry-run` | Preflight only: scope, branch plan, pin plan. Writes nothing. |
| `--engine duckdb\|spark` | Override `compute_policy.prefer` for this run. |
| `--change-set ID` | Key the work by this id. Precedence: this flag → `REBLE_CHANGE_SET` → the git branch. With `git_sync: false` and none of these, the key is `local`; with `git_sync: true` and no git branch, Reble exits `2`. |
| `--branch NAME` | Resume an existing data branch under this change-set. |
| `--events` | Stream `run.model.begin` / `run.model.end` NDJSON events on stdout (implies `--json`). |

Idempotent per model; a model skips only when its SQL is unchanged *and*
no in-scope parent ran. Re-runs replace, never append. An empty-scope `run`
is legal and registers the branch (invariant 6). Exits `6` for
unparseable model SQL.

## reble diff

Row-level + schema diff of the branch's scope tables.

```bash
reble diff                       # whole scope, vs the branch point
reble diff mart_orders           # specific tables
reble diff --against main        # advisory "what would promote do"
reble diff --schema-only         # schema deltas, no row counts
reble diff --rows 50             # save 50 sample rows per category
reble diff --full                # save all changed rows
```

The terminal shows live per-table progress and one stats line per table:
`+added -removed ~changed (N unchanged)`. Sample rows are never dumped to
the terminal — they are saved as JSON under `.reble/diffs/<change-set>/`
(one file per table plus `summary.json`), refreshed on each diff.
`--against main` writes to `<change-set>__vs_main/` so both comparisons
coexist.

| Flag | Meaning |
| --- | --- |
| `--against base\|main` | `base` = the branch point (what this change-set did); `main` = the promote preview. |
| `--rows N` | Save N sample rows per category (default: `diff.max_rows_dumped`, 1000). |
| `--full` | Save all changed rows, ignoring the cap. |
| `--schema-only` | Schema deltas only; no row work. |

Exits `7` when a table has no diff key and `on_missing_key: error`.

## reble status

The "where was I?" answer — read-only, CI-safe.

```bash
reble status                     # text summary
reble status --json              # machine envelope
```

Reports: un-run edits vs the change-set's hashes, drifted input pins
(tag no longer equals main's head — exit `3`), branch age vs
`branching.ttl_days`, and promote state. Clean = exit `0`; drift = exit
`3`. This is the CI gate: run it on every PR.

## reble promote

Fast-forward production to the branch. The accept button.

```bash
reble promote                    # drift check → forced re-run if needed → FF
reble promote --ff-only          # refuse instead of re-running (exit 4)
reble promote --dry-run          # show the plan, touch nothing
```

| Flag | Meaning |
| --- | --- |
| `--ff-only` | Refuse when any pin has drifted, instead of the forced scoped re-run + fresh diff. Exits `4` when blocked. |
| `--dry-run` | Show drift state, per-table plan, and the promote-time diff without moving any refs. |
| `--yes` / `-y` | Skip the confirmation prompt. |

Promotion is per-table fast-forward — no merge, ever. A promote-time diff
is computed and included in the envelope; PR-time diffs are advisory, this
one is authoritative. Interrupted promotes resume: per-table progress is
persisted, and completed tables are not re-done.

## reble estimate

Rough, local cost estimate from Iceberg metadata only — no data is scanned.

```bash
reble estimate                   # for the current scope
reble estimate --refresh         # for the data-driven scope
```

Reports estimated bytes read and rows per scope table and pinned input.
The numbers come from Iceberg snapshot summaries, so they are as accurate as
those summaries and no more — accurate estimation is an explicit non-goal.

| Flag | Meaning |
| --- | --- |
| `--models a,b` | Estimate this explicit scope instead of the inferred one. |
| `--depth N` | Cap the downstream cascade, as `run` does. |
| `--refresh` | Estimate the data-driven scope (`run --refresh`). |
| `--change-set ID` | Key the work by this id. |
| `--branch NAME` | Use an existing data branch. |

## reble branch

Explicit branch management. Required when `branching.git_sync` is `false`;
optional otherwise, since `run` creates the data branch for you.

### reble branch create

```bash
reble branch create staging               # explicit data branch
reble branch create staging --from main   # fork from a specific ref
```

The branch-first gesture: create a data branch before any model changes
(invariant 5 — an empty scope is legal). Branch names are sanitized
(`branching.name_sanitization`) and disambiguated if the ref already exists
on the tables.

| Flag | Meaning |
| --- | --- |
| `--from REF` | Ref to fork from (default: `warehouse.default_base`). |
| `--change-set ID` | Register a change-set id for the new branch. |

### reble branch list

```bash
reble branch list
```

Lists the data branches Reble knows about. Metadata only — it does not open
a compute engine.

### reble branch show

```bash
reble branch show fix-orders
```

Catalog refs whose name contains the argument: the branch ref on each table
carrying it, plus that branch's pin tags, each with the snapshot it points
at. Matching is a substring match on the ref name, so a short name matches
broadly. It reports catalog state only — not local run state.

### reble branch discard

Drop a branch's refs and its pin tags. The other half of promote-or-discard
— there is no merge. Refuses if a promote is in progress for that branch.

```bash
reble branch discard fix-orders          # prompts for confirmation
reble branch discard fix-orders --yes    # non-interactive
```

The branch name is required; there is no "current change-set" default.

## reble gc

Clean up correctness-critical debris: expired branches and their pin tags.

```bash
reble gc                         # expire branches older than ttl_days
reble gc --before 7              # custom age
reble gc --dry-run               # list what would be dropped
```

| Flag | Meaning |
| --- | --- |
| `--before DAYS` | Expire branches older than this instead of `branching.ttl_days`. |
| `--dry-run` | Report the branches and tags that would be dropped; change nothing. |

Two things, and only these two: expire branches older than
`branching.ttl_days`, and drop pin tags left behind by branches that are
gone. It does not promote anything and it does not touch live branches.

Dropping orphan pin tags is the correctness-critical half. A pin tag blocks
`expire_snapshots` on the table it points at, so a discarded branch whose
tags linger would prevent a production table from ever reclaiming data
files.

## reble mcp

Start the MCP server (stdio) exposing the same verbs as tools for AI
agents. Requires `pip install 'reble[mcp]'`. Tool surface and the agent
change-set protocol are documented in SPEC §9.

## Exit codes

| Code | Meaning | Typical trigger |
| --- | --- | --- |
| `0` | success | — |
| `2` | config / environment | unreachable catalog or state backend, bad `reble.yml` |
| `3` | drift | `status`: a pinned input no longer equals main's head |
| `4` | promote blocked | `promote --ff-only` with drift |
| `5` | empty scope | verbs that require one (`diff` with nothing to diff) |
| `6` | lineage error | unparseable model SQL, unknown dialect |
| `7` | missing diff key | keyed diff on a table with no key and `on_missing_key: error` |
