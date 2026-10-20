#!/usr/bin/env node
// Make sure the Vectorize index exists with the right shape before deploy.
//
// The Deploy to Cloudflare button provisions bindings, but an index's
// dimensions/metric and its metadata indexes (what lets search filter by
// project, type and date inside the KNN) are created here. Idempotent: safe
// to run on every deploy.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const config = JSON.parse(
  readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/,(\s*[}\]])/g, "$1"),
);
const index = config.vectorize?.[0]?.index_name ?? "memex";
const dims = Number(config.vars?.VECTOR_DIMENSIONS ?? 768);
const METADATA = [
  ["kind", "string"],
  ["project", "string"],
  ["type", "string"],
  ["date", "number"],
];

function wrangler(args, { allowFail = false } = {}) {
  try {
    return execFileSync("npx", ["wrangler", ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch (err) {
    if (allowFail) return null;
    process.stderr.write(err.stdout ?? "");
    process.stderr.write(err.stderr ?? "");
    throw err;
  }
}

function parseJson(out) {
  if (!out) return null;
  const start = out.search(/[[{]/);
  try {
    return start >= 0 ? JSON.parse(out.slice(start)) : null;
  } catch {
    return null;
  }
}

const existing = parseJson(wrangler(["vectorize", "get", index, "--json"], { allowFail: true }));
if (!existing) {
  console.log(`Creating Vectorize index "${index}" (${dims}d, cosine)`);
  wrangler(["vectorize", "create", index, `--dimensions=${dims}`, "--metric=cosine"]);
} else {
  const have = existing.config?.dimensions;
  const metric = existing.config?.metric;
  const wrong = (have && have !== dims) || (metric && metric !== "cosine");
  const count = parseJson(wrangler(["vectorize", "info", index, "--json"], { allowFail: true }))?.vectorCount;
  if (wrong && count === 0) {
    // The Deploy button provisions the index from dimensions/metric typed into
    // its form, before this script runs. An empty index loses nothing by being
    // recreated with the right shape.
    console.log(`Vectorize index "${index}" is empty and ${have}d/${metric}; recreating it as ${dims}d/cosine`);
    wrangler(["vectorize", "delete", index, "--force"]);
    wrangler(["vectorize", "create", index, `--dimensions=${dims}`, "--metric=cosine"]);
    console.log(`Vectorize index "${index}" recreated (${dims}d, cosine)`);
  } else if (wrong) {
    console.error(
      `Vectorize index "${index}" is ${have}d/${metric} but needs ${dims}d/cosine and already holds vectors. ` +
        `Either set VECTOR_DIMENSIONS to ${have}, or delete the index (wrangler vectorize delete ${index}) and deploy again.`,
    );
    process.exit(1);
  } else {
    console.log(`Vectorize index "${index}" exists (${have ?? "?"}d)`);
  }
}

const indexed = new Set(
  (parseJson(wrangler(["vectorize", "list-metadata-index", index, "--json"], { allowFail: true })) ?? []).map(
    (m) => m.propertyName,
  ),
);
for (const [name, type] of METADATA) {
  if (indexed.has(name)) continue;
  console.log(`Creating metadata index ${index}.${name} (${type})`);
  wrangler(["vectorize", "create-metadata-index", index, `--propertyName=${name}`, `--type=${type}`]);
}
