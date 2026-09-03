// Asserts every internal link in the built site resolves to a real file.
// Replaces mkdocs --strict as the CI gate. Run after `npm run build`.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

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

const broken = [];
const hrefRe = /href="([^"]*)"/g;

for (const file of htmlFiles) {
  const html = readFileSync(file, "utf8");
  for (const match of html.matchAll(hrefRe)) {
    let href = match[1];
    if (!href || href.startsWith("#")) continue;
    if (/^[a-z]+:\/\//i.test(href) || href.startsWith("mailto:")) continue;
    if (!href.startsWith(BASE)) continue; // external or off-site paths
    // page links only — css/js/svg/sitemap are assets, checked by the build itself
    if (/\.(css|js|mjs|json|xml|svg|png|jpg|gif|webp|ico|txt|webmanifest)$/i.test(href)) continue;
    const path = href.slice(BASE.length).split("#")[0].split("?")[0];
    if (path === "" || path === "/") continue;
    const target = join(DIST, path, "index.html");
    const targetFile = join(DIST, path);
    let ok;
    try {
      ok = statSync(target).isFile() || statSync(targetFile).isFile();
    } catch {
      ok = false;
    }
    if (!ok) broken.push(`${file.replace(DIST, "")} -> ${href}`);
  }
}

if (broken.length) {
  console.error(`linkcheck: ${broken.length} broken internal link(s):`);
  for (const b of broken) console.error("  " + b);
  process.exit(1);
}
console.log(`linkcheck: ${htmlFiles.length} pages, all internal links resolve.`);
