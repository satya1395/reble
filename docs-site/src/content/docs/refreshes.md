---
title: "Refreshes"
description: "Order, scope, and scheduling without an orchestrator."
---
"No orchestrator required" is three concrete claims.

## Order comes from lineage

Every `reble run` executes models topologically — parents strictly before
children — from the same SQLGlot graph that scopes branches. You never
declare order, and you can't get it wrong.

## Scope comes from movement

Two kinds of scope, one closure rule:

- **SQL changed** (the branch case): scope is inferred — edited models
  plus their downstream closure. See [scope](scope.md).
- **Data changed** (the nightly case): `reble run --refresh` rebuilds
  exactly the models whose inputs got new data since they last built —
  plus everything built on top of those. New rows landed overnight? The
  models that read them rebuild; everything else doesn't. On a quiet
  night, nothing rebuilds and the check costs almost nothing.

`--refresh` and `--models` are mutually exclusive;
`reble estimate --refresh` previews the same scope.

## Scheduling is deliberately yours

Cron, GitHub Actions, Airflow — they all just invoke one idempotent verb
(the [Airflow guide](airflow.md) has DAG patterns). The *run* is the unit;
*when* is not Reble's layer. Two patterns, neither involving a human:

**Scheduled** — a nightly line in crontab is a complete orchestration
setup:

```bash
0 3 * * * cd /srv/warehouse && reble run --refresh && reble gc
```

**Event-triggered** — the job that lands your data calls the refresh when
it finishes: an Airflow task after ingestion, a GitHub Actions
`workflow_dispatch` from the pipeline, a Flink completion hook. Because
`--refresh` scopes by snapshot movement, triggering it right after an
ingest rebuilds exactly that ingest's blast radius; triggering it again
later is a no-op.

PRs are jobs too: `reble status` (exit 3 on drift) and `reble diff` make
cheap, meaningful checks in any CI system.

## State, when multiple workers run Reble

Locally, state is SQLite — zero-config. For teams sharing a warehouse
from multiple workers (Airflow, CI runners), one line switches to
Postgres: `state.store: postgres` plus a URI in `reble.yml`. Concurrent
workers on different change-sets never conflict. Details in the
[configuration reference](config.md#state).
