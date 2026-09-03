---
title: "How Reble works"
description: "The loop end to end: lineage, scope, branch, pin, execute, diff, promote."
---
import Mermaid from "../../components/Mermaid.astro";

This is the one page to read if you want to understand the product before
using it. It walks the whole loop and names every part; the chapters after it
go deeper on each.

Reble is a layer over three things you already own: your SQL, your Iceberg
catalog, and your storage.

## The loop

<Mermaid>{`flowchart LR
    M[("main<br/>(Iceberg tables)")]
    E["edited SQL"] -->|"scope: AST-changed ∪<br/>downstream closure"| RUN["reble run"]
    M -->|"upstream inputs pinned<br/>via Iceberg tags"| RUN
    RUN -->|"zero-copy branch refs"| B[("data branch")]
    B --> D["reble diff<br/>rows + schema"]
    D --> P{"reble promote"}
    P -->|"pinned bases still<br/>equal main"| FF["fast-forward main"]
    P -->|"drift"| RR["scoped re-run +<br/>fresh promote-time diff"]
    RR --> FF
`}</Mermaid>

Five verbs, in the order you use them: `run` builds your change in isolation,
`diff` shows what it does to the rows, `status` says whether production moved
underneath you, and `promote` applies it — or `branch discard` throws it away.

## The layers

**Lineage (SQLGlot).** Every verb starts by parsing `models/**/*.sql`. Table
references to other models are edges, so the graph is read from the SQL and
never configured. The same parse produces an AST hash per model, which is why
cosmetic edits never trigger work.

**Scope.** Your edited models plus their downstream closure. Scope is
computed, not declared, so a run touches exactly the tables that could change
and nothing else.

**Engine.** Resolve every input read to a pinned snapshot, rewrite the SQL to
read those views, execute, write the result to the branch ref. DuckDB and
Spark implement the same one-method contract — see
[Engines](/reble/engines/).

**Catalog (pyiceberg).** Every ref operation — creating branches, pinning
tags, fast-forwarding, checking drift — goes through pyiceberg against your
catalog. Reble is not a catalog. Your other tools and Reble see the same refs.

**State.** The change-set ↔ branch mapping, per-model hashes, pins, promote
records, and run manifests. SQLite locally, Postgres for shared teams.
Validated at startup. Every verb is safe to retry because per-model and
per-table progress is persisted as it commits.

## A run, step by step

1. **Resolve the change-set.** `--change-set` → `REBLE_CHANGE_SET` → git
   branch (when `git_sync`). The change-set is the state key; the data branch
   name is derived from it.
2. **Compute scope.** Edited models (git diff ∪ AST-hash change) plus
   downstream closure. With `--refresh`, the models whose upstream snapshots
   moved. With `--force`, everything.
3. **Register the branch.** For each table in scope: if it does not exist, it
   is created with a zero-row seed snapshot on `main` (marked `reble.seed`)
   and branched from that. Main stays empty until promote.
4. **Pin inputs.** Every upstream table gets an Iceberg tag at its current
   main head. Reruns read through the tag, so inputs hold still even while
   production keeps ingesting.
5. **Execute.** Per model, in topological order: register pinned input views,
   execute the rewritten SQL, overwrite the branch head, and write provenance
   into the snapshot summary.
6. **Record.** Hashes, pins, and base heads persist per model as they commit,
   so a crashed run resumes without redoing completed models.

## Diff, status, promote

**diff** registers the branch snapshot and the base snapshot as views and
computes keyed row deltas in SQL. No data moves. Diffs taken before promote
are advisory.

**status** compares each pin tag against main's current head. Equal is clean;
not equal is drift, exit `3`. This is the CI gate.

**promote** re-checks drift, then fast-forwards each table's main to the
branch head — a metadata operation per table. If anything drifted, it forces
a scoped re-run and produces a fresh, authoritative diff. There is no merge
step anywhere.

## If you know git

The mapping is close enough to be useful, and the one place it breaks is the
important one.

| Git | Reble |
| --- | --- |
| Repository | Iceberg catalog (the one you already run) |
| Branch | Data branch: zero-copy Iceberg refs, per changed table |
| Working tree | `models/**/*.sql` — plain SQL files |
| `git diff` | `reble diff` — rows, not lines |
| `git status` | `reble status` — un-run edits, drifted pins |
| Commit | A run: scoped materialization on the branch |
| `git merge --ff-only` | `reble promote` |
| `.gitignore` | `.reble/` — machine-local state |
| `git merge` (three-way) | **Nothing. Deliberately absent.** |

Why the last row is empty is argued in
[Branches and promotion](/reble/branches/#there-is-no-merge).

## Where it sits

Reble owns the transformation layer — models, lineage, execution, branching —
which is the shape dbt-core has, without the templating or YAML. It does not
own scheduling: cron or Airflow decides when, and Reble is the step they run.
It is not a catalog and requires no new infrastructure.

## Design rules

- **One core, every frontend.** CLI, `--json`, MCP tools, and event streams
  all drive the same verbs with the same semantics. There are no privileged
  paths.
- **Bots are first-class users.** Stable envelope, documented exit codes,
  NDJSON event streams, idempotent verbs. See
  [Exit codes and JSON output](/reble/exit-codes/).
- **Your infrastructure stays yours.** Catalog, storage, and scheduler are
  unchanged.
- **No new daemons.** The CLI is a step, not a service. The MCP server is the
  only long-lived process, and only when you choose to run it.

## Next

- [Models and lineage](/reble/models/) — the unit of work and how scope is derived.
- [Branches and promotion](/reble/branches/) — pins, drift, and applying a change.
- [Running and scheduling](/reble/running/) — change-sets, retries, and cron.
- [Iceberg refs](/reble/iceberg-refs/) — the primitive underneath, if you want it.
