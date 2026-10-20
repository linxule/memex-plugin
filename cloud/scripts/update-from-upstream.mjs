#!/usr/bin/env node
// Bring a Deploy-to-Cloudflare copy of memex cloud up to a newer upstream
// release. Run by .github/workflows/update-memex-cloud.yml (which commits the
// result and opens a pull request); also works by hand from a clone:
//
//   node scripts/update-from-upstream.mjs [--ref cloud-v0.3.0|<branch>] [--from <ref>] [--check]
//
// The Deploy button copies upstream's cloud/ folder into a new repo and edits
// two files: wrangler.jsonc (Worker name, D1/Vectorize names, resource IDs) and
// package.json's "name". An update therefore keeps wrangler.jsonc as it is,
// keeps package.json's name, and skips .github/workflows (a workflow's token
// may not push workflow changes). Everything else is replaced by the release,
// and files the release removed are deleted. Changes upstream made to the
// skipped files are described in the pull request body for a hand merge.
//
// Environment: MEMEX_UPSTREAM (default linxule/memex-plugin), REF (same as
// --ref), GITHUB_OUTPUT (set by Actions; receives updated/ref/version/body).

import { execFileSync } from "node:child_process";
import { appendFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, sep } from "node:path";

const UPSTREAM = process.env.MEMEX_UPSTREAM || "linxule/memex-plugin";
const ROOT = process.cwd();
const CHECK = process.argv.includes("--check"); // report only, write nothing
const KEEP = (rel) => rel === "wrangler.jsonc" || rel.startsWith(".github/workflows/");

function arg(name) {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : undefined;
}

function semver(v) {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(v ?? "");
  return m ? m.slice(1).map(Number) : null;
}

function newer(a, b) {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

function output(key, value) {
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `${key}<<__EOF__\n${value}\n__EOF__\n`);
}

function releaseTags() {
  const out = execFileSync("git", ["ls-remote", "--tags", `https://github.com/${UPSTREAM}.git`, "refs/tags/cloud-v*"], {
    encoding: "utf8",
  });
  return out
    .split("\n")
    .map((line) => line.split("refs/tags/")[1])
    .filter((tag) => tag && !tag.endsWith("^{}") && semver(tag.slice("cloud-v".length)))
    .map((tag) => ({ tag, version: semver(tag.slice("cloud-v".length)) }));
}

/** Download upstream at `ref` and return the path of its cloud/ folder, or null if the ref doesn't exist. */
async function fetchCloud(ref) {
  const path = ref.split("/").map(encodeURIComponent).join("/");
  const res = await fetch(`https://codeload.github.com/${UPSTREAM}/tar.gz/${path}`, {
    headers: { "user-agent": "memex-cloud-updater" },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`downloading ${UPSTREAM}@${ref}: HTTP ${res.status}`);
  const dir = mkdtempSync(join(tmpdir(), "memex-upstream-"));
  const archive = join(dir, "src.tar.gz");
  writeFileSync(archive, Buffer.from(await res.arrayBuffer()));
  execFileSync("tar", ["-xzf", archive, "-C", dir]);
  const top = readdirSync(dir).find((name) => statSync(join(dir, name)).isDirectory());
  const cloud = top && join(dir, top, "cloud");
  if (!cloud || !existsSync(cloud)) throw new Error(`${UPSTREAM}@${ref} has no cloud/ folder`);
  return cloud;
}

/** Regular files under `base`. Symlinks are never followed or copied. */
function listFiles(base) {
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      const st = lstatSync(full);
      if (st.isSymbolicLink()) continue;
      if (st.isDirectory()) {
        if (name !== "node_modules" && name !== ".git" && name !== ".wrangler") walk(full);
      } else if (st.isFile()) files.push(relative(base, full));
    }
  };
  walk(base);
  return files.sort();
}

const REAL_ROOT = realpathSync(ROOT);

/** A path this script may write or delete: inside the checkout, not a symlink, and not under a symlinked directory. */
function safeTarget(rel) {
  const target = join(ROOT, rel);
  if (rel.split(/[\\/]/).includes("..")) return null;
  if (existsSync(target) && lstatSync(target).isSymbolicLink()) return null;
  let dir = dirname(target);
  while (!existsSync(dir)) dir = dirname(dir);
  const real = realpathSync(dir);
  return real === REAL_ROOT || real.startsWith(REAL_ROOT + sep) ? target : null;
}

const read = (path) => (existsSync(path) ? readFileSync(path, "utf8") : null);

function diff(a, b, label) {
  const from = existsSync(a) ? a : "/dev/null";
  try {
    execFileSync("git", ["diff", "--no-index", "--no-color", "--no-prefix", "--", from, b], { encoding: "utf8" });
    return "";
  } catch (err) {
    // exit 1 = files differ; the diff is on stdout
    if (err.status !== 1) throw err;
    // Replace git's temp-dir paths in the headers with the file's own name.
    return (err.stdout ?? "")
      .split("\n")
      .map((line) =>
        line.startsWith("diff --git ") ? `diff --git a/${label} b/${label}`
        : line.startsWith("--- ") && !line.endsWith("/dev/null") ? `--- a/${label}`
        : line.startsWith("+++ ") ? `+++ b/${label}`
        : line,
      )
      .join("\n");
  }
}

