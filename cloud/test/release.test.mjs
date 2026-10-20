// Release bookkeeping, in plain JS so it can use Node APIs the Worker's
// TypeScript config does not include.
import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { EXPECTED_SCHEMA, SERVER_VERSION } from "../src/version";

const root = new URL("../", import.meta.url);

describe("release metadata", () => {
  it("SERVER_VERSION equals package.json's version (the cloud-v<version> tag)", () => {
    const pkg = JSON.parse(readFileSync(new URL("package.json", root), "utf8"));
    expect(SERVER_VERSION).toBe(pkg.version);
  });

  it("EXPECTED_SCHEMA is the newest migration file", () => {
    const files = readdirSync(new URL("migrations/", root)).filter((f) => f.endsWith(".sql")).sort();
    expect(EXPECTED_SCHEMA).toBe(files.at(-1));
  });
});
