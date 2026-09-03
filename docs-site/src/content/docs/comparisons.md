---
title: "Comparisons"
description: "What Reble replaces in your stack, and how it stacks up against lakeFS, Nessie, warehouse clones, and dbt."
---
> **Reble replaces the transformation layer and the staging environment. It
> shrinks the orchestrator and the compute bill. It leaves ingestion,
> storage, the catalog, and BI alone.**

This page is the evaluation page: what changes in your stack, which team you
are, and the one-paragraph version of every tool people ask us about.

## Layer by layer

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

## Which team are you?

**The dbt team.** Reble is dbt-core plus its build orchestration plus the
staging clone — minus the YAML and Jinja. Migration is "point Reble at the
models directory"; your SQL ports almost unchanged.

**The SQL + Airflow team.** The hand-maintained wiring inside the DAG goes
away — a table reference to another model *is* the dependency; nobody
configures edges anymore. Airflow survives as the clock. See the
[Airflow and CI guide](/reble/airflow/) for the patterns.

**The internal-webapp team.** The webapp's whole job — store SQLs, wire
dependencies, schedule runs, pray — is the engine. This is the deepest
displacement: something you built and maintain stops needing to exist.

## Reble vs lakeFS

[lakeFS](https://lakefs.io) versions **objects**: branches are prefixes in
an object store, and tools see them as paths. That's infrastructure-level
versioning — but a lakehouse already has table-level versioning in Iceberg,
and a path-shaped abstraction can't know tables, schemas, or rows. Reble
works one level up: branches are Iceberg refs, diffs are row-level and
keyed, and the scope of a branch is derived from your SQL lineage. lakeFS
also resolves changes with merges; Reble deliberately doesn't merge at all.

**Pick lakeFS if** you need versioning of arbitrary objects and you're happy
running its server. **Pick Reble if** your tables are Iceberg and you want
branching as a workflow, not as infrastructure.

## Reble vs Nessie

[Nessie](https://projectnessie.org) is a **catalog** with git-like
versioning — a good one, and a thing you run (plus a database for it). Its
branching is catalog-wide and its merges are real three-way merges across
tables. Reble is not a catalog: it runs against whatever catalog you already
have and adds the *workflow* — scope inference from SQL, tag-pinned inputs,
row diffs, fast-forward-or-rerun promotion. The consistency models differ
where it matters: Nessie asks you to trust a three-way merge across tables;
Reble commits **atomically per table** (no partial states, interrupted work
resumes) and *proves* cross-table consistency at promote time — the
fast-forward is legal only when every pinned input still equals production.
If you already run Nessie, Reble can use it as a catalog like any other.

**Pick Nessie if** you want a versioned catalog as infrastructure and are
taking on its operation. **Pick Reble if** you want branches without
operating anything new — and without merge semantics on your data.

## Reble vs warehouse zero-copy clones

Snowflake and BigQuery offer zero-copy clones: instant table copies inside
the platform. They're genuinely good — and genuinely locked: clones live and
die inside that warehouse, priced by its economics, and a clone of a schema
is not a *scoped* branch (you clone the blast radius you know, not the one
your lineage implies). Reble's branches are open Iceberg metadata in *your*
storage and catalog; the diff and promote workflow travels with them.

**Pick clones if** you're staying inside one warehouse forever. **Pick
Reble if** your tables are Iceberg in your bucket, or might be.

## Reble vs dbt

You know dbt: your SQL lives in templates (`{{ ref('stg_orders') }}`),
your configs in YAML, and `dbt build` compiles everything and runs it in
order against your warehouse.

Reble is the same job done differently. Your SQL stays plain — no Jinja,
no YAML, no compile step. Reble reads the dependencies directly from the
SQL, builds the tables on branches (zero-copy, isolated from
production), and shows you the row-level diff before anything ships.
When you're happy, one command promotes it. No staging warehouse, no
clones, no merges.

The biggest difference is what happens when you change something:

- **dbt**: you edit the model, `dbt build` re-runs it against your
  warehouse (or a staging copy), and you compare outputs by hand.
- **Reble**: you edit the model, `reble run` rebuilds it on an isolated
  branch, `reble diff` shows the exact rows that changed, and
  `reble promote` applies it — or refuses if upstream data moved.

Migration is simple: remove the `ref()` calls from your dbt models
(Reble reads the table references from the SQL itself), strip the Jinja,
keep the SQL. Your `models/` directory is already a Reble project.

**Pick dbt if** you're deeply invested in the ecosystem (packages,
macros, tests, docs site) and your team knows the workflow.

**Pick Reble if** you want branching, row-level diffs, and safe
promotion without the Jinja/YAML/compile stack — or if you never used
dbt and just want your SQL to run safely.

## Why not a dbt extension?

Because the model contract is the product, and they're incompatible.

Reble models are plain SQL — parseable, AST-hashable, zero-config. Scope
inference (what changed, what's downstream) works in milliseconds because
SQLGlot reads the SQL directly. dbt models are Jinja templates plus YAML —
not parseable until after `dbt compile`, which means every scope check
carries the full dbt runtime (project, profiles, macros) as a dependency.

Branching also requires table-format control that dbt deliberately
abstracts away. Reble's core is native Iceberg branch refs, tag-based
pinning, and CTAS-on-branch-ref writes with provenance in every snapshot.
dbt's adapter model exists to *hide* the table format — retrofitting
branch refs into that is a fork, not an extension.

And the market is bigger than dbt. A dbt extension serves dbt shops.
Reble serves dbt shops (your SQL ports almost unchanged — remove `ref()`
calls, keep the SQL), plus the SQL+Airflow teams, plus the teams with
internal webapps. Three audiences, one engine.

| | dbt extension | Reble standalone |
| --- | --- | --- |
| Model format | Jinja + YAML | plain SQL |
| Scope inference | after `dbt compile` | direct SQLGlot parse |
| Dependencies | dbt-core, profiles, macros | none beyond SQL files |
| Table format control | through adapter abstraction | direct Iceberg |
| Serves | dbt teams | dbt + SQL + webapp teams |
| Complexity surface | dbt's entire stack | the SQL file |

**The one-sentence version:** a dbt extension would be branching bolted
onto a complex stack; Reble is branching built into a simple one — and
the simplicity is the product.

## Summary

| | Reble | lakeFS | Nessie | Warehouse clones |
| --- | --- | --- | --- | --- |
| What it versions | Iceberg tables | Objects | Catalog state | Platform tables |
| Something to run | Nothing (your catalog) | lakeFS server | Nessie + database | Nothing (the warehouse) |
| Open storage | ✓ (yours) | ✓ | ✓ | ✗ (platform) |
| Scope of a branch | SQL-lineage-inferred | Prefix you name | Whole catalog | What you clone |
| Diff | Row-level, keyed | Object lists | — | Platform-dependent |
| Merge | Never (FF or re-run) | Three-way | Three-way | n/a |
