---
title: "How Reble works"
description: "The architecture: one core, engines, catalog, state."
---
import Mermaid from "../../components/Mermaid.astro";

Reble is a thin layer over three things you already own: your SQL, your
Iceberg catalog, and your storage. This page is the tour of what sits in
between — the [Iceberg refs deep dive](iceberg-refs.md) covers the
primitive underneath it all.

<Mermaid>{`flowchart TB
    WHO["who triggers — cron · CI · Airflow · AI agents (MCP)"]
    MODELS["your models — models/*.sql, plain SQL + a 3-line header"]
    REBLE["Reble — SQLGlot lineage · scope · pin · run · diff · promote"]
    ENGINE["compute — DuckDB (default) · Spark (same interface)"]
    CAT["your Iceberg catalog — Glue · Polaris · Nessie · Hive · REST · sql"]
    STORE[("your storage — S3 · GCS · local disk")]
    WHO -->|"invokes one verb"| REBLE
    MODELS --> REBLE
    REBLE --> ENGINE
    ENGINE -->|"branch refs · tag pins · snapshots"| CAT
    CAT --> STORE
`}</Mermaid>

## The layers

**Lineage (SQLGlot).** Every verb starts by parsing `models/**/*.sql`.
Table references to other models are edges — the DAG is read from the SQL,
never configured. The same parse produces an AST hash per model: cosmetic
edits (whitespace, comments, casing) hash identically and never trigger
work.

**Scope.** A change's blast radius: your edited models plus their
downstream closure. Scope is computed, not declared — that is why a run
touches exactly the tables that could change and nothing else.

**Engine.** The execution layer: resolve every input read to a pinned
snapshot, rewrite the SQL to read those views, execute, and write the
result to the branch ref. DuckDB and Spark implement the same one-method
contract — see [Engines](engines.md).

**Catalog (pyiceberg).** All ref operations — creating branches, pinning
tags, fast-forwarding, drift checks — go through pyiceberg against your
catalog. Reble is not a catalog; both your other tools and Reble see the
same refs.

**State (SQL).** The change-set ↔ branch mapping, per-model hashes, pins,
promote records, and run manifests live in SQLite locally or Postgres for
shared teams. Validated at startup; every verb is safe to retry because
per-model and per-table progress is persisted as it commits.

## A run, step by step

1. **Resolve the change-set.** `--change-set` → `REBLE_CHANGE_SET` → git
   branch (when `git_sync`) → `local`. The change-set is the state key; a
   data branch name is derived from it.
2. **Compute scope.** Edited models (git diff ∪ AST-hash change) plus
   downstream closure. Nothing edited and `--refresh`? Scope is the models
   whose upstream snapshots moved. `--force`? Everything.
3. **Register the branch.** For each scope table: if it doesn't exist,
   it is born with a zero-row seed snapshot on main (marked
   `reble.seed`), then branched from it. Main stays empty until promote.
4. **Pin inputs.** Every upstream table gets an Iceberg tag at its current
   main head. Reruns read through the tag — inputs hold still even while
   production keeps ingesting.
5. **Execute.** Per model, in topological order: register pinned input
   views, execute the rewritten SQL, overwrite the branch head
   (replace-never-append) with provenance keys riding the snapshot
   summary.
6. **Record.** Hashes, pins, and base heads persist per model as they
   commit — a crashed run resumes without redoing completed models.

## Diff, status, promote

**diff** registers the branch snapshot and base snapshot as views and
computes keyed row deltas in SQL — no data movement. PR-time diffs are
advisory.

**status** compares each pin tag against main's current head. Equal →
clean. Not equal → drift, exit `3`. This is the CI gate.

**promote** re-checks drift, then fast-forwards each table's main to the
branch head — a metadata operation per table. If anything drifted, the
safe path is a forced scoped re-run and a fresh, authoritative diff. There
is no merge step anywhere: promote or discard.

## Design rules that keep it honest

- **One core, every frontend.** CLI, `--json`, MCP tools, events — all
  drive the same verbs with the same semantics. No privileged paths.
- **Bots are first-class users.** Stable envelope, documented exit codes,
  NDJSON event streams, idempotent verbs.
- **Your infrastructure stays yours.** Catalog, storage, scheduler —
  unchanged. Reble owns the transformation layer and the branch workflow.
- **No new daemons.** The CLI is a step, not a service. The MCP server is
  the only long-lived process, and only when you choose to run it.
