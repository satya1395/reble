# Reble Architecture

**Scope:** A single CLI that gives a data team of any size a complete analytics
platform — what's sized is the run, not the org: each run is single-node over
just the tables it touches —
DuckDB + Iceberg + SQLGlot-powered transforms pre-wired — with **subset branching**
of the warehouse and **branch-per-PR CI** as the killer workflow. Local compute over
cloud storage: your laptop (or a CI runner) is the query engine; your bucket is the
warehouse — with a zero-services all-local mode as the on-ramp.
**Models are plain SQL files** (filename = table name); dependencies, lineage, and
change detection are inferred from the SQL itself.

For the motivation and positioning, see [why.md](why.md). For proof the primitives
work at target scale, see the reproducible [spikes](../spikes/).

---

## 1. Product shape

```
$ reble init my-warehouse        # scaffold project (plain-SQL models, config, local catalog)
$ reble run                      # run models against main
$ reble branch create fix-orders # scope + pins inferred from your edits
$ reble run                      # runs on branch: writes go to branched refs,
                                 # inputs read prod as of the branch epoch
$ reble diff                     # schema + row-level diff of branched tables vs main
$ reble promote                  # apply the change to main; delete branch
```

And in CI (the flagship workflow):

```yaml
# .github/workflows/reble.yml
- uses: rebleio/reble-action@v1   # on PR: branch named pr-123, run changed models,
                                  # post diff + impact comment; on merge: promote;
                                  # on close: delete branch
```

Two modes, one tool. The **on-ramp** runs entirely on a laptop with zero services:
DuckDB embedded, Iceberg on local filesystem, SQLite-backed catalog, transforms
in-process. **Production is team mode** — the same project pointed at S3/GCS plus a
shared Postgres/REST catalog, because that's where real warehouses live; the laptop
(or CI runner) remains the query engine. Nothing in the branch model depends on
local storage: branches and pins are catalog metadata, so zero-copy creation and
epoch semantics hold identically over object storage — the only delta is scan
latency, mitigated by lineage-driven projection pushdown. Team-mode latency will be
measured (spike 6: MinIO for correctness, real S3 for numbers) before any
performance claim is made for it; the published benchmarks are local-NVMe.

---

## 2. The branch model (core primitive)

A branch is **metadata only** — no data is copied:

```
branch "pr-123":
  epoch: 2026-08-30T14:02:11Z     # the branch point — all unscoped reads
                                  # resolve "as of" this moment
  scope:                          # tables being changed (writable)
    analytics.orders        -> iceberg ref "pr-123"        (copy-on-write)
    analytics.order_totals  -> iceberg ref "pr-123"        (copy-on-write)
  pins:                           # upstream inputs (read-only, frozen at epoch)
    raw.events              -> snapshot 9931
    raw.customers           -> snapshot 4812
  created_by / ttl
```

- **Branched tables** use Iceberg's native per-table refs. Writes create new data
  files under the branch ref; the `main` ref never sees them.
- **Unbranched tables** are read through to prod but **pinned as of the branch
  epoch** — the moment the branch was created. Test inputs are stable and diffs are
  reproducible even while prod keeps ingesting, and pinning is free (Iceberg time
  travel). Pins are **lineage-scoped**: only the transitive upstream inputs of the
  scoped models are pinned (change 2 models in a 100-model warehouse and you pin
  their 4 inputs, not 96 tables) — anything else is simply irrelevant to the branch.
  `--pin-all` forces blanket pinning for warehouses where external writers touch
  tables the model graph can't see.
- **Scope inference (shipped):** `reble branch create <name>` needs no arguments —
  the model-graph diff supplies the scope (your changed models *plus* their
  downstream cascade) and the dependency graph supplies the pins. `--tables` remains
  as an explicit override.

**Both git orders work.** Like git, you can edit first or branch first:

1. *Edit-first* (shipped): change your models, then `reble branch create fix` —
   the diff against prod tells Reble exactly what to scope and pin.
2. *Branch-first* (shipped): `reble branch create fix` on a clean tree
   creates the branch with an **empty scope and a frozen epoch**. Edit models, then
   `reble run` — the plan reveals what changed, and the scope **grows lazily at run
   time**: refs are created for the changed tables *from their epoch snapshots* (not
   current main), so a branch created Monday and first run Wednesday still computes
   against Monday's inputs. Strict epoch reproducibility is the contract — the
   branch point is when you branched, exactly as in git.

**Promote ≠ merge.**
1. If the branched tables' `main` refs haven't advanced since branch creation →
   fast-forward `main` to the branch ref (cheap, atomic per table).
2. Otherwise → re-apply against main (rerun the changed models there).
   No data-level merge, ever. Last-write-wins data merges silently corrupt warehouses;
   Reble refuses to build that. Branches are ephemeral: create, test, promote or discard.

