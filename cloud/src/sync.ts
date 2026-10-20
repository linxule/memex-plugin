import { z } from "zod";
import { applyPlans, deleteDocs, planDoc, planObservations, setMeta } from "./store";
import { safeEqual } from "./text";

// The CLI side of the mirror: `memex cloud push` / `memex cloud pull`.
// Authenticated with the SYNC_TOKEN secret, outside the OAuth flow, because
// it is a machine credential for one owner rather than a user sign-in.

const vector = z.array(z.number()).nullable().optional();

const DocSchema = z.object({
  path: z.string().min(1).max(1024),
  title: z.string().default(""),
  type: z.string().default(""),
  project: z.string().default(""),
  date: z.string().default(""),
  content: z.string(),
  content_hash: z.string().min(8),
  chunks: z.array(z.object({ index: z.number().int().min(0), content: z.string(), vector })).default([]),
});

const ObsSetSchema = z.object({
  doc_path: z.string().min(1),
  set_hash: z.string().min(8),
  project: z.string().default(""),
  type: z.string().default(""),
  date: z.string().default(""),
  observations: z.array(
    z.object({
      content: z.string(),
      content_hash: z.string().min(8),
      obs_type: z.string().optional(),
      confidence: z.string().optional(),
      created_at: z.string().optional(),
      topics: z.array(z.string()).optional(),
      vector,
    }),
  ),
});

const MetaSchema = z.object({
  embedding_provider: z.string(),
  embedding_model: z.string(),
  embedding_dimensions: z.number().int().positive(),
});

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

export async function isSyncRequestAuthorized(request: Request, env: Env): Promise<boolean> {
  const header = request.headers.get("authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  return !!env.SYNC_TOKEN && (await safeEqual(token, env.SYNC_TOKEN));
}

export async function handleSync(request: Request, env: Env): Promise<Response> {
  if (!(await isSyncRequestAuthorized(request, env))) return json({ error: "unauthorized" }, 401);
  const route = new URL(request.url).pathname.replace(/^\/api\/sync\/?/, "");
  const method = request.method;

  try {
    if (method === "GET" && route === "manifest") {
      const [docs, obs, meta] = await Promise.all([
        env.DB.prepare("SELECT path, content_hash, source FROM docs").all<{ path: string; content_hash: string; source: string }>(),
        env.DB.prepare("SELECT doc_path, set_hash FROM obs_sets").all<{ doc_path: string; set_hash: string }>(),
        env.DB.prepare("SELECT key, value FROM meta").all<{ key: string; value: string }>(),
      ]);
      return json({
        vector_dimensions: Number(env.VECTOR_DIMENSIONS),
        docs: Object.fromEntries(docs.results.map((d) => [d.path, { hash: d.content_hash, source: d.source }])),
        observations: Object.fromEntries(obs.results.map((o) => [o.doc_path, o.set_hash])),
        meta: Object.fromEntries(meta.results.map((m) => [m.key, m.value])),
      });
    }

    if (method === "POST" && route === "meta") {
      const meta = MetaSchema.parse(await request.json());
      if (meta.embedding_dimensions !== Number(env.VECTOR_DIMENSIONS)) {
        return json(
          {
            error: `this deployment's Vectorize index is ${env.VECTOR_DIMENSIONS}d but your local index stores ${meta.embedding_dimensions}d vectors`,
          },
          409,
        );
      }
      await setMeta(env, {
        embedding_provider: meta.embedding_provider,
        embedding_model: meta.embedding_model,
        embedding_dimensions: String(meta.embedding_dimensions),
        last_push: new Date().toISOString(),
      });
      return json({ ok: true });
    }

    if (method === "POST" && route === "docs") {
      const { docs } = z.object({ docs: z.array(DocSchema).max(100) }).parse(await request.json());
      // applyPlans writes vectors before D1, so a failure anywhere leaves the
      // affected docs' hashes as they were and the next push re-sends them.
      const plans = [];
      for (const doc of docs) plans.push(await planDoc(env, doc, "vault"));
      await applyPlans(env, plans);
      return json({ upserted: docs.length });
    }

    if (method === "POST" && route === "delete") {
      const { paths } = z.object({ paths: z.array(z.string()).max(500) }).parse(await request.json());
      return json({ deleted: await deleteDocs(env, paths) });
    }

    if (method === "POST" && route === "observations") {
      const { sets } = z.object({ sets: z.array(ObsSetSchema).max(100) }).parse(await request.json());
      const plans = [];
      for (const s of sets) plans.push(await planObservations(env, s.doc_path, s.observations, s.set_hash, s));
      await applyPlans(env, plans);
      return json({ replaced: sets.length });
    }

    if (method === "GET" && route === "inbox") {
      const rows = await env.DB.prepare(
        "SELECT path, content, created_at, client, observations FROM inbox WHERE pulled_at IS NULL ORDER BY id",
      ).all<{ path: string; content: string; created_at: string; client: string; observations: string }>();
      return json({ memos: rows.results.map((r) => ({ ...r, observations: JSON.parse(r.observations || "[]") })) });
    }

    if (method === "POST" && route === "inbox/ack") {
      const { paths } = z.object({ paths: z.array(z.string()).max(500) }).parse(await request.json());
      await env.DB.batch(
        paths.map((p) => env.DB.prepare("UPDATE inbox SET pulled_at = datetime('now') WHERE path = ?").bind(p)),
      );
      return json({ acknowledged: paths.length });
    }

    return json({ error: "not found" }, 404);
  } catch (err) {
    if (err instanceof z.ZodError) return json({ error: "invalid request", issues: err.issues }, 400);
    console.error("sync error", err);
    return json({ error: err instanceof Error ? err.message : String(err) }, 500);
  }
}