const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
const current = semver(pkg.version);
const refArg = arg("--ref") || process.env.REF || "";

let ref = refArg;
let version = null;
if (!ref) {
  const tags = releaseTags();
  if (tags.length === 0) {
    console.log(`No cloud-v* releases on ${UPSTREAM} yet.`);
    output("updated", "false");
    process.exit(0);
  }
  const latest = tags.reduce((best, t) => (newer(t.version, best.version) ? t : best));
  ref = latest.tag;
  version = latest.version.join(".");
  if (current && !newer(latest.version, current)) {
    console.log(`Up to date: this deployment is ${pkg.version}; the newest release is ${version}.`);
    output("updated", "false");
    process.exit(0);
  }
}

const next = await fetchCloud(ref);
if (!next) throw new Error(`${UPSTREAM} has no ref ${ref}`);
version ??= JSON.parse(readFileSync(join(next, "package.json"), "utf8")).version;
const fromRef = arg("--from") || (pkg.version ? `cloud-v${pkg.version}` : null);
const prev = fromRef ? await fetchCloud(fromRef) : null;

const notes = [];
const nextFiles = listFiles(next);
let changed = 0;

for (const rel of nextFiles) {
  if (KEEP(rel)) continue;
  let text = readFileSync(join(next, rel));
  if (rel === "package.json") {
    // Keep the name the Deploy button gave this copy.
    text = Buffer.from(text.toString("utf8").replace(/"name":\s*"[^"]*"/, `"name": ${JSON.stringify(pkg.name)}`));
  }
  const target = safeTarget(rel);
  if (!target) {
    notes.push(`### \`${rel}\` was not written\n\nIt is a symlink or lies outside the repository here.\n`);
    continue;
  }
  if (existsSync(target) && readFileSync(target).equals(text)) continue;
  if (!CHECK) {
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, text);
  }
  changed++;
}

if (prev) {
  for (const rel of listFiles(prev)) {
    if (KEEP(rel) || nextFiles.includes(rel) || !existsSync(join(ROOT, rel))) continue;
    const target = safeTarget(rel);
    if (!target) continue;
    if (!CHECK) rmSync(target);
    changed++;
  }
}

for (const rel of ["wrangler.jsonc", ...nextFiles.filter((f) => f.startsWith(".github/workflows/"))]) {
  const base = prev ? join(prev, rel) : join(ROOT, rel);
  if (read(base) === read(join(next, rel))) continue;
  const patch = "```diff\n" + diff(base, join(next, rel), rel).trim() + "\n```\n";
  if (!existsSync(join(ROOT, rel))) {
    notes.push(
      `### \`${rel}\` is new\n\nAdd it by hand (a workflow's token can't add workflow files)` +
        `${rel === ".github/workflows/update-memex-cloud.yml" ? " to get these update pull requests automatically" : ""}:\n\n` + patch,
    );
    continue;
  }
  const what = rel === "wrangler.jsonc"
    ? "keeps your resource names and IDs, so it was not replaced"
    : "can't be changed by this workflow's token, so it was not replaced";
  const against = prev
    ? `the change between ${fromRef} and this release`
    : "your copy against this release (the release you are on has no tag, so your own names and IDs show up too)";
  notes.push(`### \`${rel}\` changed upstream\n\nThis file ${what}. Apply by hand; below is ${against}:\n\n` + patch);
}

const label = ref.startsWith("cloud-v") ? ref : `${UPSTREAM}@${ref}`;
const body = [
  `Updates this memex cloud deployment from ${pkg.version ?? "an unknown version"} to **${version}** (${label}).`,
  "",
  `Release notes: https://github.com/${UPSTREAM}/releases${ref.startsWith("cloud-v") ? `/tag/${ref}` : ""}`,
  "",
  "Merging redeploys the Worker through Workers Builds; the deploy script applies any new D1 migrations.",
  `\`wrangler.jsonc\` (your resource names and IDs), \`package.json\`'s name and \`.github/workflows\` were left as they are.`,
  ...(notes.length ? ["", "## Needs a hand merge", "", ...notes] : []),
].join("\n");

console.log(`${changed} file(s) differ from ${label}${notes.length ? `; ${notes.length} need a hand merge` : ""}.`);
console.log(body);
const bodyPath = join(mkdtempSync(join(tmpdir(), "memex-pr-")), "body.md");
writeFileSync(bodyPath, body + "\n");
output("updated", changed > 0 || notes.length > 0 ? "true" : "false");
output("ref", ref);
output("version", version);
output("body", bodyPath);
