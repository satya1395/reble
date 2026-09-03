---
title: "Iceberg refs"
description: "The primitive Reble is built on, and exactly how it's used."
---
import Mermaid from "../../components/Mermaid.astro";

Everything Reble does reduces to one Apache Iceberg feature: **refs**. This
page explains what they are and exactly how Reble uses them.

This is optional reading. Reble is usable without it — but after it, the CLI
reads as obvious rather than magical.

## What an Iceberg table actually is

An Iceberg table is not data — it's a log of **snapshots**. Every write
(append, overwrite, delete) commits a new snapshot that points at the
complete set of data files constituting the table *as of that commit*.
Snapshots are immutable and cumulative: reading snapshot *N* gives
byte-identical results forever, even after a thousand more commits.

A **ref** is a named pointer to a snapshot. Iceberg has two kinds:

- **branch** — a movable pointer that records its own history of commits.
  `main` is the branch every other tool reads.
- **tag** — an immutable pointer at one snapshot.

Refs live in table metadata. Creating one writes a small JSON file — no
data is touched. That is the whole trick:

> A "copy" of a 5M-row table costs one metadata write: < 10 ms, zero bytes.

Two tables never share refs — branches are **per-table**, which is why
Reble's promote is per-table (more on that below).

## How Reble uses refs

### Branches: the isolated workspace

Every change-set gets a data branch on each table it touches. When you
edit `stg_orders` and run, Reble creates (or advances) a branch named
after your change-set on `stg_orders` and everything downstream. Reads of
in-scope models resolve through the branch; writes land on the branch
head. `main` is untouched until you promote.

<Mermaid>{`flowchart LR
    S1(("seed")) --> S2(("snapshot 2"))
    S2 --> S3(("snapshot 3 = main"))
    S3 --> B1(("branch write 1"))
    B1 --> B2(("branch write 2"))
    MAIN["main"] -.-> S3
    BR["branch fix_orders"] -.-> B2
    style MAIN stroke-width:3px
    style BR stroke-width:3px,stroke-dasharray: 5 5
`}</Mermaid>

One subtlety: Iceberg can't hang a branch off a table with zero
snapshots. So a model's first-ever run creates the table with one
**zero-row seed snapshot on main**, marked `reble.seed: true` in the
snapshot summary, and branches from that. Main stays empty: the seed exists
only so the branch has something to attach to.

### Tags: the pins

When a run starts, every *upstream* input table gets a **tag**
(`reble_pin__<branch>__<table>`) at main's current head. All reads of
that input resolve through the tag for the life of the branch. The
consequence: you can rerun `reble run` fifty times while production keeps
ingesting, and get the same answer fifty times.

Tags also protect the data they point at. Iceberg's `expire_snapshots`
refuses to delete data files that a tag still references. A pin physically protects
the snapshots your branch depends on. (The flip side is why `reble gc`
exists — a discarded branch whose pin tags linger would block snapshot
expiry on a production table forever.)

### Snapshot summaries: the provenance

Every commit Reble makes carries provenance in the snapshot summary:

```json
{
  "reble.model": "stg_orders",
  "reble.ast_hash": "7c1661…",
  "reble.branch": "fix-orders",
  "reble.run_id": "996d85d0eda7",
  "reble.changeset": "fix-orders"
}
```

"Which code produced this table state" is answerable from the catalog
itself — the metadata travels with the data, through promote, forever.
No git required.

### Promote: fast-forward, proven safe

Promoting moves `main` to the branch head. Whether that is *safe* is a
graph question: is main's current head an ancestor of the branch head?
Reble walks the snapshot **parent chain** to answer it — never snapshot-id
ordering, because ids are not monotonic across writers.

- **Pins still equal main** → every input you read is still current →
  fast-forward each table. A metadata write per table; instant.
- **Anything drifted** → your diff was computed against stale inputs →
  Reble refuses to promote blind and instead forces a scoped re-run
  against fresh pins, then a fresh diff. `--ff-only` makes it refuse
  outright (exit `4`).

This is why there is no merge. A three-way merge of *data* requires guessing
what rows were meant to be; a re-run against current inputs does not.

### The read path

`reble diff` and run inputs read exact snapshots, resolved through refs,
never "whatever's current":

- in-scope model → its branch ref
- upstream input → its pin tag
- otherwise → `main`

On DuckDB the read is `iceberg_scan(location, snapshot_from_id=…)` —
streams, spills, never materializes. On Spark it's
`tbl VERSION AS OF <snapshot_id>`. Same guarantee either way: the rows you
see are the rows that were committed.

## What this buys you, concretely

| Gesture | Cost |
| --- | --- |
| Branch a 5M-row table | < 10 ms, zero bytes copied |
| Rerun a scoped change | reads only the branch's inputs, replaces branch head |
| Diff branch vs main | keyed SQL over two snapshots — no data movement |
| Promote (no drift) | one metadata write per table |
| Discard | drop refs; `reble gc` clears pins so expiry reclaims files |

Compare that to the staging-warehouse alternative — copy the data, refresh
the copy, queue for the copy — and you can see why Reble treats Iceberg
refs as the primitive and everything else as workflow around it.