**Two workflows, one loop.** Modified models get a **diff** (schema, row-level
added/removed/changed) plus column-lineage-derived downstream impact. A **new** model
has no counterpart to diff, so it gets a **profile** instead — schema, row counts,
null rates, sample stats, upstream dependencies. Both flow through the same PR
comment; the branch earns its keep for greenfield work through pinned inputs during
iteration, isolation of work-in-progress tables, and automatic registration in the
lineage graph on promote.

**Model format & engine.** A model is a plain SQL file: `models/demo/orders.sql`
defines table `demo.orders`, full stop. SQLGlot parses each file once and supplies:

- **dependencies** — table references read from the AST (CTEs excluded), which drive
  scope inference, pins, and topological execution order;
- **column-level lineage** — for impact analysis and lineage-aware promotes;
- **change detection** — fingerprints hash the *canonical* AST (whitespace, comments,
  and keyword case don't count) composed with upstream fingerprints, so a change to
  a model re-runs it and its downstream cascade, and nothing else.

**Model configuration (the "no YAML" fine print).** The claim is *zero boilerplate
per model*, not "no configuration exists": a model with no config entry is a FULL
rebuild, always — no registration required. Models that need non-default behavior
(incremental materializations, v0.2) get a few lines in the **one `reble.yml` the
project already has** — never a per-model sidecar, never a header in the SQL:

```yaml
# reble.yml
models:                        # only the exceptions live here
  demo.events:
    kind: incremental
    time_column: event_ts
  demo.big_fact:
    kind: incremental
    unique_key: order_id       # upsert semantics via pyiceberg
```

Two rules keep this from rotting: (1) **config is validated against the AST** — if
`time_column: event_ts` names a column the model's SQL never references, that's a
loud plan-time error, so config can't silently drift from the SQL; (2) magic-comment
pragmas are deliberately rejected — comment-config fails silently on typos and is a
header in disguise.

There is deliberately no second environment system: Iceberg branch refs are the one
and only isolation layer, for model outputs and raw tables alike.

**Concurrency & team workflow.** Multiple engineers work on branches simultaneously
(independent refs; requires the shared team-mode catalog — local SQLite is
single-user) and promote sequentially:

1. **Promotes are serialized** (promote lock / merge-queue semantics; maps 1:1 onto
   PRs merging one at a time in CI).
2. **Disjoint scopes:** after someone else promotes, your branch's promote runs a
   **lineage-aware staleness check** — if none of your models read the tables that
   advanced, fast-forward proceeds; if they do, promote requires a rebase.
3. **Overlapping scopes:** the clean/dirty check fails (your table's main ref moved)
   → fast-forward refused, rebase mandatory. Reble warns at branch *creation* when a
   table in scope is already branched by someone else.
4. **`reble branch rebase`** is a first-class command: re-pin upstreams to current
   main, rerun changed models (cheap via fingerprint change detection), re-validate.
   Rebase-then-promote is the only path forward for a stale branch — never data merge.

---

## 3. System architecture

```
┌────────────────────────────────────────────────────────────┐
│  reble CLI (Python, one entrypoint)                        │
│  init · branch · run · query · diff · promote · gc · ci    │
├────────────────────────────────────────────────────────────┤
│  Branch Engine                                             │
│  branch manifests · snapshot pinning · write guards ·      │
│  promote (fast-forward / re-apply) · TTL + GC              │
├──────────────────────────┬─────────────────────────────────┤
│  Catalog Resolver        │  Runner (SQLGlot-direct)        │
│  wraps the underlying    │  parse -> deps -> fingerprints  │
│  Iceberg catalog; every  │  -> topo order; DuckDB computes │
│  table lookup resolves   │  over Iceberg-backed views;     │
│  through branch context  │  pyiceberg commits to the ref   │
├──────────────────────────┴─────────────────────────────────┤
│  Storage & catalog                                         │
│  local:  filesystem warehouse + pyiceberg SQL catalog      │
│          (SQLite)                                          │
│  team:   S3/MinIO + Postgres SQL catalog or existing       │
│          REST catalog                                      │
└────────────────────────────────────────────────────────────┘
```

### Query flow in team mode (where the engine lives)

The query engine is **DuckDB, embedded as a library inside the `reble`
process** — on whatever machine runs the CLI. It is not a service; S3 is pure
storage and never executes anything. On `reble run` against `s3://`:

1. **Metadata first (KBs):** pyiceberg GETs the Iceberg metadata + manifests,
   learning exactly which Parquet files/row-groups belong to the pinned
   snapshot each read resolves to — pins cost nothing because choosing a
   snapshot is choosing a file list.
2. **Data, surgically:** ranged GETs for only the column chunks the model's
   SQL references (Parquet is columnar; the lineage-driven projection rule
   applies on the network, not just RAM) → Arrow batches in process memory.
3. **Compute on the local CPU:** DuckDB executes the SQL over those buffers
   in-process.
4. **Write-back:** results land as new Parquet in the bucket plus a tiny
   metadata commit moving the *branch ref*. Promote is another pointer move.
5. **The process exits.** Nothing persists locally except the small branch
   manifest state. Any machine with the CLI and bucket credentials is a full
   query engine for exactly as long as it needs to be — which is why CI
   runners work as team compute.

The contrast with a hosted warehouse: the identical steps happen there too,
inside the vendor's VPC on the vendor's metered compute. Reble deletes the
middle tier — same physics, no meter.

**Memory model and the scale ceiling (honest version).** Two memory stories per
run: DuckDB's *execution* is not RAM-bound — joins/sorts/aggregations spill to
disk past `memory_limit`. The *input* step currently is: each needed scan is
materialized fully into Arrow RAM before execution, so peak memory ≈ the
projected columns of the tables the changed models read (measured: 10GB table
→ ~12GB RSS). This is an implementation choice, not an architectural wall —
pyiceberg's streaming readers (`to_arrow_batch_reader`) feed DuckDB batch-wise
and drop peak RAM to ~batch size; on the roadmap (single-pass streams need
re-scan-per-consumer care). And the format is the escape hatch for real scale:
Reble is single-node by design, but the tables are standard Iceberg — the day
a job outgrows one machine, Spark/Trino read the *same tables* with zero
migration. Reble never needs to become distributed; it needs to never block
your exit to something that is. (Arrow itself is a memory format, not an
engine — it distributes nothing.)

### Load-bearing design decisions

**Language: Python.** SQLGlot, pyiceberg, and DuckDB all live natively in Python;
wrapping them in-process is the whole point. "Single binary" is a distribution concern, not a
language concern: v1 ships via `uv tool install reble` / pipx; a single-file build is
a fast-follow.

**Compute: DuckDB computes, pyiceberg commits.** The DuckDB iceberg extension is
read-oriented; Reble does not depend on DuckDB writing Iceberg. Each run opens an
**ephemeral in-memory DuckDB session**, registers branch-resolved Iceberg scans as
views, executes models in dependency order, and commits outputs to the branch ref
via pyiceberg — data lives in Iceberg and nowhere else; there is no persistent
intermediate database. Validated end-to-end in
[spike 4](../spikes/04-sqlglot-direct/RESULTS.md).

**Projection pushdown everywhere.** At 10GB scale the constraint is RAM, not time
(measured in [spike 2](../spikes/02-perf/RESULTS.md)): every scan — model inputs and
diffs — passes `selected_fields` derived from column lineage, and diffs read key +
compared columns only, never whole rows. This is what keeps target-scale workloads
inside laptop memory.

**Catalog resolution is in-process for local, a REST proxy for team mode.** Locally,
reble wraps the pyiceberg catalog object directly — no server needed. A future
`reble catalog serve` exposes the same resolver as an Iceberg REST catalog proxy so
external engines (Trino, Spark, Snowflake) resolve branches transparently — the
long-term path to branching an *existing* lakehouse with no migration.

**Write guards are non-negotiable.** From a branch context, any write to a table
outside the branch scope is refused with a hard error. One corrupted prod table ends
the project's credibility.

**GC from day one.** Pinned snapshots block Iceberg snapshot expiration on prod
tables. Branches carry a TTL (default 14 days); `reble gc` expires dead branches,
drops their refs, and releases pins. The CI action deletes branches on PR close.

---

## 4. Deliberately out of scope (v1)

- Web UI
- Unity Catalog, Trino, Spark integrations
- Data-level merge of branches
- Incremental (time-interval) materializations — FULL models only in v1; at the
  target scale a full rebuild is seconds (see spike 2)
- Multi-tenant cloud service, auth/RBAC
- Scheduling/orchestration (Airflow etc.)
- Embedded dbt/SQLMesh compatibility — planned as *importers* (`{{ ref('...') }}`
  and `MODEL(...)` translation), not as core dependencies

---

## 5. Validation status

All three feasibility spikes are green, on current releases, with reproducible
scripts in [`spikes/`](../spikes/):

| Spike | What it proves | Status |
|---|---|---|
| [01 — branch lifecycle](../spikes/01-pyiceberg-branches/RESULTS.md) | create / write / isolate / pin / promote / cleanup on Iceberg refs, plus DuckDB reads via Arrow | ✅ |
| [02 — performance](../spikes/02-perf/RESULTS.md) | full loop at **140M rows / 10.22GB** on a laptop: branch create <10ms, full diff 5.9s | ✅ |
| [03 — SQLMesh embedding](../spikes/03-sqlmesh-embedding/RESULTS.md) | *(historical — superseded by spike 4)* headless plan/apply, env-per-branch | ✅ |
| [04 — SQLGlot-direct core](../spikes/04-sqlglot-direct/RESULTS.md) | deps (CTE-safe), topo order, column lineage, cosmetic-vs-semantic fingerprints with upstream cascade, ~20-line runner over Iceberg-backed views onto branch refs | ✅ |

Pinned versions: `pyiceberg==0.11.1`, `sqlglot==30.8.0`, `duckdb==1.5.5` —
managed as a certified set (`requirements-lock.txt`, API-contract tests, weekly
canary against latest). The spikes double as upgrade regression tests.
