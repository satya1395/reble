# Spike 08 — REST catalog proxy feasibility (plan)

**Question:** can a ~200-line local HTTP server speaking the Iceberg REST
catalog protocol make off-the-shelf clients read a *branch's* view of a
reble warehouse?

**Setup:** a staged local project (reuse `docs/assets/demo/stage.sh`) with a
branch whose `core.stg_orders` differs from main and a pinned `raw.orders`
that has *newer* rows on main than at the pin.

**The server:** stdlib `http.server`; endpoints `GET /v1/config` and
`GET /v1/{prefix}/namespaces/{ns}/tables/{tbl}`. LoadTable response =
pyiceberg's real `TableMetadata` serialized with `current-snapshot-id`
swapped to `BranchEngine.resolve_read(table)` and reble's refs stripped.

**Exit criteria (all must pass):**

1. `pyiceberg` REST client (`load_catalog(type="rest", uri=...)`) scans
   `core.stg_orders` and gets the **branch's** rows.
2. DuckDB `ATTACH (TYPE iceberg, ENDPOINT ...)` gets the same rows.
3. Both clients scanning `raw.orders` get the **pinned** rows, not main's
   newer rows.
4. A `PUT`/`POST` (commit attempt) gets 405.
5. Record: min DuckDB iceberg-extension version, whether inline metadata
   sufficed or `metadata-location` had to exist (design doc open Qs 1-2).

**Timebox:** one evening. If synthesized metadata fights back, fallback to
measure: write a real (temp) metadata JSON file per request and point
`metadata-location` at it.
