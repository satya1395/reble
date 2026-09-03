---
title: "Engines"
description: "DuckDB by default, Spark when transforms outgrow it."
---
Reble's engine interface is one method: execute a model's SQL against
pinned input snapshots and write the result to a branch. Two engines
implement it today — **DuckDB** (default, embedded, zero setup) and
**Spark** (embedded PySpark, for transforms that outgrow a single DuckDB
process). Same contract either way: scoped runs, tag-pinned inputs,
provenance in the snapshot summary, branch writes, main untouched until
promote.

## Picking

**DuckDB** is the right default. Reads stream out-of-core through
`iceberg_scan` and spill under `memory_limit` — measured, a 1M-row
lifecycle over S3 runs in ~30s from a laptop ([numbers](performance.md)).
It starts in milliseconds and has no JVM.

**Spark** is for the jobs DuckDB genuinely can't do: joins whose working
set exceeds single-node spill, or transforms that lean on Spark UDFs you
already have. `pip install 'reble[spark]'`, then:

```yaml
compute_policy:
  prefer: spark   # or per-run: reble run --engine spark
```

or override per run: `reble run --engine spark`.

## Spark configuration

`engines.spark` in reble.yml:

```yaml
engines:
  spark:
    master: local[*]        # default — no cluster required
    app_name: reble
    settings:               # raw Spark conf passthrough
      packages: org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.1
```

`packages` defaults to the Iceberg Spark runtime matched to PySpark 3.5
(plus `iceberg-aws-bundle` for Glue catalogs, plus a JDBC driver for sql
catalogs). First run downloads the jars (ivy cache) — later runs are
cached.

### Catalogs

The Spark engine maps your existing reble.yml catalog to a Spark catalog
and points both engines at the *same* backend, so branches and tags
created by either side are visible to both:

| reble.yml type | Spark catalog | notes |
| --- | --- | --- |
| `glue` | Glue catalog | aws-bundle jar included |
| `hive` | Hive catalog | |
| `rest` / `polaris` / `nessie` | REST catalog | same `uri` |
| `sql` | JDBC catalog | **postgresql only** — SQLite holds a database-level lock that Spark's connection pool and pyiceberg would fight over; a server database lets both writers interleave safely |

Reads resolve through Spark version travel — `tbl VERSION AS OF
<snapshot_id>` — the pinned-snapshot equivalent of DuckDB's
`iceberg_scan(snapshot_from_id=…)`. Writes go through Iceberg's
DataFrameWriterV2 with `snapshot-property.*` options, which is the only
path that carries provenance into the snapshot summary on Spark 3.5.

## Honest limits

- **Diffs still run on DuckDB** regardless of engine. Diffing is read-only
  compute; DuckDB spills under `memory_limit`. If real workloads hit that
  wall, a Spark diff path is a straightforward follow-up.
- The Spark engine writes **unpartitioned tables**, like the DuckDB
  engine — v0 model outputs are flat full-refresh builds.
- A JVM (17+) is required. macOS/Linux x86_64/arm64 both work.
