---
title: "Branches and promotion"
description: "How a data branch isolates a change, how pins keep it reproducible, and how promotion applies it."
---
import Mermaid from "../../components/Mermaid.astro";

This page covers the safety machinery: what a data branch is, why your inputs
hold still while you work, what drift means, and what happens when you accept
a change. It is the half of Reble that decides whether a change is safe to
apply.

## A data branch

When you run a change, Reble creates an Apache Iceberg branch ref on each
table in scope. Reads of those models resolve through the branch; writes land
on the branch head. `main` is untouched until you promote.

A branch ref is metadata. Creating one writes a small JSON file and copies no
data, so branching a 5M-row table costs under 10 ms and zero bytes. Branches
are per-table — Iceberg has no catalog-wide branch — which is why promotion
also works per table.

Branches are cheap and short-lived. Each is either promoted or discarded;
`branching.ttl_days` expires the ones nobody finished.

The mechanics are in [Iceberg refs](/reble/iceberg-refs/), which is optional
reading.

## Pins

You run your change at 2pm. Production keeps ingesting while you work. You
rerun at 4pm and the diff now shows rows that have nothing to do with your
change. Separating your edit from four hours of new data is guesswork.

Pins remove the guesswork. When a branch first runs, Reble records the exact
version of every upstream input your models read, as an Iceberg tag named
`reble_pin__<branch>__<table>`. Every rerun on that branch reads those
versions, not whatever landed since.

Rerun five times while production ingests all afternoon and you get the same
answer five times. The diff shows only your change, because only your change
is in it.

Pins also protect the data they point at. Iceberg's `expire_snapshots`
refuses to delete files a tag still references, so cleanup on production
cannot pull the ground out from under a live branch. The cost of that
guarantee is that abandoned pins block expiry forever, which is what
`reble gc` exists to prevent.

Pinning is on by default and controlled by `branching.pin_inputs`.

## Drift

Drift is the word for "production moved since this branch pinned it." A
pinned input no longer equals current `main`.

```bash
reble status        # exit 3 when anything drifted
```

`reble status` is read-only and CI-safe, so exit 3 is a cheap gate to put on
a pull request. Drift is not an error — it is the normal consequence of time
passing. It only means the diff you are looking at was computed against
inputs that are no longer current.

## Promote

Promotion moves `main` to the branch head, one table at a time.

<Mermaid>{`flowchart LR
    D["reble diff<br/>(advisory)"] --> P{"reble promote"}
    P -->|"pins == main"| FF["fast-forward<br/>(per-table, resumable)"]
    P -->|"drift"| RR["re-pin + scoped re-run<br/>+ fresh authoritative diff"] --> FF
    P -.->|"never"| M["three-way merge"]
    style M stroke-dasharray: 5
`}</Mermaid>

There are exactly two outcomes:

- **No drift** — every pinned input still equals `main`, so the branch was
  built from current data. Each table fast-forwards. That is one metadata
  write per table.
- **Drift** — the branch was built from data that has since moved. Reble
  re-pins, re-runs the scope against fresh inputs, computes a new diff, and
  then fast-forwards. `--ff-only` makes it refuse instead, exiting `4`.

Promotion is per-table atomic and it remembers its progress. Interrupt it,
run it again, and it finishes without redoing tables it already moved. Every
table it moves carries a record of which model and which SQL produced it, and
that record travels into production permanently.

### Advisory and authoritative diffs

The diff you review before promoting is **advisory**. The diff Reble computes
*at promote time*, after any forced re-run, is **authoritative**.

The distinction matters because it is the guarantee: you never review one set
of rows and then get a different set in production. If main moved, you get a
fresh diff to review before anything ships.

## Discard

The other outcome is throwing the branch away.

```bash
reble branch discard fix-orders --yes
```

That drops the branch refs and the pin tags. It refuses if a promote is
already in progress for that branch.

`reble gc` does the same thing on a schedule: expire branches older than
`branching.ttl_days` and drop pin tags left behind by branches that are gone.
Run it alongside your refresh. Orphan pin tags are not cosmetic debris — each
one blocks snapshot expiry on a production table for as long as it exists.

## There is no merge

Promote is a fast-forward or a re-run. There is no third option and no
three-way merge, now or later.

This is a design decision, not a missing feature. When two branches derive
from the same base, merging their *outputs* means guessing what rows were
meant to be. There are no conflict markers for data and no resolution a
reviewer can check. A re-run against current inputs is a guarantee; a merge is
a guess.

Refusing to merge is also what keeps branches cheap. You never accumulate
merge debt, so branches stay short-lived and disposable.

If you know git: promote is `git merge --ff-only`, and there is no
`git merge` at all. The rest of the mapping is in
[How Reble works](/reble/how-it-works/#if-you-know-git).
