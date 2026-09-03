---
title: "Running and scheduling"
description: "What a run does, how scope is decided, how work is keyed, and how to put Reble on a schedule."
---
This page covers the operational half: what `reble run` does, the three ways
a scope gets decided, how work is keyed when there is no git branch, what
happens on a retry, and how to hand Reble to cron or Airflow.

## What a run does

`reble run` resolves a scope, creates or advances the data branch, pins the
upstream inputs, and executes the models in topological order — parents
strictly before children.

Order comes from the lineage graph, which comes from your SQL. You never
declare it, so you cannot get it wrong. See
[Models and lineage](/reble/models/#lineage-comes-from-the-sql).

## Three ways scope is decided

A run rebuilds a scope, never the whole warehouse. What lands in that scope
depends on which of these you use:

| | Command | Scope |
| --- | --- | --- |
| **Your SQL changed** | `reble run` | Models whose AST hash changed, plus their downstream closure. The branch case. |
| **Your data changed** | `reble run --refresh` | Models whose upstream snapshots moved since they last built, plus their downstream closure. The nightly case. |
| **You say so** | `reble run --models a,b` | Exactly these, plus their downstream closure. First builds and targeted rebuilds. |

One closure rule underneath all three: whatever seeds the scope, everything
downstream of it comes too.

`--refresh` and `--models` are mutually exclusive (exit `2`).
`reble estimate` previews any of these scopes without running them, and
`reble run --dry-run` shows the branch and pin plan while writing nothing.

`--refresh` is the one to schedule. New rows landed overnight? The models
that read them rebuild, and everything else does not. On a quiet night the
scope is empty and the check costs one catalog listing.

## How work is keyed

Every run belongs to a **change-set** — the id Reble stores its state under.
A data branch name is derived from it.

Precedence, highest first:

1. `--change-set ID`
2. `REBLE_CHANGE_SET` in the environment
3. the current git branch, when `branching.git_sync` is `true`

If none of those produce an id and `git_sync` is `false`, the key is `local`.
If none produce an id and `git_sync` is `true`, Reble exits `2` rather than
guessing which change your work belongs to.

Git is one adapter for step 3, not a requirement. Reble never *runs* git; it
reads the branch name when you ask it to. For an agent session, a
scheduler-owned project, or a notebook, key the work explicitly:

```bash
reble run --change-set agent-42
REBLE_CHANGE_SET=agent-42 reble run     # or from the environment
```

Every other verb then takes the same `--change-set agent-42`. Set
`branching.git_sync: false` in projects with no repo — see
[`branching`](/reble/config/#branching).

`--branch NAME` resumes an existing data branch under a new change-set.

## Retries

**"Retry" is "run the same command again."** There is no separate resume
command and no cleanup step.

Runs record progress as each model commits, so a run that dies at model 7 of
50 picks up at model 7. Finished models are not redone. Promotion resumes the
same way, per table.

Nothing duplicates on a retry, because every model materializes as
replace-never-append and each table commit is atomic. A table is either fully
at its old state or fully at its new one.

This is why your scheduler's retry settings need no special handling. Airflow
retries a Reble task like any other task.

`reble run --force` means "rebuild the scope even though nothing changed" —
useful after switching engines. Forcing never duplicates rows; a rerun always
replaces the previous result.

## There is no `incremental` kind

Every run fully rebuilds its scope: replace, never append. A model kind that
said "incremental" while recomputing everything would be false, so the word
is not in the contract — `kind: incremental` is rejected when the model is
parsed.

Full rebuilds are also why reruns give the same answer and why diffs can be
trusted.

Watermark and partition-insert-overwrite execution are on the
[roadmap in the README](https://github.com/satya1395/reble#roadmap-to-10).
The kind returns when it means something.

Backfills today are branch-shaped: branch, rebuild the affected models over
pinned inputs, diff, promote. Backfilling a date range without recomputing
the whole table does not exist yet.

## Scheduling is yours

Reble is the step your scheduler runs. It decides *when*; Reble does the
refresh. Nothing here needs a human.

**Scheduled** — a line in crontab is a complete orchestration setup:

```bash
0 3 * * * cd /srv/warehouse && reble run --refresh && reble gc
```

**Event-triggered** — the job that lands your data calls the refresh when it
finishes: an Airflow task after ingestion, a `workflow_dispatch` from the
pipeline, a Flink completion hook. Because `--refresh` scopes by snapshot
movement, triggering it right after an ingest rebuilds exactly that ingest's
scope, and triggering it again later does nothing.

Both are safe to fire repeatedly. A double trigger is harmless.

DAG patterns, mid-run failure behavior, and CI recipes are in the
[Airflow and CI guide](/reble/airflow/).

## Shared state

Locally, Reble keeps its state in SQLite under `.reble/` with no
configuration.

When several workers run Reble against the same warehouse — Airflow workers,
CI runners — point them at Postgres instead:

```yaml
state:
  store: postgres
  uri: ${REBLE_STATE_URI}
```

Install with `pip install 'reble[postgres]'`. It is validated at startup
(exit `2` if unreachable). Concurrent workers on different change-sets do not
conflict. See [`state`](/reble/config/#state).
