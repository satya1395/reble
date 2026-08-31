# Reble

**Branch your warehouse like you branch your code.**

Reble is a single CLI that gives a small data team a complete local-first analytics
platform — DuckDB + Apache Iceberg + SQLMesh pre-wired — with subset branching of the
warehouse and branch-per-PR CI for data pipelines.

> ⚠️ **Status: design phase / pre-alpha.** The architecture is documented, the code is
> not written yet. Star the repo to follow along; feedback on the design is the most
> valuable contribution right now — open a Discussion.

**→ [Why Reble exists](docs/why.md)** — the full story: the four gaps in data
engineering workflows, why existing tools don't close them, and why now.

## The idea

Testing a data pipeline change today means cloning or rebuilding an entire dev
warehouse — even when your change touches three tables. Reble branches **just the
tables you're changing**:

```bash
pip install reble
reble init my-warehouse
# edit your models…
reble branch create fix-orders          # scope + pins inferred from your changes —
                                        # your edited models, their downstream
                                        # cascade, and their upstream inputs
reble run                               # writes go to zero-copy Iceberg branch refs;
                                        # inputs read prod as of the branch epoch
reble diff                              # schema + row-level diff vs your branch base
reble promote                           # atomic fast-forward to main, clean up
```

Both git orders work: edit-first (scope inferred from the diff) or branch-first
(empty scope + frozen epoch; the scope grows automatically at first run, and reads
resolve as of the moment you branched).

- **Zero-copy branches** — branched tables use native Iceberg refs (copy-on-write);
  a branch of a 10GB table costs ~nothing until you write.
- **Pinned inputs** — unbranched tables are read at their snapshot from branch-creation
  time, so your test inputs don't drift while prod keeps ingesting.
- **Row-level diffs** — answer the question every reviewer actually has: *what rows
  does this change?*
- **Column-level lineage & change detection** — via SQLMesh, embedded: only changed
  models run, downstream impact is shown before you apply.
- **No merge, ever** — branches are ephemeral: create, test, promote (fast-forward or
  re-run) or discard. We refuse to build last-write-wins data merges.

## Measured, not promised

The design is validated by reproducible spikes in [`spikes/`](spikes/), including a
full-scale performance run — **140M rows / 10.22GB** on an Apple M4 Pro laptop
(pyiceberg 0.11.1, DuckDB 1.5.5):

| Operation at 10GB scale | Time |
|---|---|
| Create a branch of the 140M-row table | **< 10ms** (zero-copy, size-independent) |
| Pinned full-table scan → Arrow | 4.0s |
| Projected scan (2 of 6 columns) | 0.47s |
| Full diff — both refs scanned, added + changed rows | **5.9s** |
| Branch append (5M rows) | 1.3s |
| Bulk load throughput | ~3.5M rows/s |

Peak RAM 12.3GB, 3.5GB on disk (Parquet ≈ 2.9× compression). Details and the scripts
to reproduce: [spike 1 — branch lifecycle](spikes/01-pyiceberg-branches/RESULTS.md) ·
[spike 2 — performance](spikes/02-perf/RESULTS.md) ·
[spike 3 — SQLMesh embedding](spikes/03-sqlmesh-embedding/RESULTS.md).

## The killer workflow: branch-per-PR

A GitHub Action that, on every pull request:

1. Creates a branch scoped to the changed models' tables
2. Runs only the changed models
3. Posts a PR comment: models changed, downstream impact, row-level diff stats
4. Promotes on merge, cleans up on close

Data PRs become reviewable like code PRs.

## Local-first, zero services

Everything runs on a laptop: DuckDB embedded, Iceberg on local filesystem, SQLite
catalog, SQLMesh in-process. No Docker, no daemons. The same project moves to team
mode (S3/MinIO + Postgres or REST catalog) via config.

## What Reble is not

- Not a query engine, storage engine, or table format — it composes DuckDB, Iceberg,
  and SQLMesh and adds the branching layer and glue.
- Not a Snowflake competitor — the target is small teams and the local/CI loop.
- Not a full-catalog branching system (see Nessie/lakeFS for that) — Reble branches
  subsets over standard Iceberg catalogs, no migration required.

## Design docs

- [Why Reble exists](docs/why.md) — motivation and positioning
- [Architecture & implementation plan](docs/architecture.md)
- [Getting started](docs/getting-started.md) *(will be real once code exists)*
- [Validated spikes](spikes/) — reproducible proof the core primitives work today

## Contributing

Design feedback > code right now. Open a [Discussion](../../discussions) or an issue.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
