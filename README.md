# Reble

**The workspace where humans and AI agents change the lakehouse safely.**
Every change runs on an isolated Iceberg branch and is accepted only after
its consequences are visible.

- **For humans:** the branch button for the lakehouse.
- **For agents:** a transactional data-change API — scope, pin, run, diff,
  promote or discard.
- **For the cloud (coming):** richer frontends on the same verbs
  live data branch, and an accept button that shows you the rows.

The headline capability is **accept-with-consequences**: before you accept a
change, Reble shows you the exact rows it adds, removes, and modifies. A code
IDE can't do that; a lakehouse can, structurally — the branch already
materialized the effect.

## Quick start

```
pip install reble
reble init                # writes reble.yml; probes your catalog
git switch -c fix-orders  # or: --change-set agent-42 — git is one adapter
# ...edit two models...
reble run                 # → data branch: edited models + downstream closure
                          #   written; upstream inputs pinned via Iceberg tags
reble diff                # schema + row-level diff vs. branch base
reble status              # un-run edits, drifted pins, branch age/expiry
reble promote             # fast-forward if base is current; forced re-run with
                          #   fresh diff if main moved. No merge. Ever.
```

Bots and agents are first-class users: every command speaks a stable
[`--json` envelope](SPEC.md) with documented exit codes, and `run`/`diff`
stream versioned [`--events`](SPEC.md#event-streams) (NDJSON) for progress.
Change-sets don't need git: `reble run --change-set <id>` (or
`REBLE_CHANGE_SET`) keys the work; `--branch` resumes an existing data
branch under a new change-set.

## Models are plain SQL

No orchestrator required. `models/**/*.sql` — one file is one model, the file
stem is the model name, and a minimal header comment block carries the
semantics:

```sql
-- model: mart_orders      (optional; defaults to file name)
-- kind: table | view | incremental
-- key: order_id           (diff key; required for incremental)
select ... from stg_orders join raw_customers using (customer_id)
```

Lineage is parsed with SQLGlot: a table reference that matches another model
is an edge; anything else is an upstream input, pinned with an Iceberg tag at
run time. Cosmetic edits (whitespace, comments, casing) hash identically on
the canonical AST and never trigger a run. Every branch snapshot carries
provenance (`reble.model`, `reble.ast_hash`, `reble.run_id`) in its summary —
"which code produced this table state" is answered from the catalog itself.

## How it works

Reble is built on **native Iceberg branch refs** — a per-table Iceberg spec
feature supported by any catalog (Glue, Polaris, Nessie, Hive, or any
REST-compliant catalog). It is *not* a catalog and requires no new
infrastructure. A branch ref is metadata-only: zero bytes are copied.

- **Scoped branching** — scope = edited models ∪ downstream closure, capped
  by `--depth`.
- **Pinned inputs** — upstream tables pinned with Iceberg **tags**
  (`reble_pin__*`) at run time; tags block `expire_snapshots`, so branch
  reads stay correct even while main moves.
- **Row-level diffs** — computed on your compute via DuckDB.
- **Promote semantics** — fast-forward only when every pinned base still
  equals current main; otherwise a scoped re-run and a fresh, promote-time
  diff. The PR diff is advisory; the promote diff is authoritative.

## Documentation

- [`design notes`](design notes) — vision design notes: positioning, architecture, open-core
  boundary, roadmap.
- [`SPEC.md`](SPEC.md) — normative CLI specification (v0.2): invariants,
  on-disk layout, `reble.yml` schema, command reference, JSON envelope,
  event streams, provenance, exit codes.
- [`DECISIONS.md`](DECISIONS.md) — recorded behavior decisions.

## Requirements

- Python 3.10+
- An Iceberg catalog (Glue, Polaris, Nessie, Hive, or any REST-compliant one)
- SQL models under `models/` (path configurable via `lineage.models_path`)

## Status

v0.1 — the full branch → run → diff → promote loop on DuckDB + pyiceberg,
with change-set keying, event streams, and catalog-side provenance. Spark
runner, Trino read path, and shadow-namespace mode are next; the editor
cloud and MCP tool surface follow (see the design notes roadmap).

## License

Apache-2.0.
