---
title: "Models"
description: "One SQL file, one table — the only unit of work."
---
A **model** is one SQL file that produces one table. Nothing more. If you
have a folder of query files and some way of running them in order, you
already have models.

## Plain SQL, three optional lines

Real models from the runnable
[orders-lakehouse example](https://github.com/satya1395/reble/tree/main/examples/orders-lakehouse) —
CTEs, window functions, casts, whatever your SQL needs:

```sql title="models/stg_orders.sql"
-- kind: table
-- key: order_id
-- Upstream input (not a model): raw_events, where ingestion lands events
-- as they arrive — duplicates and all.
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

The comment header is the only metadata Reble asks for, and even it is
optional:

| Header | Meaning |
| --- | --- |
| `-- kind: table` | Default. A materialized table on the branch. |
| `-- key: order_id` | The diff key — what identifies a row for `reble diff`. Comma-separate for compound keys. |
| `-- model: name` | Override the table name (default: the file name). |

No Jinja, no templating, no YAML model files. References to other models
are just SQL table references — `from stg_orders` — and those references
*are* the dependency graph. Reble parses them with SQLGlot; ordering and
blast radius come from lineage, never from configuration you maintain.

## Coming from dbt

Your SQL ports almost unchanged: delete the `ref()` calls and point Reble
at the models directory. `stg_orders` in dbt becomes `from stg_orders` in
plain SQL — same edge, no function call. dbt users are welcome, but
nothing assumes dbt.
