import { embed } from "./embed";
import { dateInt, ftsQuery, rrf, sinceCutoff, snippet } from "./text";

// Hybrid search: FTS5 (BM25) in D1 + KNN in Vectorize, fused with RRF.
// Same shape as memex/scripts/hybrid_search.py, so a query ranks the same
// way in the cloud as on your machine, modulo the index contents.

export interface SearchFilters {
  project?: string;
  type?: string;
  since?: string;
  before?: string;
}

export interface DocHit {
  path: string;
  title: string;
  type: string;
  project: string;
  date: string;
  score: number;
  match: "hybrid" | "keyword" | "semantic";
  snippet: string;
}

interface DocRow {
  path: string;
  title: string;
  type: string;
  project: string;
  date: string;
  snip?: string;
}

const CANDIDATES = 50; // Vectorize topK cap when returning metadata

function sqlFilters(f: SearchFilters, alias = "d"): { sql: string; params: string[] } {
  const parts: string[] = [];
  const params: string[] = [];
  if (f.project) {
    parts.push(`${alias}.project = ?`);
    params.push(f.project);
  }
  if (f.type) {
    parts.push(`${alias}.type = ?`);
    params.push(f.type);
  }
  const since = sinceCutoff(f.since);
  if (since) {
    parts.push(`${alias}.date >= ?`);
    params.push(since);
  }
  if (f.before) {
    // Undated docs ('') sort before every date; exclude them like the CLI does.
    parts.push(`${alias}.date <> '' AND ${alias}.date < ?`);
    params.push(f.before);
  }
  return { sql: parts.length ? ` AND ${parts.join(" AND ")}` : "", params };
}

function vectorFilter(kind: "chunk" | "obs", f: SearchFilters): VectorizeVectorMetadataFilter {
  const filter: VectorizeVectorMetadataFilter = { kind };
  if (f.project) filter.project = f.project;
  if (f.type) filter.type = f.type;
  const since = sinceCutoff(f.since);
  const range: Record<string, number> = {};
  if (since) range.$gte = dateInt(since);
  if (f.before) range.$lt = dateInt(f.before);
  if (Object.keys(range).length) filter.date = range as VectorizeVectorMetadataFilter[string];
  return filter;
}

async function keywordDocs(env: Env, query: string, f: SearchFilters): Promise<DocRow[]> {
  const match = ftsQuery(query);
  if (!match) return [];
  const { sql, params } = sqlFilters(f);
  const rows = await env.DB.prepare(
    `SELECT d.path, d.title, d.type, d.project, d.date,
            snippet(fts_docs, 1, '**', '**', '…', 32) AS snip
       FROM fts_docs JOIN docs d ON d.id = fts_docs.rowid
      WHERE fts_docs MATCH ?${sql}
      ORDER BY bm25(fts_docs) LIMIT ${CANDIDATES}`,
  )
    .bind(match, ...params)
    .all<DocRow>();
  return rows.results;
}

async function semanticChunks(
  env: Env,
  vector: number[] | null,
  f: SearchFilters,
): Promise<{ path: string; chunkId: string }[]> {
  if (!vector) return [];
  const res = await env.VECTORIZE.query(vector, {
    topK: CANDIDATES,
    returnMetadata: "all",
    filter: vectorFilter("chunk", f),
  });
  return res.matches
    .filter((m) => typeof m.metadata?.path === "string")
    .map((m) => ({ path: m.metadata!.path as string, chunkId: m.id }));
}

