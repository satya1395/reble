---
title: "Quickstart"
description: "From zero to a promoted branch in one sitting."
---
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

Create three SQL files — a *model* is just Reble's word for one SQL file
that produces one table, and the file name is the table name:

```sql title="models/stg_orders.sql"
-- kind: table
-- key: order_id
-- Upstream input (not a model): raw_events, where ingestion lands events
-- as they arrive.
with latest as (
    select
        *,
        row_number() over (partition by order_id order by event_ts desc) as _rn
    from raw_events
),
typed as (
    select
        cast(order_id as bigint)               as order_id,
        cast(user_id as bigint)                as user_id,
        lower(status)                          as status,
        cast(amount as decimal(12, 2))         as amount,
        cast(event_ts as timestamp)            as event_ts
    from latest
    where _rn = 1
)
select *
from typed
where status = 'paid'
  and amount > 0
```

```sql title="models/mart_orders.sql"
-- kind: table
-- key: order_id
select
    order_id,
    user_id,
    amount,
    round(amount * 0.0825, 2)            as tax_amount,
    round(amount + amount * 0.0825, 2)   as total_with_tax,
    date_trunc('day', event_ts)          as order_date
from stg_orders
```

Write the project config and seed the upstream input table:

```bash
reble init --catalog sql --namespace analytics
```

```python title="seed.py"
import datetime as dt

import pyarrow as pa, yaml
from pyiceberg.catalog import load_catalog

cfg = yaml.safe_load(open("reble.yml"))
cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
cat.create_namespace_if_not_exists("analytics")
cat.create_table(
    "analytics.raw_events",
    schema=pa.schema([
        ("order_id", pa.int64()),
        ("user_id", pa.int64()),
        ("status", pa.string()),
        ("amount", pa.float64()),
        ("event_ts", pa.timestamp("us")),
    ]),
).append(pa.table({
    "order_id": [1, 2, 3],
    "user_id":  [101, 102, 103],
    "status":   ["paid", "paid", "paid"],
    "amount":   [10.0, 20.0, 30.0],
    "event_ts": [dt.datetime(2026, 8, 30, 14, 22, 5),
                 dt.datetime(2026, 8, 31, 9, 14, 33),
                 dt.datetime(2026, 9, 1, 18, 3, 47)],
}))
```

```bash
python seed.py
```

First run. A fresh project has no run history, so there are no SQL hashes to
compare against and the inferred scope is empty — declare it explicitly this
once:

```bash
reble run --models stg_orders,mart_orders,report_daily
```

```
scope (edited)       3   mart_orders, report_daily, stg_orders
scope (downstream)   0   -
pinned inputs        1   reble_pin__local__raw_events
engine                 duckdb (local)
stg_orders: ran (3 rows, 113ms)
mart_orders: ran (3 rows, 50ms)
report_daily: ran (3 rows, 48ms)
```

There is no `--models all`; use `reble run --force` when you mean everything.
Every later run infers its own scope.

Now make a real change and give it a branch:

```bash
git switch -c tighten-filter
$EDITOR models/stg_orders.sql     # amount > 0  →  amount > 15

reble run        # scope inferred: stg_orders + everything downstream
```

```
scope (edited)       1   stg_orders
scope (downstream)   2   mart_orders, report_daily
pinned inputs        1   reble_pin__local__raw_events
stg_orders: ran (2 rows, 145ms)
mart_orders: ran (2 rows, 50ms)
report_daily: ran (2 rows, 48ms)
```

You edited one model; Reble worked out the other two.

```bash
reble diff mart_orders
```

```
diffing analytics.mart_orders …  +2 -0 ~0 (0.1s)
detail: .reble/diffs/local (per-table JSON; --rows N / --full for more)
```

The terminal prints the summary; the changed rows themselves are written to
`.reble/diffs/<change-set>/<table>.json`. Use `--rows N` to save more per
category, or `--full` for all of them.

```bash
reble status     # clean — nothing drifted
reble promote    # fast-forward main
```

No git repo? Same verbs, keyed by an explicit change-set:
`reble run --change-set agent-42 --models ...`, then pass
`--change-set agent-42` to diff/status/promote. See
[Git, optional](/reble/running/#how-work-is-keyed).

## Watch the guardrails

Simulate production moving under you — append a row to the input on main:

```bash
python - <<'PY'
import pyarrow as pa, yaml
from pyiceberg.catalog import load_catalog
cfg = yaml.safe_load(open("reble.yml"))
cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
cat.load_table("analytics.raw_events").append(
    pa.table({
        "order_id": [4], "user_id": [104], "status": ["paid"],
        "amount": [40.0],
        "event_ts": [__import__("datetime").datetime(2026, 9, 2, 11, 0, 0)],
    }))
PY
```

```bash
reble status            # exit code 3: a pinned input no longer matches main
reble promote --ff-only # exit code 4: refuses
reble promote           # re-pins, re-runs the scope, emits the authoritative
                        # promote-time diff, then fast-forwards
```

Promote is legal only when every pinned input still equals production. Exit codes are a contract —
`3` = drift, `4` = promote blocked, `5` = empty scope, `6` = lineage error,
`7` = missing diff key. CI can (and should) branch on them.

## Where to go next

- [How Reble works](/reble/how-it-works/) — the whole loop in one page.
- [Quickstart on AWS](/reble/aws/) — the same walkthrough against Glue + S3.
- [CLI reference](/reble/cli/) — every command and flag.
