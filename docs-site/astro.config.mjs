import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Docs for Reble — served at https://satya1395.github.io/reble/
//
// Sidebar order follows the reader: evaluate (Start here) → understand
// (Concepts) → operate (Guides) → look up (Reference). Slugs stay flat so
// URLs match the previous mkdocs site; pages that were merged away keep
// working through `redirects` below.
export default defineConfig({
  site: "https://satya1395.github.io",
  base: "/reble",

  // Pages merged into chapters during the 2026-09 docs consolidation.
  // Keep these: they are linked from the README, PyPI, and outside.
  redirects: {
    "/scope": "/reble/models/",
    "/pins": "/reble/branches/",
    "/promote": "/reble/branches/",
    "/git": "/reble/running/",
    "/refreshes": "/reble/running/",
    "/incremental": "/reble/running/",
    "/scenarios": "/reble/comparisons/",
    "/replaces": "/reble/comparisons/",
    "/concepts": "/reble/how-it-works/",
  },

  integrations: [
    starlight({
      title: "Reble",
      description:
        "An open SQL engine for your Iceberg lakehouse: scoped zero-copy branches, row-level diffs, fast-forward promotion.",
      editLink: {
        baseUrl: "https://github.com/satya1395/reble/edit/main/docs-site",
      },
      social: [
        { icon: "github", label: "GitHub", href: "https://github.com/satya1395/reble" },
      ],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Start here",
          items: [
            { label: "Introduction", link: "/" },
            { label: "Quickstart", slug: "getting-started" },
            { label: "Quickstart on AWS", slug: "aws" },
          ],
        },
        {
          label: "Concepts",
          items: [
            { label: "How Reble works", slug: "how-it-works" },
            { label: "Models and lineage", slug: "models" },
            { label: "Branches and promotion", slug: "branches" },
            { label: "Running and scheduling", slug: "running" },
          ],
        },
        {
          label: "Guides",
          items: [
            { label: "Airflow and CI", slug: "airflow" },
            { label: "Engines", slug: "engines" },
            { label: "Performance", slug: "performance" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "CLI reference", slug: "cli" },
            { label: "Configuration", slug: "config" },
            { label: "Exit codes and JSON output", slug: "exit-codes" },
            { label: "MCP and agents", slug: "mcp" },
            { label: "Comparisons", slug: "comparisons" },
            { label: "Glossary", slug: "glossary" },
          ],
        },
        {
          label: "Deep dive",
          items: [{ label: "Iceberg refs", slug: "iceberg-refs" }],
        },
      ],
    }),
  ],
});
