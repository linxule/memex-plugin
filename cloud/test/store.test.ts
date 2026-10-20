import { describe, expect, it } from "vitest";
import { applyPlans, deleteDocs, type WritePlan } from "../src/store";

// D1 holds the content hash the next push compares, and the old vector ids a
// retry must delete. So vectors are written first: if they fail, D1 must be
// untouched (the push re-sends), and a D1 failure leaves the ids findable.
function fakeEnv(opts: { failVectors?: boolean } = {}) {
  const calls: string[] = [];
  const stmt = { bind: () => stmt, all: async () => ({ results: [{ id: "old-1" }] }), run: async () => ({}) };
  const env = {
    VECTOR_DIMENSIONS: "2",
    DB: {
      prepare: () => stmt,
      batch: async () => {
        calls.push("d1.batch");
        return [];
      },
    },
    VECTORIZE: {
      upsert: async () => {
        calls.push("vectors.upsert");
        if (opts.failVectors) throw new Error("vectorize down");
      },
      deleteByIds: async () => {
        calls.push("vectors.delete");
      },
    },
  } as unknown as Env;
  return { env, calls };
}

const plan = (id: string): WritePlan => ({
  stmts: [{} as D1PreparedStatement],
  upserts: [{ id, values: [1, 0] }],
  deletes: ["stale-" + id],
});

describe("write ordering", () => {
  it("applyPlans writes every vector before any D1 batch", async () => {
    const { env, calls } = fakeEnv();
    await applyPlans(env, [plan("a"), plan("b")]);
    expect(calls).toEqual(["vectors.upsert", "vectors.delete", "d1.batch", "d1.batch"]);
  });

  it("a Vectorize failure leaves D1 untouched", async () => {
    const { env, calls } = fakeEnv({ failVectors: true });
    await expect(applyPlans(env, [plan("a")])).rejects.toThrow("vectorize down");
    expect(calls).not.toContain("d1.batch");
  });

  it("deleteDocs removes vectors before the D1 rows that list them", async () => {
    const { env, calls } = fakeEnv();
    await deleteDocs(env, ["projects/p/memos/x.md"]);
    expect(calls.indexOf("vectors.delete")).toBeLessThan(calls.indexOf("d1.batch"));
  });

  it("deletes vectors in calls of at most 100 ids (the Vectorize limit)", async () => {
    const sizes: number[] = [];
    const env = {
      VECTOR_DIMENSIONS: "2",
      DB: { prepare: () => ({}), batch: async () => [] },
      VECTORIZE: { upsert: async () => {}, deleteByIds: async (ids: string[]) => void sizes.push(ids.length) },
    } as unknown as Env;
    const stale = Array.from({ length: 250 }, (_, i) => `o:${i}`);
    await applyPlans(env, [{ stmts: [], upserts: [], deletes: stale }]);
    expect(sizes).toEqual([100, 100, 50]);
  });
});
