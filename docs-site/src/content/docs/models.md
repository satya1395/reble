---
title: "Models and lineage"
description: "What a model is, how Reble reads dependencies from your SQL, and how it decides what to rebuild."
---
This page covers the unit of work — a model — and everything Reble derives
from your SQL: the dependency graph, which models an edit affects, and which
models a run can skip. Read it once and `reble run` stops being surprising.

## A model is one SQL file

A model is one SQL file that produces one table. Nothing more. If you keep a
folder of query files and run them in some order, you already have models;
point Reble at the folder.

The file stem is the table name. `models/stg_orders.sql` builds
`<namespace>.stg_orders`. The path is `models/**/*.sql`, configurable via
`lineage.models_path`.

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

## The header

A comment header is the only metadata Reble asks for, and every line of it
is optional.

| Header | Meaning |
| --- | --- |
| `-- kind: table` | Default. A materialized table on the branch. |
| `-- kind: view` | Declares intent. In v0 a view is *materialized* like a table — computed and written at run time. Compute-on-read views are a later question. |
| `-- key: order_id` | The diff key: what identifies a row for `reble diff`. Comma-separate for compound keys. |
| `-- model: name` | Override the table name. Default is the file name. |

`kind` accepts `table` and `view` and nothing else. `kind: incremental` is
rejected when the model is parsed, rather than accepted and quietly ignored.
Every run rebuilds its scope in full — replace, never append — so a kind that
said "incremental" would not be true. See
[Running and scheduling](/reble/running/#there-is-no-incremental-kind).

No Jinja, no templating, no YAML model files.

## Lineage comes from the SQL

A reference to another model is a dependency. `from stg_orders` in
`mart_orders.sql` is the edge; there is no `ref()` call and no configuration
file listing edges.

Reble parses the SQL with [SQLGlot](https://github.com/tobymao/sqlglot) under
`lineage.dialect`. Each table reference is resolved one of two ways:

- **It matches another model** → a graph edge. That model builds first.
- **It matches nothing** → an *upstream input*: a table someone else fills,
  such as the output of your ingestion job. Reble never builds it. At run
  time it is pinned, so your inputs hold still while you work — see
  [Branches and promotion](/reble/branches/#pins).

Because the graph is derived, run order is not something you declare and not
something you can get wrong.

## How edits are detected

Reble hashes the *structure* of your SQL, not its text. The same parse that
builds the graph produces an AST hash per model.

Reformat a file, rewrite a comment, change keyword casing — the hash is
identical and nothing runs. Add a filter, a join, or a column and the hash
changes, which puts that model in scope.

## Scope: what a run rebuilds

```
scope = edited models ∪ downstream closure
```

A run never rebuilds the whole warehouse. It rebuilds the models you edited,
plus everything built on top of them, because a model whose inputs changed is
itself changed.

`--depth N` caps how far the cascade travels. Models cut off by the cap are
reported as stale, so a tighter radius is never a silent one.

There are three ways a scope gets decided — from your edits, from data
movement (`--refresh`), or explicitly (`--models`). That is the subject of
[Running and scheduling](/reble/running/).

## When a model skips

A model in scope skips execution only when *both* hold:

1. its SQL hash is unchanged, **and**
2. none of its in-scope parents executed in this run.

The second condition is the one that matters. Unchanged SQL over changed
inputs is a changed table, so a rerun that skipped on SQL alone would be fast
and wrong.

## The first build

A fresh project has no run history, so there are no hashes to compare against
and the inferred scope is empty. Declare the first build explicitly:

```bash
reble run --models stg_orders,mart_orders,report_daily   # a comma-separated list
reble run --force                                        # or: every model in scope
```

There is no `--models all`: the value is a list of model names, so `all`
resolves to a model called `all` and the run exits `1`. Use `--force` when
you mean "everything".

Every later run infers its own scope.

## Coming from dbt

Your SQL ports almost unchanged. Delete the `ref()` calls and point Reble at
the models directory: `{{ ref('stg_orders') }}` becomes `stg_orders`. Same
edge, no function call, no compile step.

dbt users are welcome and nothing assumes dbt. For the fuller argument, see
[Comparisons](/reble/comparisons/#reble-vs-dbt).