export async function searchDocs(
  env: Env,
  query: string,
  f: SearchFilters,
  limit = 10,
): Promise<{ hits: DocHit[]; semantic: boolean }> {
  const vector = await embed(env, query, "query");
  const [kw, sem] = await Promise.all([keywordDocs(env, query, f), semanticChunks(env, vector, f)]);

  const fused = rrf([kw.map((r) => r.path), sem.map((r) => r.path)]).slice(0, limit);
  const byPath = new Map(kw.map((r) => [r.path, r]));
  const kwPaths = new Set(byPath.keys());
  const semPaths = new Set(sem.map((s) => s.path));

  // Vector-only hits need their doc row and the best chunk's text for a snippet.
  const missing = fused.map((h) => h.key).filter((p) => !byPath.has(p));
  if (missing.length) {
    const rows = await env.DB.prepare(
      `SELECT path, title, type, project, date FROM docs WHERE path IN (${missing.map(() => "?").join(",")})`,
    )
      .bind(...missing)
      .all<DocRow>();
    for (const r of rows.results) byPath.set(r.path, r);
    const bestChunk = new Map<string, string>();
    for (const s of sem) if (!bestChunk.has(s.path)) bestChunk.set(s.path, s.chunkId);
    const ids = missing.map((p) => bestChunk.get(p)).filter((x): x is string => !!x);
    if (ids.length) {
      const chunks = await env.DB.prepare(
        `SELECT doc_path, content FROM chunks WHERE id IN (${ids.map(() => "?").join(",")})`,
      )
        .bind(...ids)
        .all<{ doc_path: string; content: string }>();
      for (const c of chunks.results) {
        const row = byPath.get(c.doc_path);
        if (row) row.snip = snippet(c.content, query);
      }
    }
  }

  const hits: DocHit[] = [];
  for (const h of fused) {
    const row = byPath.get(h.key);
    if (!row) continue; // vector for a doc that was deleted since; ignore
    hits.push({
      path: row.path,
      title: row.title,
      type: row.type,
      project: row.project,
      date: row.date,
      score: Number(h.score.toFixed(5)),
      match: kwPaths.has(h.key) && semPaths.has(h.key) ? "hybrid" : kwPaths.has(h.key) ? "keyword" : "semantic",
      snippet: row.snip ?? "",
    });
  }
  return { hits, semantic: vector !== null };
}

export interface ObservationHit {
  content: string;
  doc_path: string;
  obs_type: string;
  topics: string[];
  score: number;
}

export async function searchObservations(
  env: Env,
  query: string | undefined,
  topic: string | undefined,
  f: SearchFilters,
  limit = 20,
): Promise<ObservationHit[]> {
  const db = env.DB;
  const { sql, params } = sqlFilters(f);
  const topicJoin = topic ? " JOIN observation_topics t ON t.observation_id = o.id AND t.topic_slug = ?" : "";
  const topicParams = topic ? [topic] : [];
  let ranked: string[][] = [];

  if (query) {
    const match = ftsQuery(query);
    const vector = await embed(env, query, "query");
    const [kw, sem] = await Promise.all([
      match
        ? db
            .prepare(
              `SELECT o.id FROM fts_observations
                 JOIN observations o ON o.rid = fts_observations.rowid${topicJoin}
                 LEFT JOIN docs d ON d.path = o.doc_path
                WHERE fts_observations MATCH ?${sql}
                ORDER BY bm25(fts_observations) LIMIT ${CANDIDATES}`,
            )
            .bind(...topicParams, match, ...params)
            .all<{ id: string }>()
            .then((r) => r.results.map((x) => x.id))
        : Promise.resolve([] as string[]),
      vector
        ? env.VECTORIZE.query(vector, { topK: CANDIDATES, filter: vectorFilter("obs", f) }).then((r) =>
            r.matches.map((m) => m.id),
          )
        : Promise.resolve([] as string[]),
    ]);
    ranked = [kw, sem];
  } else if (topic) {
    const rows = await db
      .prepare(
        `SELECT o.id FROM observations o${topicJoin}
           LEFT JOIN docs d ON d.path = o.doc_path
          WHERE 1 = 1${sql}
          ORDER BY o.created_at DESC LIMIT ${Math.min(limit, 200)}`,
      )
      .bind(...topicParams, ...params)
      .all<{ id: string }>();
    ranked = [rows.results.map((r) => r.id)];
  } else {
    return [];
  }

  let fused = rrf(ranked);
  if (fused.length === 0) return [];
  const ids = fused.map((h) => h.key).slice(0, 90);
  const rows = await db
    .prepare(
      `SELECT o.id, o.content, o.doc_path, o.obs_type,
              (SELECT group_concat(topic_slug) FROM observation_topics WHERE observation_id = o.id) AS topics
         FROM observations o WHERE o.id IN (${ids.map(() => "?").join(",")})`,
    )
    .bind(...ids)
    .all<{ id: string; content: string; doc_path: string; obs_type: string; topics: string | null }>();
  const byId = new Map(rows.results.map((r) => [r.id, r]));
  // A topic filter on a semantic hit is applied here: Vectorize has no topics.
  if (topic) fused = fused.filter((h) => byId.get(h.key)?.topics?.split(",").includes(topic));
  return fused
    .map((h) => {
      const r = byId.get(h.key);
      return r
        ? {
            content: r.content,
            doc_path: r.doc_path,
            obs_type: r.obs_type,
            topics: r.topics ? r.topics.split(",") : [],
            score: Number(h.score.toFixed(5)),
          }
        : null;
    })
    .filter((x): x is ObservationHit => x !== null)
    .slice(0, limit);
}
