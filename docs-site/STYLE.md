# Docs authoring guide

Applies to everything under `src/content/docs/`. Written after the 2026-09
consolidation, which merged seven ~300-word concept pages into three chapters
and removed 37 broken links.

## Terminology — one word per concept

Terms come from what the code calls things (`lineage.ModelNode`,
`scope.ScopePlan`, `catalog.Pin`, `state.BranchState`, `core.changeset()`).
Every term here has a definition in `glossary.md`; if you add a term to one,
add it to the other.

| Use | Not | Note |
| --- | --- | --- |
| **model** | query, SQL file (after first definition) | one SQL file → one table |
| **change-set** | changeset, "your change" | the primary state key |
| **data branch** | branch, when a git branch is also in play | plain "branch" is fine once context is set |
| **scope** | blast radius, downstream closure | "blast radius" is allowed **once**, on the landing page. "downstream closure" only where the closure rule is being defined. |
| **pin** | bookmark, pin tag | a pin *is* an Iceberg tag; say "tag" only in `iceberg-refs.md` |
| **upstream input** | input, upstream table, source | a table a model reads that is not itself a model |
| **`main`** | production, base | "base" only when naming `warehouse.default_base` |
| **promote** / **discard** | apply, merge, ship | never soften "no merge" |
| **lakehouse** | warehouse | "warehouse" only for Snowflake/BigQuery-style products and the staging copy Reble replaces |

Numbers have one format too: branch creation is **`< 10 ms`**, everywhere.

## Voice

`index.mdx` and `comparisons.md` are **exempt** — they are the pitch and keep
a persuasive voice. Everything else follows these.

1. **One idea per sentence, ~25 words.** The failure mode is triple-clause
   em-dash sentences.
2. **No rhetorical questions.** State the problem declaratively.
3. **No aphorisms or value claims.** Not "where correctness goes to die", not
   "a guess wearing a lab coat", not "meant to die young".
4. **No defensive headings.** Not "Honest limits" — just "Limits". Not
   "answered honestly", not "the honest one-paragraph version". Saying the
   docs are honest is not evidence that they are.
5. **Descriptive titles.** A title should not need the page read to decode it.
6. **Open with one sentence naming what the page covers and who needs it.**
   No page opens with a story or a slogan.
7. **Bold is for defined terms and table cells.** Not mid-sentence emphasis.
8. **No numbers that rot.** No test counts, no version assertions, no "nobody
   has hit that yet".
9. **Second person is fine.** "You" is working; keep it.

## Links

**In-body links are root-absolute: `/reble/<slug>/`.**

Starlight does not rewrite relative `.md` links in this setup — not in `.md`,
not in `.mdx`, not in MDX-processed `.md` (any file with an `import`). A
relative link survives verbatim into the built HTML and 404s. That is how 37
broken links shipped.

`npm run linkcheck` fails on any surviving `.md` extension, any unresolvable
path, and any `#fragment` with no matching `id` on the target page. Run
`npm run build && npm run linkcheck` before pushing.

## Accuracy

The CLI reference must match `reble --help`, not the spec or intent. Before
editing `cli.md`, run the command. Three separate cases of documented-but-
nonexistent commands and flags reached production because nobody did.

Anything you can execute, execute — paste real terminal output rather than
writing plausible output. `tests/test_example.py` runs
`examples/orders-lakehouse` end to end and is the source for quickstart
output.

## Structure

Aim for chapters, not fragments. A page under ~400 words is usually a section
of something else. The previous split into seven micro concept pages made
readers visit seven pages to learn one loop, and let the same loop get
re-explained on five of them.
