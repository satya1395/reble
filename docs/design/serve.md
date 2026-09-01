# Design: `reble serve` — your branch in your own tools

**Status: M1 (spike 08) and M2 (the `reble serve` command) are BUILT on this
branch, with 7 tests. Spike 08 PASSED: stock pyiceberg and stock DuckDB 1.5.5
both read a branch's world through the proxy — see
[spikes/08-serve/RESULTS.md](../../spikes/08-serve/RESULTS.md), including the
snapshot-log lesson. Merge target: first post-launch release.**

## Goal

A local, read-only **Iceberg REST catalog proxy** that answers every request
with **branch-resolved snapshots**. Any Iceberg-speaking client — DBeaver,
DataGrip, the DuckDB iceberg extension, Spark, Trino, a notebook — connects
to `localhost` and sees the warehouse exactly as the current reble branch
sees it, without knowing reble exists.

```
$ reble serve
⎇ fix-cancelled-revenue
✓ serving this branch's view at http://127.0.0.1:8181
  connect any Iceberg REST client · read-only · Ctrl-C to stop
```

```sql
-- DuckDB, in DBeaver or anywhere:
ATTACH 'warehouse' AS wh (TYPE iceberg, ENDPOINT 'http://127.0.0.1:8181');
SELECT * FROM wh.core.fct_revenue_daily;   -- the BRANCH's rows
```

This is the network skin of the branch resolver (`reble query` is the
in-process skin; both route reads identically). It is also the `pscale
connect` analog with the service removed — and the OSS on-ramp to the
hosted-resolver service described in the internal plan.

## The one trick

The Iceberg REST `LoadTable` response carries the table metadata. The proxy
serves **synthesized metadata** in which:

- `current-snapshot-id` is set to `resolve_read(table)`'s answer
  (branch ref for scoped tables; pin / epoch snapshot for everything else;
  `EPOCH_EMPTY` → metadata with no current snapshot, so clients see an
  empty table)
- the branch's private refs are stripped (clients see a normal table, not
  reble's bookkeeping)
- everything else (schemas, snapshots list, file locations) passes through
  from the real metadata

Clients then plan scans against that snapshot's manifests and read Parquet
directly from the warehouse (local disk or bucket) with their own storage
credentials. **No data flows through the proxy — metadata only**, which is
the same property the future hosted service inherits.

## Protocol subset (read-only, REST spec v1)

| Endpoint | Purpose |
|---|---|
| `GET /v1/config` | capabilities handshake |
| `GET /v1/{prefix}/namespaces` | list schemas |
| `GET /v1/{prefix}/namespaces/{ns}` | namespace exists |
| `GET /v1/{prefix}/namespaces/{ns}/tables` | list tables |
| `GET /v1/{prefix}/namespaces/{ns}/tables/{tbl}` | **LoadTable — the trick above** |
| `HEAD …/tables/{tbl}` | exists check |

Everything else (commits, DDL, register) returns 405: the proxy is
read-only by design — external writes would bypass the branch guard.

## Decisions

- **Snapshot resolution is computed per-request** (no caching in M1): a
  `reble run` mid-session is visible on the next query. Cheap locally;
  revisit for S3 metadata latency.
- **Dependency footprint:** implement on stdlib `http.server` if the JSON
  models from pyiceberg suffice; take FastAPI/uvicorn only as a
  `reble[serve]` optional extra if stdlib gets ugly. The CLI's core install
  stays lean either way.
- **Auth:** none in M1 (binds 127.0.0.1 only). `--token` bearer option in
  M2. Never bind non-loopback without a token.
- **Branch selection:** serves the *current* branch; `--branch <name>`
  override. Re-resolves per request, so `reble branch switch` in another
  terminal switches what clients see (document this loudly).
- **F15 composition (later):** when local-overlay team branches land, the
  resolver already answers "local catalog or remote pinned?" per table; the
  proxy inherits that for free. This is why serve waits for nothing.

## Why not a DuckDB extension?

