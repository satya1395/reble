---
hide:
  - navigation
---

# Reble

**An open SQL engine for your Iceberg lakehouse.**

Your models are plain SQL files; Reble derives their dependencies, builds
the tables into your catalog and bucket, and refreshes exactly what moved —
triggered by cron, CI, Airflow, or an agent. And the engine is
branch-capable at the core: every change can run on an isolated zero-copy
branch where you review the exact rows it changes, then fast-forward
production. No warehouse server, no clones, no merges.

!!! note "Status: early, but real"

    The full loop works today — `init → run → diff → status → promote`, in
    git or standalone, with 75 passing tests on Linux and macOS. `pip install reble`
    and go. Feedback is by far the most valuable contribution right now —
    [open a discussion](https://github.com/satya1395/reble/discussions) —
    and the [docs site](/) you're reading is built from this repo.

## The problem

To test a change to *one* model, data teams either re-run the pipeline
against a copy of the warehouse — slow, expensive, and stale the moment prod
ingests — or they cross their fingers on production. Neither gives you an
answer to the only question that matters before shipping: **what rows would
this change?**

## The loop

```bash
pip install reble
reble init --catalog sql --namespace analytics   # local catalog, no infra

git switch -c fix-orders        # branch in git…
# ...edit models/stg_orders.sql...
reble run                       # …and the warehouse follows: a zero-copy data
                                # branch of exactly the tables you touched
reble diff                      # rows, not lines: +1,204 -0 ~312 changed
reble status                    # un-run edits, drifted pins, branch age
reble promote                   # fast-forward main — or a scoped re-run with a
                                # fresh diff if main moved. No merge. Ever.
```

That's the whole product. Start with a story you've lived —
[Days you've had](scenarios.md) — see exactly
[what Reble replaces](replaces.md) in your stack, or walk the loop end to
end in [Getting started](getting-started.md).

## Just SQL — no dbt required

A **model** is just Reble's word for *one SQL file that produces one
table*. Most teams already have these — a folder of query files and some
way of running them in order. If that's you:

- **Your SQL files are models as-is.** One file = one table; a three-line
  comment header (`-- key: order_id`) is the only metadata, and even that
  is optional.
- **The dependencies you configure by hand** — in a DAG file, a UI, or a
  config — **are parsed from the SQL itself.** A table reference to
  another model is an edge; ordering and blast radius come from lineage,
  not from clicking.
- **Your scheduler stays your scheduler.** Airflow, cron, or the internal
  webapp keeps deciding *when*; Reble takes over *what and how*: scoped
  builds, isolated branches, row-level diffs, safe promotion.

dbt users are welcome (your SQL ports almost unchanged), but nothing
assumes dbt.

![The Reble loop](assets/demo.gif)

## Where it sits

```mermaid
flowchart TB
    WHO["who triggers — cron · CI · Airflow · AI agents (MCP)"]
    MODELS["your models — models/*.sql, plain SQL + a 3-line header"]
    REBLE["Reble — SQLGlot lineage · scope · pin · run · diff · promote"]
    ENGINE["compute — DuckDB (today), Spark (behind the same interface)"]
    CAT["your Iceberg catalog — Glue · Polaris · Nessie · Hive · REST · sql"]
    STORE[("your storage — S3 · GCS · local disk")]
    WHO -->|"invokes one verb"| REBLE
    MODELS --> REBLE
    REBLE --> ENGINE
    ENGINE -->|"branch refs · tag pins · snapshots"| CAT
    CAT --> STORE
```

Reble owns the transformation layer (the shape dbt-core has); your
scheduler owns *when*; your catalog and bucket stay yours. State lives
in SQLite locally or in [Postgres for shared teams](airflow.md#reble-in-production-airflow)
— one config line.

## What makes it different

A branch is **metadata only**. The tables you're changing get zero-copy
Apache Iceberg branch refs; the upstream inputs are pinned with Iceberg tags
at the moment you run, so your inputs hold still while you iterate — even
while production keeps ingesting. Branching costs nothing until you write.

There is deliberately **no merge**. Promote is a fast-forward when your
pinned inputs still match production, and a scoped re-run when they don't.
Data merges with conflict resolution silently corrupt warehouses; Reble
refuses to build one. The full argument is in
[Concepts](concepts.md#where-the-analogy-breaks-merge).

And it runs against **the catalog you already have** — Glue, Polaris,
Nessie, Hive, or any REST-compliant Iceberg catalog. Reble is not a catalog,
not an orchestrator, and not a new thing to operate. How that stacks up
against lakeFS, Nessie, and warehouse clones:
[Comparisons](comparisons.md).
