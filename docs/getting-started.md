# Getting started

Everything runs on your machine: a sqlite-backed Iceberg catalog, a local
file warehouse, DuckDB compute. No S3, no credentials, no Docker. The same
steps work against a real catalog (Glue, Polaris, Nessie, Hive, REST) by
pointing `reble.yml` at it.

## Install

```bash
pip install reble          # or: uv tool install reble / pipx install reble
```

Python 3.10+.

## First branch in ten minutes

The example below lives in the repo as
[`examples/orders-lakehouse`](https://github.com/satya1395/reble/tree/main/examples/orders-lakehouse)
— copy it or follow along.

```bash
mkdir orders && cd orders && mkdir models
```

Create three SQL files — one file is one model, the file name is the table
name:

```sql title="models/stg_orders.sql"
-- kind: table
-- key: order_id
select * from raw_events where amount > 0
```

```sql title="models/mart_orders.sql"
-- kind: table
-- key: order_id
select order_id, amount * 2 as amount_doubled from stg_orders
```

Write the project config and seed the upstream input table:

```bash
reble init --catalog sql --namespace analytics
python examples/orders-lakehouse/seed.py   # creates analytics.raw_events
```

First run — a fresh project has no run history, so declare the scope
explicitly (after this, Reble derives it from your edits):

```bash
reble run --models stg_orders,mart_orders
```

Now make a real change and give it a branch:

```bash
git switch -c tighten-filter
$EDITOR models/stg_orders.sql     # amount > 0  →  amount > 15

reble run        # scope inferred: stg_orders + everything downstream
reble diff mart_orders
#   analytics.mart_orders:  +2 -0 ~0  (keys: order_id)
reble status     # clean — nothing drifted
reble promote    # fast-forward main; verify: the rows are there
```

Outside git, or with `git_sync: false`, the same loop works keyed by a
change-set: `reble run --change-set agent-42 --models ...`, then pass
`--change-set agent-42` to diff/status/promote.

## Watch the guardrails

Simulate production moving under you — append a row to the input on main:

```bash
python - <<'PY'
import pyarrow as pa, yaml
from pyiceberg.catalog import load_catalog
cfg = yaml.safe_load(open("reble.yml"))
cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
cat.load_table("analytics.raw_events").append(
    pa.table({"order_id": [4], "amount": [40.0]}))
PY
```

```bash
reble status            # exit code 3: a pinned input no longer matches main
reble promote --ff-only # exit code 4: refuses
reble promote           # re-pins, re-runs the scope, emits the authoritative
                        # promote-time diff, then fast-forwards
```

The drift check is the heart of the safety story: promote is legal only when
every pinned input still equals production. Exit codes are a contract —
`3` = drift, `4` = promote blocked, `5` = empty scope, `6` = lineage error,
`7` = missing diff key. CI can (and should) branch on them.

## Where to go next

- [Concepts](concepts.md) — the branching model, pinning, and why there is
  no merge.
- [Command reference](cli.md) — every command and flag.
- [`SPEC.md`](https://github.com/satya1395/reble/blob/main/SPEC.md) — the
  normative specification, if you want the fine print.
