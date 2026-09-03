---
title: "Promote or discard"
description: "The accept button — and the only other option."
---
import Mermaid from "../../components/Mermaid.astro";

<Mermaid>{`flowchart LR
    D["reble diff<br/>(advisory)"] --> P{"reble promote"}
    P -->|"pins == main"| FF["fast-forward<br/>(per-table, resumable)"]
    P -->|"drift"| RR["re-pin + scoped re-run<br/>+ fresh authoritative diff"] --> FF
    P -.->|"never"| M["three-way merge"]
    style M stroke-dasharray: 5
`}</Mermaid>

Promotion is per-table fast-forwards on vanilla catalogs, with recorded,
re-entrant state — an interrupted promote resumes without re-executing
already-promoted tables. Promotion is also *provenance-carrying*: every
branch snapshot records the model name, AST hash, and run id that produced
it, and that metadata travels with the snapshot to main.

## Advisory vs authoritative diffs

The diff you review before promote is **advisory**; the diff Reble computes
*at promote time* (after any forced re-run) is **authoritative**. If main
moved, you re-run and re-review — you never review a stale diff and then
get different rows.

## Branches are ephemeral

Branches are cheap to create and meant to die young: promote them, or
`reble branch discard` them. TTL expiry (`reble gc`) drops stale branches
and — importantly — orphan pin tags, which otherwise block snapshot
expiration on production tables.

There is no third option, and that is the design. The full argument is in
[where the git analogy breaks](git.md#where-the-analogy-breaks-merge).
