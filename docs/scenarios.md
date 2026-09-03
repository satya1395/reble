# Days you've had

The situations Reble was built for — told the way they actually happen,
before any vocabulary. If one of these is your Tuesday, Reble is for you;
[Getting started](getting-started.md) shows the loop, and
[Concepts](concepts.md) supplies the words for what you just read.

## "The backfill"

Prod re-ingested last Tuesday. Legally you can't just re-run it — a fresh
full run would clobber today's appended data, and a partial patch is how
marts end up quietly inconsistent. So you snapshot the affected tables to a
staging project, wire credentials, re-run, compare by hand, and hope the
snapshot was from the right moment.

With Reble: `reble branch create backfill-tuesday`, run the corrected
models — the branch's inputs are **tag-pinned to the state you branched
from**, so the re-run reproduces exactly — `reble diff` shows every row the
backfill adds or repairs, `reble promote` fast-forwards when you're
satisfied. One command to clean up, nothing copied.

## "Just add one column" — to a mart 40 models depend on

You know the shape: the SQL change is five minutes; the *verification* is
the week. Which marts shift? By how much? You find out by re-running the
world in staging, or you find out in production.

With Reble: edit the model, `reble run`. Scope inference pulls in the
downstream closure automatically — you don't have to remember who depends
on whom — and `reble diff` gives you per-table row deltas, keyed, with
samples, before anything touches main.

## The silent filter

Someone tightened a `where` clause in a staging model. Tests passed. Three
weeks later someone asks why a segment shrank. Nobody can connect the
change to the missing rows — code review reviewed *SQL*, not *rows*.

With Reble, that review looks different: the PR conversation includes
`reble diff` output — `analytics.customers: -12,304 rows` — and the promote
that landed it carries the model + AST hash in the snapshot summary, so
"which code produced this table state" is answerable from the catalog,
forever.

## An agent wants to change the warehouse

An AI assistant offers to "fix" a revenue model. You'd like the help; you
do not want unfettered write access to prod. The agent drives the same
verbs over MCP — but they're *our* verbs: it gets a scoped branch it can't
escape, pinned inputs, and a promote that a human (or a gate) accepts only
after seeing the rows. If main moved underneath it, the promote forces a
re-run instead of a clever merge.

## Friday, 4:45pm

A stakeholder needs a new cut of the orders mart for Monday. On a shared
staging warehouse, that's a negotiation. With branches that cost nothing
and die clean, it's just: branch, build, diff, promote — or `discard` and
nobody ever knew.
