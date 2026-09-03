# orders-lakehouse — a runnable Reble example

A three-model warehouse over a local Iceberg catalog. Everything runs on
your machine: sqlite-backed catalog, file-based warehouse, DuckDB compute.
No S3, no credentials, no Docker.

```
raw_events (upstream input, seeded: order events as they land)
   └── stg_orders          (dedupe + types + paid filter)
         └── mart_orders   (per-order amounts: tax, totals, order date)
               └── report_daily (view: daily counts and revenue)
```

## Setup

From this directory:

```bash
# 1. Write reble.yml — a local sql catalog + ./warehouse, namespace analytics
reble init --catalog sql --namespace analytics

# 2. Seed the upstream input table on "main"
python seed.py

# 3. First run: materialize all three models on the main-side state
reble run --models stg_orders,mart_orders,report_daily
```

Why `--models` on the first run? A fresh branch with no run history has an
empty scope by design (invariant 6 — see SPEC.md). After this run, Reble
tracks model hashes and derives scope itself.

## The loop you came for

```bash
# a real change: raise the minimum-order threshold
$EDITOR models/stg_orders.sql     # change  amount > 0  to  amount > 15

reble run                          # scope: stg_orders + downstream closure
reble status                       # what ran, what's pinned, any drift
reble diff mart_orders             # +added / -removed / ~changed rows
reble promote                      # fast-forward main; no merge, ever
```

What just happened, mechanically:

- `reble run` created zero-copy Iceberg **branch refs** for the scope tables
  and **pinned** `raw_events` with a tag (`reble_pin__<branch>__raw_events`)
  — so the branch is a deterministic function of one input state.
- `reble diff` compared the branch snapshots against the branch point,
  keyed on `order_id` (from the `-- key:` header).
- `reble promote` fast-forwarded main to the branch heads — with per-table
  state that resumes if interrupted, and a forced re-run if main moved.

## Watch the guardrails

```bash
# simulate production moving under you: append a row to raw_events on main
python - <<'PY'
import pyarrow as pa, yaml
from pyiceberg.catalog import load_catalog
cfg = yaml.safe_load(open("reble.yml"))
cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
ns = cfg["warehouse"]["namespace"]
cat.load_table(f"{ns}.raw_events").append(
    pa.table({"order_id": [4], "amount": [40.0]}))
PY

reble status            # exit code 3: drifted pin
reble promote --ff-only # exit code 4: refuses
reble promote           # re-runs the scope on fresh pins, emits the
                        # authoritative promote-time diff, fast-forwards
```

## Agent mode

The same loop works without git, keyed by change-set:

```bash
reble run --change-set agent-42 --models stg_orders
reble status --change-set agent-42
reble promote --change-set agent-42
```

Or over MCP — see the README's Agents section.

## Cleanup

Delete the directory contents' generated state: `rm -rf .reble catalog.db
warehouse`, or `reble branch discard <branch>` to drop just the branch refs
and pin tags.
