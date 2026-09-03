// Asserts every internal link in the built site resolves to a real file, and that
// every #fragment resolves to a real id. Replaces mkdocs --strict as the CI gate.
// Run after `npm run build`.
//
// Authoring rule this enforces: in-body links are root-absolute (`/reble/<slug>/`).
// Relative `.md` links are NOT rewritten by Starlight in this setup — not in .md,
// not in .mdx, not in MDX-processed .md — so a `.md` extension surviving into built
// HTML is always an authoring error and is failed unconditionally.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, posix, resolve } from "node:path";

const DIST = resolve(process.argv[2] ?? "dist");
const BASE = "/reble";

const htmlFiles = [];
(function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full);
    else if (entry.endsWith(".html")) htmlFiles.push(full);
  }
})(DIST);

// id="..." on any element, plus name="..." on anchors — what a #fragment can target.
const idsOf = (html) => {
  const ids = new Set();
  for (const m of html.matchAll(/\sid="([^"]+)"/g)) ids.add(m[1]);
  for (const m of html.matchAll(/<a[^>]*\sname="([^"]+)"/g)) ids.add(m[1]);
  return ids;
};

const idCache = new Map();
const idsForFile = (file) => {
  if (!idCache.has(file)) idCache.set(file, idsOf(readFileSync(file, "utf8")));
  return idCache.get(file);
};

const ASSET_RE = /\.(css|js|mjs|json|xml|svg|png|jpg|jpeg|gif|webp|ico|txt|webmanifest|pdf|woff2?)$/i;
const MD_RE = /\.mdx?($|[#?])/i;

const broken = [];
const hrefRe = /href="([^"]*)"/g;

for (const file of htmlFiles) {
  const html = readFileSync(file, "utf8");
  const rel = file.replace(DIST, "");
  // The page's own URL directory, for resolving relative hrefs.
  const pageDir = posix.dirname(posix.join(BASE, rel.split("/").join(posix.sep)));

  for (const match of html.matchAll(hrefRe)) {
    const href = match[1];
    if (!href || href.startsWith("#")) continue;
    if (/^[a-z][a-z0-9+.-]*:/i.test(href)) continue; // http:, https:, mailto:, data:
    if (href.startsWith("//")) continue;             // protocol-relative

    // A .md/.mdx extension in built HTML is always an authoring error, however it
    // would otherwise resolve. This is the class the old checker skipped entirely.
    if (MD_RE.test(href)) {
      broken.push(`${rel} -> ${href}  (raw .md link — use /reble/<slug>/)`);
      continue;
    }
    if (ASSET_RE.test(href.split("#")[0].split("?")[0])) continue;

    const [rawPath, fragment] = href.split("#");
    const cleanPath = rawPath.split("?")[0];

    // Resolve relative hrefs against the page's own directory.
    const urlPath = cleanPath === ""
      ? pageDir
      : cleanPath.startsWith("/")
        ? cleanPath
        : posix.normalize(posix.join(pageDir, cleanPath));

    if (!urlPath.startsWith(BASE)) {
      broken.push(`${rel} -> ${href}  (escapes base ${BASE})`);
      continue;
    }
    const path = urlPath.slice(BASE.length);
    if (path === "" || path === "/") continue;

    const asDir = join(DIST, path, "index.html");
    const asFile = join(DIST, path);
    let target = null;
    try { if (statSync(asDir).isFile()) target = asDir; } catch {}
    if (!target) { try { if (statSync(asFile).isFile()) target = asFile; } catch {} }

    if (!target) { broken.push(`${rel} -> ${href}`); continue; }

    if (fragment && !idsForFile(target).has(fragment)) {
      broken.push(`${rel} -> ${href}  (no id="${fragment}" on target page)`);
    }
  }
}

if (broken.length) {
  console.error(`linkcheck: ${broken.length} broken internal link(s):`);
  for (const b of broken) console.error("  " + b);
  process.exit(1);
}
console.log(`linkcheck: ${htmlFiles.length} pages, all internal links resolve.`);
