import { truncateUnit } from "./text";

// Query-time embeddings. The vectors in Vectorize come from your local index
// (pushed by `memex cloud push`), so a query must be embedded by the same
// model the push recorded in `meta`, then truncated to the index dimension.

export interface EmbeddingInfo {
  provider: string;
  model: string;
  dimensions: number;
}

export async function embeddingInfo(env: Env): Promise<EmbeddingInfo | null> {
  const rows = await env.DB.prepare(
    "SELECT key, value FROM meta WHERE key IN ('embedding_provider', 'embedding_model', 'embedding_dimensions')",
  ).all<{ key: string; value: string }>();
  const m = Object.fromEntries(rows.results.map((r) => [r.key, r.value]));
  if (!m.embedding_provider || !m.embedding_model) return null;
  return {
    provider: m.embedding_provider,
    model: m.embedding_model,
    dimensions: Number(m.embedding_dimensions || env.VECTOR_DIMENSIONS),
  };
}

/**
 * Embed `text` with the pushed model, or return null when that is not
 * possible here (no key, or a provider the cloud cannot reach such as a local
 * LM Studio). Callers fall back to keyword search, exactly like the CLI does
 * without a key.
 */
export async function embed(
  env: Env,
  text: string,
  task: "query" | "document",
): Promise<number[] | null> {
  if (!env.VECTORIZE) return null; // nothing to compare a vector against
  const info = await embeddingInfo(env);
  if (!info || info.provider !== "google" || !env.GEMINI_API_KEY) return null;

  const body: Record<string, unknown> = {
    model: `models/${info.model}`,
    content: { parts: [{ text }] },
  };
  // Gemini Embedding 2 takes no task type (memex/scripts/embeddings.py
  // `_build_embed_config`); earlier Gemini embedding models do.
  if (!info.model.startsWith("gemini-embedding-2")) {
    body.taskType = task === "query" ? "RETRIEVAL_QUERY" : "RETRIEVAL_DOCUMENT";
  }
  const res = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(info.model)}:embedContent`,
    {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) {
    console.warn("embedding failed", res.status, (await res.text()).slice(0, 300));
    return null;
  }
  const data = (await res.json()) as { embedding?: { values?: number[] } };
  const values = data.embedding?.values;
  if (!values?.length) return null;
  return truncateUnit(values, info.dimensions);
}
