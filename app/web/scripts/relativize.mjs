/**
 * Make the static export servable under any URL prefix.
 *
 * Two absolute-path assumptions are baked into a Next export and rewritten
 * here, once, at build time:
 *
 * 1. HTML asset references ("/_next/...") become relative ("./_next/...") --
 *    every exported page sits at the export root (the app routes by hash), so
 *    "./" always resolves to the directory _next lives in.
 * 2. webpack's runtime publicPath (".p=\"/_next/\"") is what *lazy* chunks
 *    load through; it is replaced with a global the page computes from its own
 *    location before any script runs.
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const out = join(import.meta.dirname, "..", "out");

const PREFIX_SNIPPET =
  '<script>self.__semAssets=location.pathname.replace(/[^/]*$/,"")+"_next/";</script>';

let htmlCount = 0;
for (const name of readdirSync(out)) {
  if (!name.endsWith(".html") && !name.endsWith(".txt")) continue;
  const path = join(out, name);
  const before = readFileSync(path, "utf-8");
  let after = before
    .replaceAll('"/_next/', '"./_next/')
    .replaceAll("\\/_next\\/", ".\\/_next\\/")
    .replaceAll('href="/favicon', 'href="./favicon');
  if (name.endsWith(".html") && !after.includes("__semAssets")) {
    after = after.replace("<head>", "<head>" + PREFIX_SNIPPET);
  }
  if (after !== before) {
    writeFileSync(path, after);
    htmlCount++;
  }
}

// The webpack runtime chunk carries the publicPath assignment (minified as
// `X.p="/_next/"`); other chunks may embed the same literal for asset URLs.
let jsCount = 0;
const chunks = join(out, "_next", "static", "chunks");
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      walk(path);
      continue;
    }
    if (!name.endsWith(".js")) continue;
    const before = readFileSync(path, "utf-8");
    const after = before.replaceAll('.p="/_next/"', '.p=self.__semAssets||"/_next/"');
    if (after !== before) {
      writeFileSync(path, after);
      jsCount++;
    }
  }
};
walk(chunks);

console.log(`relativize: rewrote ${htmlCount} page(s), ${jsCount} runtime chunk(s)`);
