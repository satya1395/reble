# Getting Started with Reble

> ⚠️ **Pre-alpha.** These commands describe the intended v0.1 experience; the code is
> not written yet. See [architecture.md](architecture.md) for the plan.

## Install

```bash
uv tool install reble    # or: pipx install reble
```

No Docker, no services. DuckDB is embedded, Iceberg tables live on your filesystem,
the catalog is SQLite, SQLMesh runs in-process.

## First branch in 5 minutes

```bash
# 1. Scaffold a project (SQLMesh models, config, local warehouse, example data)
reble init my-warehouse && cd my-warehouse

# 2. Build main
reble run

# 3. Edit a model, then branch just what you're changing
reble branch create fix-orders --tables analytics.orders,analytics.order_totals

# 4. Run on the branch — writes go to zero-copy Iceberg branch refs;
#    all other tables are read from main, pinned to their current snapshots
reble run

# 5. See what actually changed
reble diff
#   analytics.orders:        +1,204 rows, -0, ~312 changed | schema: +1 column
#   analytics.order_totals:  ~48 changed

# 6. Apply to main and clean up
reble promote
```

## Branch-per-PR CI

```yaml
# .github/workflows/reble.yml
on: pull_request
jobs:
  reble:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: rebleio/reble-action@v1
```

On each PR the action creates a scoped branch, runs only the changed models, and posts
a comment with downstream impact and row-level diff stats. Promote on merge, cleanup
on close.

## Team mode

Point the same project at shared infrastructure — no tool changes:

```yaml
# reble.yml
warehouse: s3://my-bucket/warehouse
catalog:
  type: sql
  uri: postgresql://...
```

## Learn more

- [Architecture & implementation plan](architecture.md)
- [Example project](../examples/ecommerce/)
