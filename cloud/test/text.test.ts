import { describe, expect, it } from "vitest";
import { chunkId, dateInt, ftsQuery, rrf, safeEqual, sinceCutoff, slugify, stripFrontmatter, truncateUnit } from "../src/text";

describe("ftsQuery", () => {
  it("drops stop words and ORs quoted terms, like extract_fts_keywords", () => {
    expect(ftsQuery("why did we choose hybrid search?")).toBe('"choose" OR "hybrid" OR "search"');
  });
  it("keeps short technical terms and caps at 7 terms", () => {
    expect(ftsQuery("AI ML a b c d e f g h")).toBe('"ai" OR "ml"');
    expect(ftsQuery("one two three four five six seven eight").split(" OR ")).toHaveLength(7);
  });
  it("falls back to stop words when nothing else is left", () => {
    expect(ftsQuery("what is it")).toBe('"what" OR "is" OR "it"');
  });
  it("neutralises FTS syntax in user input", () => {
    expect(ftsQuery('NEAR(x y) "quoted" col:val*')).toBe('"near" OR "quoted" OR "col" OR "val"');
    expect(ftsQuery("???")).toBe("");
  });
});

describe("rrf", () => {
  it("rewards documents found by both retrievers", () => {
    const fused = rrf([
      ["a", "b", "c"],
      ["c", "d"],
    ]);
    expect(fused[0]!.key).toBe("c");
    expect(fused[0]!.hits).toBe(2);
    expect(fused[0]!.score).toBeCloseTo(1 / 63 + 1 / 61);
  });
  it("counts a document once per list at its best rank", () => {
    const fused = rrf([["a", "a", "b"]]);
    expect(fused.find((h) => h.key === "b")!.score).toBeCloseTo(1 / 62);
  });
});

describe("truncateUnit", () => {
  it("truncates and renormalises to unit length", () => {
    const v = truncateUnit([3, 4, 12], 2);
    expect(v).toEqual([0.6, 0.8]);
  });
  it("normalises without truncating when dims >= length", () => {
    const v = truncateUnit([3, 4], 768);
    expect(Math.hypot(...v)).toBeCloseTo(1);
  });
});

describe("dates", () => {
  it("encodes YYYYMMDD and handles unknowns", () => {
    expect(dateInt("2026-10-19T08:00:00Z")).toBe(20261019);
    expect(dateInt("")).toBe(0);
  });
  it("parses relative windows", () => {
    const now = new Date("2026-10-19T12:00:00Z");
    expect(sinceCutoff("7d", now)).toBe("2026-10-12");
    expect(sinceCutoff("2w", now)).toBe("2026-10-05");
    expect(sinceCutoff("2026-01-01", now)).toBe("2026-01-01");
    expect(sinceCutoff("soon", now)).toBeNull();
  });
});

describe("ids and names", () => {
  it("keeps Vectorize ids under 64 bytes", async () => {
    const id = await chunkId("projects/" + "x".repeat(500) + "/memos/a.md", 12345);
    expect(new TextEncoder().encode(id).length).toBeLessThanOrEqual(64);
  });
  it("slugifies titles", () => {
    expect(slugify("Cloud Mirror: Smoke Test!")).toBe("cloud-mirror-smoke-test");
    expect(slugify("???")).toBe("memo");
  });
  it("strips frontmatter only at the top", () => {
    expect(stripFrontmatter("---\ntitle: x\n---\n# Body\n---\n")).toBe("# Body\n---\n");
    expect(stripFrontmatter("# No frontmatter")).toBe("# No frontmatter");
  });
  it("compares secrets", async () => {
    expect(await safeEqual("abc", "abc")).toBe(true);
    expect(await safeEqual("abc", "abd")).toBe(false);
    expect(await safeEqual("", "")).toBe(false);
  });
});