Asked during design (2026-09-01), answered here for posterity:

1. **One resolver, or two.** The resolver (scope/pins/epoch, branch state)
   is Python on pyiceberg. A C++ extension means a second implementation of
   the most subtle semantics, kept in lockstep forever. The proxy reuses the
   one resolver verbatim — the point of the two-skins architecture.
2. **Reach.** An extension serves DuckDB; the proxy serves every engine,
   because the Iceberg REST catalog protocol is effectively the plugin API
   the whole industry already speaks — DuckDB's own iceberg extension
   attaches to it.
3. **Maintenance.** DuckDB's C++ API churns per release; the REST spec is
   stable and versioned.
4. **SaaS continuity.** The proxy is the same code path as the future
   hosted resolver. An extension can't be hosted.

The embedded experience already exists as `reble query` (same process, no
HTTP). Possible M4, demand-driven: a thin sugar extension giving stock
DuckDB `ATTACH 'reble:…'` that talks to the local resolver — a wrapper,
never a second resolver.

## Milestones

- **M1 — spike 08 (`spikes/08-serve/`):** hand-rolled server, `config` +
  `LoadTable` for one table with an overridden snapshot id. Exit criteria:
  pyiceberg's REST client AND DuckDB `ATTACH (TYPE iceberg, ENDPOINT …)`
  both read the branch's rows, and the pinned table shows the pinned rows
  while main has newer data.
- **M2 — `reble serve` command:** full table list, per-request resolution,
  read-only 405s, `--port`/`--branch`/`--token`, CLI output per the design
  system, tests (spin server in a thread, assert through pyiceberg REST
  client).
- **M3 — docs + demo:** README roadmap item flips to shipped; GIF snippet
  of DBeaver showing branch rows; architecture.md adapter section updated.

## Using it from DBeaver / DataGrip (the M2 user guide, drafted now)

Both tools speak to serve through their existing DuckDB driver — no plugin:

**DBeaver**
1. New connection → **DuckDB** → in-memory (`:memory:`).
2. Connection settings → *Initialization* → bootstrap SQL:
   ```sql
   INSTALL iceberg; LOAD iceberg;
   ATTACH 'warehouse' AS wh (TYPE iceberg, ENDPOINT 'http://127.0.0.1:8181');
   ```
3. Connect. The `wh` database in the tree *is your branch's warehouse* —
   browse schemas, run SQL, export results. Switch reble branches in the
   terminal; reconnect (or re-run ATTACH) to see the other branch.

**DataGrip**: same shape — DuckDB data source, the ATTACH block as a
startup script.

**Anything that can't run DuckDB** (some BI tools): point it at any engine
that speaks Iceberg REST (Trino, Spark) configured with
`http://127.0.0.1:8181` as its catalog. Same proxy, same branch view.

Spike 08 must validate the exact ATTACH incantation and minimum driver
versions before this guide ships.

## No UI in OSS (decided 2026-09-01)

There is deliberately **no web UI in the open-source product** — a common
and honest open-core line (the CLI is complete without one). The OSS visual
surface is *your own tools through serve*: DBeaver, DataGrip, notebooks, BI
readers. The only reble UI that will ever exist is the hosted dashboard on
the SaaS side (collaboration, review, RBAC, audit, hosted branch
endpoints) — see the internal plan. This also keeps `serve` small: one
protocol, no static assets, no frontend toolchain in the repo.

## Non-goals

Writes through the proxy (ever, for individuals — that's the merge gate's
job); serving multiple branches concurrently on one port (the hosted
service's job); TLS (loopback only in OSS).

## Open questions

1. DuckDB iceberg extension's exact REST attach requirements (min version,
   auth header quirks) — answered by spike 08.
2. Does synthesized metadata need `metadata-location` to point at a real
   file for some clients? (Spec says inline metadata is authoritative;
   verify against DuckDB + Spark.)
3. Vended-credentials passthrough for S3 warehouses, or document
   "client brings its own AWS creds" (M1 answer: the latter).
