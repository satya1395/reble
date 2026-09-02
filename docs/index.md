---
hide:
  - navigation
---

# Reble

**Git-style branches for your Iceberg warehouse.**

Change a model, run it on an isolated zero-copy branch, review the exact rows
that changed, then fast-forward production. No warehouse clones, no copies,
no merges.

!!! note "Status: early, but real"

    The full loop works today — `init → run → diff → status → promote`, in
    git or standalone, with 75 passing tests on Linux and macOS. `pip install reble`
    and go. Feedback is by far the most valuable contribution right now —
    [open a discussion](https://github.com/satya1395/reble/discussions).

## The problem

To test a change to *one* model, data teams either re-run the pipeline
against a copy of the warehouse — slow, expensive, and stale the moment prod
ingests — or they cross their fingers on production. Neither gives you an
answer to the only question that matters before shipping: **what rows would
this change?**

## The loop

```bash
pip install reble
reble init --catalog sql --namespace analytics   # local catalog, no infra

git switch -c fix-orders        # branch in git…
# ...edit models/stg_orders.sql...
reble run                       # …and the warehouse follows: a zero-copy data
                                # branch of exactly the tables you touched
reble diff                      # rows, not lines: +1,204 -0 ~312 changed
reble status                    # un-run edits, drifted pins, branch age
reble promote                   # fast-forward main — or a scoped re-run with a
                                # fresh diff if main moved. No merge. Ever.
```

That's the whole product. [Getting started](getting-started.md) walks it end
to end on a runnable example.

## What makes it different

A branch is **metadata only**. The tables you're changing get zero-copy
Apache Iceberg branch refs; the upstream inputs are pinned with Iceberg tags
at the moment you run, so your inputs hold still while you iterate — even
while production keeps ingesting. Branching costs nothing until you write.

There is deliberately **no merge**. Promote is a fast-forward when your
pinned inputs still match production, and a scoped re-run when they don't.
Data merges with conflict resolution silently corrupt warehouses; Reble
refuses to build one. The full argument is in
[Concepts](concepts.md#where-the-analogy-breaks-merge).

And it runs against **the catalog you already have** — Glue, Polaris,
Nessie, Hive, or any REST-compliant Iceberg catalog. Reble is not a catalog,
not an orchestrator, and not a new thing to operate. How that stacks up
against lakeFS, Nessie, and warehouse clones:
[Comparisons](comparisons.md).
