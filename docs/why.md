# Why Reble Exists

## The problem: data teams can't safely try things

Software engineers get branches, cheap CI environments, and reviewable diffs. Data
engineers get none of that. Four gaps, all painfully familiar:

### 1. Environments are all-or-nothing

Testing a pipeline change today means a "dev" or "staging" copy of the **entire
warehouse** — expensive to build, slow to refresh, stale the moment it exists. But
look at any real PR: most changes touch 2–5 tables. Nobody needs a whole second
warehouse; they need *those tables*, isolated, with everything else readable as-is.

Snowflake users discovered this years ago — zero-copy `CLONE` per pull request is a
celebrated CI pattern ("clone, test, drop, on every PR"). It's also locked to
Snowflake. The open-stack equivalents don't exist: dbt's Slim CI approximates it at
the framework layer and is [documented as fragile](https://github.com/dbt-labs/dbt-core/issues/14235)
(state-comparison false positives, incremental models silently full-refreshing,
forgetting `--defer` rebuilding everything at full cost); Nessie and lakeFS branch at
the right layer but branch *everything* and require migrating your catalog or storage
onto them first.

### 2. Test inputs drift under you

Even with a dev environment, upstream tables keep ingesting. Run your model Monday
and Wednesday and the outputs differ — was that your logic change, or new data?
Before/after comparisons are unreproducible exactly when you need them to be exact.

### 3. "What rows does this change?" has no answer

A reviewer approving a data PR is approving SQL text and vibes. The question they
actually want answered — *what does this do to the data?* — requires tooling almost
nobody has. The demand is proven: Datafold's open-source `data-diff` was widely
adopted, then [archived in May 2024](https://www.datafold.com/blog/the-lowdown-open-source-data-diff-vs-datafold-cloud/)
with diffing moved behind a paid cloud product. The open-source gap has been sitting
there since.

### 4. The open stack is real but unassembled

DuckDB + Iceberg + SQLGlot is a genuinely great analytics stack — embedded compute,
open table format with time travel, and a parser that can read dependencies, column
lineage, and change detection straight out of plain SQL. Small teams are noticing; "lakehouse on a laptop" went from meme to
practice. But wiring it together — catalog, storage layout, environments, CI — is a
weekend of yak-shaving per team, redone everywhere, slightly differently, usually
without branching at all.

## What Reble does about it

Reble is a single CLI that ships that stack pre-wired, and adds the missing
primitive: **subset branching**. The deployment shape matches how data teams
actually live: **your data stays in your bucket** (S3/GCS — or local disk while
trying it out), and compute is a single fast node — your laptop for development,
a CI runner for checks. No cluster, no migration.

A branch scopes to the tables you're changing. Those get zero-copy, copy-on-write
Iceberg refs — writable, invisible to prod. Every *other* table is read from prod,
**pinned to its snapshot at branch creation**, so your inputs hold still while you
work. A branch is pure metadata: branching a 10GB table costs nothing until you write.

There is deliberately **no merge**. Branches are ephemeral: create, test, then
*promote* (fast-forward the refs when prod hasn't moved; rerun on main when it has)
or discard. Data-level merges with conflict resolution silently corrupt warehouses —
we refuse to build one.

On top of that primitive, the killer workflow: **branch-per-PR CI**. Open a pull
request and a GitHub Action creates a scoped branch, runs only the changed models,
and posts a comment a reviewer can actually act on. Merge promotes; close cleans up.
This is [write-audit-publish](https://lakefs.io/blog/how-to-implement-write-audit-publish/),
made turnkey.

### It works for both kinds of data engineering

- **Modifying an existing transformation?** The PR comment shows a **diff** — schema
  changes, row-level added/removed/changed — plus column-lineage-derived downstream
  impact: *this change to `orders.amount` affects these 3 models.*
- **Building a new model on existing tables?** There's nothing to diff, so you get a
  **profile** instead — schema, row counts, null rates, sample stats, and the
  upstream columns you depend on. Meanwhile the branch gives you what greenfield work
  actually needs: inputs that don't drift while you iterate, and isolation so your
  half-built table never leaks into the prod namespace where a BI tool can find it.
  When you promote, your model joins the lineage graph automatically — which is what
  makes the *next* person's impact analysis trustworthy.

And because branches are independent refs with serialized, lineage-aware promotes,
**two engineers on two branches need no coordination until promote** — something
neither Slim CI nor manual clone discipline gives you structurally.

## Why now

- **The transform stack is being reevaluated.** dbt's Fusion transition and licensing
  turbulence pushed many teams to reassess for the first time in years. Reble's answer
  is deliberately minimal: **your models are just SQL files** — no headers, no Jinja,
  no per-model YAML — with dependencies and lineage inferred by
  [SQLGlot](https://github.com/tobymao/sqlglot), the same parser foundation the
  ecosystem's transform and lineage tools are built on. Importers for dbt
  (`{{ ref('...') }}`) and SQLMesh (`MODEL(...)`) projects are on the roadmap, so
  reevaluating doesn't mean rewriting.
- **Iceberg won the table-format question.** Native per-table branching, time travel,
  and an ecosystem from DuckDB to Snowflake. The primitives Reble needs shipped;
  nobody had composed them into this workflow.
- **The pieces are proven, today.** This repo's [`spikes/`](../spikes/) directory
  contains reproducible scripts validating the full loop on current releases:
  zero-copy branch refs, isolated branch writes, pinned reads, one-line promote
  (spike 1); laptop-scale performance at 10GB (spike 2); and the full SQLGlot-direct
  core — deps, lineage, change-detection hashing, and a ~20-line runner executing
  over Iceberg-backed views (spike 4). All green.

## What Reble is not

- **Not a query engine, storage engine, or table format.** We compose DuckDB,
  Iceberg, and SQLGlot, and add the branch layer and glue.
- **Not a Snowflake competitor.** The target is small teams — one fast node over
  data in your bucket, correct and pleasant at tens of gigabytes, not petabytes on
  a cluster.
- **Not a data merge tool.** Promote or discard. Never merge.
- **Not a migration.** Subset branches work over standard Iceberg catalogs — the
  long-term goal is branching *your existing* lakehouse, not moving it.

## The one-sentence version

> Reble is the `git init` of lakehouses: one tool, zero services, and every pipeline
> change — new or modified — gets its own cheap, isolated, reviewable branch.
