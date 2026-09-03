---
title: "Pins"
description: "Why a rerun gives the same answer every time."
---
You run your change at 2pm. Production keeps ingesting while you work. You
rerun at 4pm — and now your diff shows differences that have nothing to do
with your change. Which rows are yours, and which are just... later data?
Nobody knows.

This is the problem pins solve.

## What Reble does

When your branch first runs, Reble bookmarks the exact version of every
input table your models read. Every rerun on that branch reads those
bookmarks — not whatever landed since.

So: rerun five times while production ingests all afternoon, and get the
same answer five times. Your diff shows only *your* change, because only
your change is in it.

The bookmark also can't be swept away under you: cleanup jobs on
production are not allowed to delete data your branch still depends on.
While your branch lives, its inputs live.

## When the world moves anyway

Suppose you finish, and production has moved since your branch started.
Reble won't pretend nothing happened:

1. `reble status` tells you an input moved (exit code `3`).
2. `reble promote` redoes your change against the fresh data, then shows
   you the new diff before anything goes live.

You never review an old diff and then get different rows in production.

## Under the hood

The bookmarks are Iceberg tags — immutable pointers your catalog already
understands. The full mechanics, including why cleanup can't touch pinned
data, are in [Iceberg refs](../iceberg-refs.md). You don't need that page
to use Reble; it's there when you want to know exactly what's happening
to your tables.
