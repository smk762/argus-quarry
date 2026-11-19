// Post-build check for the hosted demo (`make pages`): the export is pure static files, and it publishes nothing it
// must not.
//
//   - every page carries the hash-based CSP <meta>, and _headers is there for the host
//   - no page loads anything from another origin (no <img>, <script> or <link> to Commons or anywhere else)
//   - the tape is in the export, with no identity record
//   - an image file exists only for a record that landed with a PD or CC0 licence, every such record has its file,
//     and there is no other file under tape/ (so nothing of a quarantined or excluded candidate can be fetched)
//   - every record has its own page
//   - no home or server path, private address or email address in the tape or any page
//   - the whole export stays small (Cloudflare Pages caps a file at 25 MiB; this site aims under 30 MB in all)
//
//   node scripts/check-static.mjs [outDir]   (run by `npm run build`)
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { denylistFromEnv, opsecHits } from "./opsec.mjs";

const out = process.argv[2] ?? "out";
const problems = [];
const list = (dir) =>
  existsSync(dir)
    ? readdirSync(dir, { withFileTypes: true, recursive: true })
        .filter((e) => e.isFile())
        .map((e) => join(e.parentPath, e.name))
    : [];
const files = list(out);
const denylist = denylistFromEnv();
const ACCEPTED = new Set(["PD", "CC0"]);
const FOREIGN = /<(?:img|script|link|source|iframe)\b[^>]*\b(?:src|href|srcset)="(?:https?:)?\/\/[^"]*"/gi;

for (const file of files.filter((f) => f.endsWith(".html"))) {
  const html = readFileSync(file, "utf8");
  if (!html.includes('http-equiv="Content-Security-Policy"')) problems.push(`${file} has no CSP <meta>`);
  for (const [tag] of html.matchAll(FOREIGN)) {
    // Plain <a href> links out are fine; a <link> to another origin is not, except the manifest/icons, which are local.
    problems.push(`${file} loads from another origin: ${tag.slice(0, 120)}`);
  }
  for (const hit of opsecHits(html, denylist)) problems.push(`${file}:${hit.line}: ${hit.kind} (${hit.match})`);
}
if (!existsSync(join(out, "_headers"))) problems.push("no _headers file");

const root = join(out, "tape");
const tapeJson = join(root, "tape.json");
if (!existsSync(tapeJson)) problems.push(`no tape at ${tapeJson}`);
else {
  const text = readFileSync(tapeJson, "utf8");
  const tape = JSON.parse(text);
  const listed = new Set(["tape.json"]);
  for (const record of tape.records) {
    if (record.category === "identity") problems.push(`${record.id} is an identity record`);
    const cleared = record.verdict === "landed" && ACCEPTED.has(record.licence.normalised);
    if (record.image) {
      if (!cleared) problems.push(`${record.id} is ${record.verdict} (${record.licence.raw}) but has an image`);
      if (record.image.src !== `thumbs/${record.id}.webp`) problems.push(`${record.id}: unexpected image path ${record.image.src}`);
      listed.add(record.image.src);
      if (!existsSync(join(root, record.image.src))) problems.push(`tape file missing: ${record.image.src}`);
    } else if (cleared) problems.push(`${record.id} landed with ${record.licence.raw} but has no image`);
    if (!existsSync(join(out, "record", record.id, "index.html"))) problems.push(`no page for record ${record.id}`);
  }
  for (const path of list(root)) {
    const rel = relative(root, path);
    if (!listed.has(rel)) problems.push(`tape/${rel} is on disk but no cleared record lists it`);
  }
  for (const hit of opsecHits(text, denylist)) problems.push(`${tapeJson}:${hit.line}: ${hit.kind} (${hit.match})`);
}

const MiB = 1024 * 1024;
let total = 0;
for (const file of files) {
  const size = statSync(file).size;
  total += size;
  if (size > 25 * MiB) problems.push(`${file} is ${(size / MiB).toFixed(1)} MiB, over Cloudflare Pages' 25 MiB file cap`);
}
if (total > 30 * 1000 * 1000) problems.push(`the export is ${(total / MiB).toFixed(1)} MiB, over the 30 MB budget`);

if (problems.length) {
  console.error(`check-static: ${problems.length} problem(s)\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(`check-static: ${files.length} files, ${(total / MiB).toFixed(2)} MiB, pure static, CSP on every page, images for landed PD/CC0 records only`);
