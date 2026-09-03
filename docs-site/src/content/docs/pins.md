---
title: "Pins"
description: "Why a branch is a function of one input state."
---
When you run on a branch, every **upstream input** — a table that isn't a
model you're branching — is pinned with an **Iceberg tag** at that moment.
Branch reads resolve through the tags for the life of the branch.

## What that buys

**Determinism.** A branch becomes a *function of one input state* rather
than a snapshot of whatever prod happened to be mid-run. Rerun fifty
times while production keeps ingesting; get the same answer fifty times.

**Physical safety.** Tags block `expire_snapshots`, so the pinned data
physically cannot disappear while the branch lives. (The flip side: a
discarded branch's orphan tags would block expiry on a production table
forever — which is why [`reble gc`](../cli.md#reble-gc) is a correctness
command, not hygiene.)

## Pins are the drift detector

`reble status` compares each pin to the input's current main snapshot.
Any mismatch — exit code `3` — means the world moved under your branch,
and [promote](promote.md) will refuse to fast-forward blind and force a
scoped re-run against fresh pins instead.

The full mechanics of tags and refs are in
[Iceberg refs](../iceberg-refs.md).
