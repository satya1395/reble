---
title: "Configuration"
description: "The complete reble.yml reference."
---
`reble.yml` is the project config — versioned in git, one per project. It
is written by [`reble init`](cli.md#reble-init) and validated before any
verb runs (exit `2` on a bad file). Secrets never live here: they come
from `${ENV_VAR}` interpolation, and `init` refuses to save values that
look like secrets.

A complete, annotated file:

```yaml
version: 1

warehouse:
  catalog:
    type: glue                 # glue|polaris|nessie|hive|rest|reble|sql|in-memory|dynamodb|bigquery
    region: us-east-1          # translated to glue.region for you
    warehouse: s3://my-bucket/reble
    # uri: https://...         # required for rest/polaris/nessie
    # name: mycatalog          # catalog name in table identity (default: reble)
  namespace: analytics         # Iceberg namespace for model tables
  default_base: main           # the base ref branches fork from

lineage:
  models_path: models          # models/**/*.sql — one file, one model
  dialect: duckdb              # SQLGlot dialect for parsing + AST hashing

branching:
  git_sync: true               # derive change-set from the git branch
  pin_inputs: true             # tag-pin upstream inputs at run time
  tag_prefix: reble_pin__      # pin tag namespace
  ttl_days: 14                 # branch age before gc expires it
  name_sanitization: {"/": "__", " ": "_"}

state:
  store: local                 # local (SQLite) | postgres
  # uri: ${REBLE_STATE_URI}    # required when store: postgres

diff:
  keys: {}                     # per-table keys: {"analytics.mart_orders": [order_id]}
  on_missing_key: hash         # hash | error (error → exit 7)
  max_rows_dumped: 1000        # sample rows saved per category

engines:
  duckdb:
    read_mode: auto            # auto (iceberg_scan) | arrow
    memory_limit: 4GB
    temp_directory: .reble/spill
    settings: {}               # raw SET passthrough (overrides S3 auto-config)
  spark:
    master: local[*]
    app_name: reble
    settings: {}               # raw Spark conf; `packages` overrides default jars

compute_policy:
  prefer: duckdb               # duckdb | spark

profiles:
  ci:
    state: {store: postgres, uri: ${REBLE_STATE_URI}}
  prod:
    compute_policy: {prefer: spark}
```

## warehouse

| Key | Default | Meaning |
| --- | --- | --- |
| `catalog.type` | — | Catalog backend. `sql` and `in-memory` are local (zero infra); `glue`, `hive`, `rest`/`polaris`/`nessie` point at infrastructure you already run. |
| `catalog.region` | — | AWS region; Reble translates it to the `glue.region` pyiceberg expects. |
| `catalog.uri` | — | Required for REST-spec catalogs. |
| `catalog.warehouse` | — | Table storage location (S3 bucket path, or a local dir for `sql`). |
| `catalog.name` | `reble` | Catalog name — part of table identity for SQL-backed catalogs, must be stable. |
| `namespace` | — | Iceberg namespace where model tables live. |
| `default_base` | `main` | The ref everything branches from and promotes to. |

## lineage

| Key | Default | Meaning |
| --- | --- | --- |
| `models_path` | `models` | Scanned recursively for `*.sql`; one file = one model. |
| `dialect` | `duckdb` | SQLGlot dialect used for lineage parsing and AST hashing. Affects which SQL parses, not what executes (models are transpiled per engine). |

## branching

| Key | Default | Meaning |
| --- | --- | --- |
| `git_sync` | `true` | Derive the change-set id from the current git branch. `false` makes Reble fully git-ignorant — agents pass `--change-set`. |
| `pin_inputs` | `true` | Tag-pin upstream inputs at run time so reruns are reproducible. [Why](iceberg-refs.md#pins). |
| `tag_prefix` | `reble_pin__` | Namespace for pin tags, so they are recognizable in the catalog. |
| `ttl_days` | `14` | Branch age at which `reble gc` expires it. |
| `name_sanitization` | `{"/": "__", " ": "_"}` | Characters replaced when a git branch name becomes a data branch ref. |

## state

| Key | Default | Meaning |
| --- | --- | --- |
| `store` | `local` | `local` = SQLite at `.reble/state.db` (WAL, zero config). `postgres` = shared state for CI runners / Airflow workers / teammates — requires the `postgres` extra and `uri`. |
| `uri` | — | SQLAlchemy URI for the Postgres backend, e.g. `postgresql://user:pass@host/reble`. |

State is validated at startup: a `Reble()` construction with an unreachable
backend exits `2` before touching anything. Legacy `state.json` from older
versions auto-migrates on first use.

## diff

| Key | Default | Meaning |
| --- | --- | --- |
| `keys` | `{}` | Explicit primary keys per table. Usually unnecessary — keys come from the model's `-- key:` header. |
| `on_missing_key` | `hash` | `hash` = fall back to full-row hash diff (no keys needed). `error` = exit `7` instead. |
| `max_rows_dumped` | `1000` | Sample rows saved per category to `.reble/diffs/…`. |

## engines

**duckdb** — see [Engines](engines.md). `read_mode: auto` streams reads
through `iceberg_scan` (out-of-core, spills under `memory_limit` to
`temp_directory`); `arrow` forces materialized reads. `settings` is a raw
`SET` passthrough and takes full responsibility for S3 config (otherwise
credentials are resolved via boto3's default chain).

**spark** — `master` (default `local[*]`), `app_name`, and `settings`
(raw Spark conf passthrough; a `packages` key overrides the default
Iceberg runtime jars).

## compute_policy and profiles

`compute_policy.prefer` picks the engine (`duckdb` default). Per-run
override: `reble run --engine spark`.

`profiles` are named overlays applied with `--profile NAME` or
`REBLE_PROFILE` — dict sections merge over the base. The common shape: a
`ci` profile with shared Postgres state, a `prod` profile preferring
Spark.

## Precedence and interpolation

CLI flag → `REBLE_*` env var → profile → `reble.yml` → built-in default.
`${VAR}` interpolation happens first; a referenced variable that isn't set
is a config error (exit `2`). Any key whose name contains
`secret`/`password`/`token`/… must be a `${VAR}` reference — literal
values are refused at save time.
