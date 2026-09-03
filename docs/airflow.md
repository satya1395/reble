# Airflow

Reble is to Airflow what dbt-core is to Airflow: the transformation step
the scheduler runs. Airflow owns *when* (schedules, retries, cross-system
dependencies); Reble owns *what and how* (scope, order, isolation, diffs,
promotion). You already know the shape — it's the dbt + Airflow pairing,
minus the dbt.

The integration surface is deliberately boring: **one CLI verb per task,
exit codes as the contract.** No custom operators required — though a
provider package is on the roadmap (below).

## Pattern 1: nightly refresh

The whole warehouse, as one task, scoped by data movement:

```python
from airflow.decorators import dag, task
from pendulum import datetime

@dag(schedule="0 3 * * *", start_date=datetime(2026, 1, 1), catchup=False)
def reble_nightly():
    @task(bash_command="cd /srv/warehouse && reble run --refresh && reble gc")
    def refresh():
        pass

    refresh()

reble_nightly()
```

`--refresh` is idempotent: trigger it manually, trigger it twice, trigger
it from the ingestion pipeline's `TriggerDagRun` — a night with no new data
is one catalog listing and a green task.

## Pattern 2: refresh on ingest

Event-driven, from the DAG that lands your data:

```python
@task
def trigger_reble():
    from airflow.providers.http.hooks.http import HttpHook  # or cosmos-style
    # simplest robust form: call the reble refresh DAG when ingest succeeds
    ...
```

Or skip Airflow-to-Airflow plumbing entirely: make the **last task of your
ingestion DAG** `reble run --refresh` — the refresh scopes to exactly the
snapshots that ingest created.

## Pattern 3: task per model

Fine-grained DAGs where Airflow sees the model graph. Two ways:

```python
# static: you name the tasks, Airflow orders them
build_stg = BashOperator(task_id="stg_orders",
                         bash_command="reble run --models stg_orders")
build_mart = BashOperator(task_id="mart_orders",
                          bash_command="reble run --models mart_orders")
build_stg >> build_mart
```

```python
# generated: derive the task list from Reble's own lineage
import json, subprocess

def model_tasks():
    listing = json.loads(subprocess.check_output(
        ["reble", "--json", "branch", "list"]))
    # each entry's `scope` is ordered parents-before-children already
    ...
```

(Reble executes models topologically *within* a run — task-per-model is
for when you want Airflow's per-task retries, SLAs, or observability, not
because ordering needs it.)

## Pattern 4: promotion as a gated task

```python
promote = BashOperator(
    task_id="promote",
    bash_command="reble promote --ff-only",  # refuse under drift, never merge
)
checks >> promote   # checks: tests, reble status (exit 3 on drift), reble diff
```

Exit codes are the contract (SPEC §8): `0` success, `3` drift detected,
`4` promote blocked, `6` lineage unresolved. Airflow's retry/on-failure
semantics map onto them directly — and because a blocked promote *records*
re-entrant state, a retried task resumes rather than double-applies.

## Setup notes

- Install Reble on workers or in the image: `pip install reble`
  (+ `reble[mcp]` if agents share the environment).
- `REBLE_CHANGE_SET` pins a DAG's work to its own key (e.g.
  `{{ dag.dag_id }}`), so scheduled runs, backfills, and manual triggers
  never collide.
- Credentials are the usual ones: your catalog (Glue/Polaris/Nessie/REST)
  and object storage, via Airflow connections or env vars.

## Roadmap: provider package

A dedicated `apache-airflow-providers-reble` (operators with structured
exit-code handling, a deferrable run operator streaming `--events` for
progress, a sensor that waits for drift to clear) is planned once there's
user pull. Until then the BashOperator patterns above are first-class, not
a workaround — Reble's verbs were designed for exactly this.
