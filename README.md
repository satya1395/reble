# Reble

**Git-style branching for data warehouses on Apache Iceberg.** Works with
your Iceberg catalog — bring your own SQL. One branch gesture produces two
artifacts: your code branch and a live, diffable, promotable data branch
holding exactly the change's blast radius.

Stop cloning warehouses to change three tables.

```
pip install reble
reble init                # writes reble.yml; probes your catalog
git switch -c fix-orders  # code branch, as usual
# ...edit two models...
reble run                 # → data branch `fix_orders`: edited models + downstream
                          #   closure written; upstream inputs pinned via Iceberg tags
reble diff                # schema + row-level diff vs. branch base
reble status              # un-run edits, drifted pins, branch age/expiry
reble promote             # fast-forward if base is current; forced re-run with
                          #   fresh diff if main moved. No merge. Ever.
```

## Models are plain SQL

No orchestrator required. `models/**/*.sql` — one file is one model, the file
stem is the model name, and a minimal header comment block carries the
semantics:

```sql
-- model: mart_orders      (optional; defaults to file name)
-- kind: table | view | incremental
-- key: order_id           (diff key; required for incremental)
select ... from stg_orders join raw_customers using (customer_id)
```

Lineage is parsed with SQLGlot: a table reference that matches another model
is an edge; anything else is an upstream input, pinned with an Iceberg tag at
run time. Cosmetic edits (whitespace, comments, casing) hash identically on
the canonical AST and never trigger a run.

## How it works

Reble is built on **native Iceberg branch refs** — a per-table Iceberg spec
feature supported by any catalog (Glue, Polaris, Nessie, Hive, or any
REST-compliant catalog). It is *not* a catalog and requires no new
infrastructure. A branch ref is metadata-only: zero bytes are copied.

- **Scoped branching** — scope = edited models ∪ downstream closure, capped
  by `--depth` if you want it tighter.
- **Pinned inputs** — upstream tables are pinned with Iceberg **tags**
  (`reble_pin__*`) created at run time; tags block `expire_snapshots`, so
  branch reads stay correct even while main moves.
- **Row-level diffs** — computed on your compute via DuckDB: added / removed
  / changed counts, samples, schema delta.
- **Promote semantics** — fast-forward only when every pinned base still
  equals current main; otherwise a scoped re-run and a fresh, promote-time
  diff. The PR diff is advisory; the promote diff is authoritative.

## Documentation

- [`SPEC.md`](SPEC.md) — normative v0 CLI specification: invariants, on-disk
  layout, `reble.yml` schema, command reference, JSON envelope, exit codes.
- [`DECISIONS.md`](DECISIONS.md) — recorded behavior decisions where the
  spec left room, including the standalone (no-dbt) model contract.

## Requirements

- Python 3.10+
- An Iceberg catalog (Glue, Polaris, Nessie, Hive, or any REST-compliant one)
- SQL models under `models/` (path configurable via `lineage.models_path`)

## Status

v0.1 — the full branch → run → diff → promote loop on DuckDB + pyiceberg.
Spark runner, Trino read path, and shadow-namespace mode are next.

## License

Apache-2.0.
