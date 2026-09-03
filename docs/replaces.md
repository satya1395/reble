# What Reble replaces

> **Reble replaces the transformation layer and the staging environment. It
> shrinks the orchestrator and the compute bill. It leaves ingestion,
> storage, the catalog, and BI alone.**

## Today, and with Reble

**Today**

- SQL files turn into tables through *something* — dbt, an internal webapp,
  cron scripts, or Airflow tasks wired one query at a time.
- The dependencies between those SQLs live somewhere you maintain by hand —
  YAML, DAG edges, UI clicks. They drift.
- Testing a change means a copy of the warehouse: refresh it, queue for it,
  hope it matches prod.
- A backfill means scripts and a maintenance window.

**With Reble**

- Same SQL files. `reble run` builds them — the compute is DuckDB, the
  tables land in your bucket through your catalog.
- The dependencies are read from the SQL. Nothing to maintain, nothing to
  drift.
- Testing a change is a **branch** — free, instant, exactly the tables the
  change touches. You review the rows it changes, then apply it. No merge
  conflicts, ever.
- Airflow or cron still decides *when* — but it's one command now, not a
  task per query.
- Fivetran, Kafka, S3, your catalog, your BI tool: unchanged.

## Which team are you?

**The dbt team.** Reble is dbt-core plus its build orchestration plus the
staging clone — minus the YAML and Jinja. Migration is "point Reble at the
models directory"; your SQL ports almost unchanged.

**The SQL + Airflow team.** The hand-maintained wiring inside the DAG goes
away — a table reference to another model *is* the dependency; nobody
configures edges anymore. Airflow survives as the clock. See the
[Airflow guide](airflow.md) for the patterns.

**The internal-webapp team.** The webapp's whole job — store SQLs, wire
dependencies, schedule runs, pray — is the engine. This is the deepest
displacement: something you built and maintain stops needing to exist.

## The fine print, layer by layer

| Stack layer | Today | With Reble |
| --- | --- | --- |
| Ingestion | Fivetran, Airbyte, Kafka, Flink | unchanged |
| Storage + table format | S3/GCS + Iceberg | unchanged (yours) |
| Catalog | Glue, Hive, Polaris, Nessie | unchanged (yours) |
| Transformation | dbt, homegrown runners, webapps, stored procs | **replaced** |
| Test environments | staging warehouse, schema copies | **replaced by branches** |
| Orchestration | Airflow, Dagster, cron | kept, but shrunk to *when* |
| Transform compute | warehouse seats, Spark clusters | DuckDB embedded (Spark behind the same interface) |
| Data quality | dbt tests, Great Expectations | complementary — diffs verify, assertions assert |
| Lineage tooling | OpenLineage, wiki diagrams | mostly obviated — parsed, not maintained |
| Backfills | scripts and windows | a branch: frozen inputs, diff, promote |

What Reble never does: move data in, store it, catalog it, serve
dashboards, or decide when anything runs. Every "unchanged" row is also a
"works with what you have."
