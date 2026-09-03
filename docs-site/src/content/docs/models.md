---
title: "Models"
description: "One SQL file, one table — the only unit of work."
---
A **model** is one SQL file that produces one table. Nothing more. If you
have a folder of query files and some way of running them in order, you
already have models.

## Plain SQL, three optional lines

```sql
-- kind: table
-- key: order_id
select * from raw_events where amount > 10
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
