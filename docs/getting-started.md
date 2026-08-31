# Getting Started with Reble

> ⚠️ **Pre-alpha.** These commands work today from a source install; there is no
> PyPI release yet — see the install note below.

## Install

```bash
# no PyPI release yet — install from source:
git clone https://github.com/satya1395/reble.git && cd reble
python3 -m venv .venv && .venv/bin/pip install -e .
# put .venv/bin/reble on your PATH (symlink or alias)
```

No Docker, no services. DuckDB is embedded, Iceberg tables live on your filesystem,
the catalog is SQLite, SQLMesh runs in-process.

## First branch in 5 minutes

```bash
# 1. Scaffold a project (SQLMesh models, config, local warehouse, example data)
reble init my-warehouse && cd my-warehouse

# 2. Build main
reble run

# 3. Edit a model, then branch — scope and pins are inferred from your changes
#    (your edited models + their downstream cascade; pins = their upstream inputs).
#    Both git orders work: branch-first on a clean tree gives you an empty-scope
#    branch with a frozen epoch, and the scope grows at first `reble run`.
reble branch create fix-orders
#   scope (inferred from your changes): analytics.orders, analytics.order_totals
#   pins  (2): raw.events, raw.customers

# 4. Run on the branch — writes go to zero-copy Iceberg branch refs;
#    pinned tables read main as of the branch epoch, even while prod ingests
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
