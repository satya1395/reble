import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Docs for Reble — served at https://satya1395.github.io/reble/
// Sidebar groups follow the reader-intent pattern (learn → do → evaluate →
// lookup), with flat slugs so URLs match the previous mkdocs site exactly.
export default defineConfig({
  site: "https://satya1395.github.io",
  base: "/reble",
  integrations: [
    starlight({
      title: "Reble",
      description:
        "Git-style branching for Iceberg data warehouses: scoped branches, row-level diffs, fast-forward promotion.",
      logo: { src: "./src/assets/logo.svg" },
      favicon: "/favicon.svg",
      editLink: {
        baseUrl: "https://github.com/satya1395/reble/edit/main/docs-site",
      },
      social: [
        { icon: "github", label: "GitHub", href: "https://github.com/satya1395/reble" },
      ],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "Introduction", link: "/" },
            { label: "Quickstart", slug: "getting-started" },
          ],
        },
        {
          label: "Core concepts",
          items: [
            { label: "Models", slug: "models" },
            { label: "Scope", slug: "scope" },
            { label: "Refreshes", slug: "refreshes" },
            { label: "Incremental & retries", slug: "incremental" },
            { label: "Pins", slug: "pins" },
            { label: "Promote or discard", slug: "promote" },
            { label: "Git, optional", slug: "git" },
          ],
        },
        {
          label: "Architecture",
          items: [
            { label: "How Reble works", slug: "how-it-works" },
            { label: "Iceberg refs", slug: "iceberg-refs" },
          ],
        },
        {
          label: "Guides",
          items: [
            { label: "AWS (Glue + S3)", slug: "aws" },
            { label: "Airflow", slug: "airflow" },
            { label: "Engines", slug: "engines", badge: "New" },
            { label: "Performance", slug: "performance" },
          ],
        },
        {
          label: "Why Reble",
          items: [
            { label: "Days you've had", slug: "scenarios" },
            { label: "What Reble replaces", slug: "replaces" },
            { label: "Comparisons", slug: "comparisons" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "CLI", slug: "cli" },
            { label: "Configuration", slug: "config" },
          ],
        },
      ],
    }),
  ],
});
