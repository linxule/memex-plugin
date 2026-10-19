import { chunkId, dateInt, observationId, stripFrontmatter } from "./text";

// Writes into D1 + Vectorize. Every write path goes through here so the
// three representations of a document (docs row, FTS row, chunk vectors)
// cannot drift apart: callers never touch fts_* or chunks directly.

export interface DocInput {
  path: string;
  title: string;
  type: string;
  project: string;
  date: string;
  content: string;
  content_hash: string;
  chunks: { index: number; content: string; vector?: number[] | null }[];
}

export interface ObservationInput {
  content: string;
  content_hash: string;
  obs_type?: string;
  confidence?: string;
  created_at?: string;
  topics?: string[];
  vector?: number[] | null;
}

type VectorMeta = Record<string, string | number>;

/** Vectorize allows 1000 vectors per upsert from a Worker. */
async function upsertVectors(env: Env, vectors: VectorizeVector[]): Promise<void> {
  if (!env.VECTORIZE) return; // keyword-only deployment
  for (let i = 0; i < vectors.length; i += 1000) {
    await env.VECTORIZE.upsert(vectors.slice(i, i + 1000));
  }
}

async function deleteVectors(env: Env, ids: string[]): Promise<void> {
  if (!env.VECTORIZE) return;
  for (let i = 0; i < ids.length; i += 1000) {
    await env.VECTORIZE.deleteByIds(ids.slice(i, i + 1000));
  }
}

/** D1 caps bound parameters at 100 per statement; keep IN lists under that. */
function batches<T>(items: T[], size = 90): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

async function chunkIdsFor(env: Env, paths: string[]): Promise<string[]> {
  const ids: string[] = [];
  for (const group of batches(paths)) {
    const rows = await env.DB.prepare(
      `SELECT id FROM chunks WHERE doc_path IN (${group.map(() => "?").join(",")})`,
    )
      .bind(...group)
      .all<{ id: string }>();
    ids.push(...rows.results.map((r) => r.id));
  }
  return ids;
}

function checkVector(env: Env, vector: number[]): void {
  const dims = Number(env.VECTOR_DIMENSIONS);
  if (vector.length !== dims) {
    throw new Error(
      `vector has ${vector.length} dimensions but the index expects ${dims}; set VECTOR_DIMENSIONS and recreate the Vectorize index, or push with matching dimensions`,
    );
  }
}

/**
 * Vector writes collected across several documents, so one sync request
 * makes a handful of Vectorize calls instead of two per document (a Worker
 * invocation has a subrequest budget).
 */
export class VectorWrites {
  upserts: VectorizeVector[] = [];
  deletes: string[] = [];

  async flush(env: Env): Promise<void> {
    const upserted = new Set(this.upserts.map((v) => v.id));
    await upsertVectors(env, this.upserts);
    await deleteVectors(env, this.deletes.filter((id) => !upserted.has(id)));
    this.upserts = [];
    this.deletes = [];
  }
}

export async function upsertDoc(
  env: Env,
  doc: DocInput,
  source: "vault" | "inbox" = "vault",
  writes?: VectorWrites,
): Promise<void> {
  const oldIds = await chunkIdsFor(env, [doc.path]);
  const db = env.DB;
  const stmts: D1PreparedStatement[] = [
    db
      .prepare(
        `INSERT INTO docs (path, title, type, project, date, content, content_hash, source, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
         ON CONFLICT(path) DO UPDATE SET
           title = excluded.title, type = excluded.type, project = excluded.project,
           date = excluded.date, content = excluded.content,
           content_hash = excluded.content_hash, source = excluded.source,
           updated_at = excluded.updated_at`,
      )
      .bind(doc.path, doc.title, doc.type, doc.project, doc.date, doc.content, doc.content_hash, source),
    db.prepare("DELETE FROM fts_docs WHERE rowid = (SELECT id FROM docs WHERE path = ?)").bind(doc.path),
    // Index the body without frontmatter, so YAML keys don't match queries.
    db
      .prepare("INSERT INTO fts_docs (rowid, title, content) SELECT id, title, ? FROM docs WHERE path = ?")
      .bind(stripFrontmatter(doc.content), doc.path),
    db.prepare("DELETE FROM chunks WHERE doc_path = ?").bind(doc.path),
  ];

  const vectors: VectorizeVector[] = [];
  const meta: VectorMeta = {
    kind: "chunk",
    path: doc.path,
    project: doc.project,
    type: doc.type,
    date: dateInt(doc.date),
  };
  for (const chunk of doc.chunks) {
    const id = await chunkId(doc.path, chunk.index);
    stmts.push(
      db
        .prepare("INSERT INTO chunks (id, doc_path, chunk_index, content) VALUES (?, ?, ?, ?)")
        .bind(id, doc.path, chunk.index, chunk.content),
    );
    if (chunk.vector) {
      checkVector(env, chunk.vector);
      vectors.push({ id, values: chunk.vector, metadata: meta });
    }
  }
  await db.batch(stmts);
  // Old vectors that were not just overwritten are stale: chunks that no
  // longer exist, or chunks that come without a vector this time.
  const sink = writes ?? new VectorWrites();
  sink.upserts.push(...vectors);
  sink.deletes.push(...oldIds);
  if (!writes) await sink.flush(env);
}

export async function deleteDocs(env: Env, paths: string[]): Promise<number> {
  if (paths.length === 0) return 0;
  const chunkIds = await chunkIdsFor(env, paths);
  const obsIds: string[] = [];
  for (const group of batches(paths)) {
    const marks = group.map(() => "?").join(",");
    const rows = await env.DB.prepare(`SELECT id FROM observations WHERE doc_path IN (${marks})`)
      .bind(...group)
      .all<{ id: string }>();
    obsIds.push(...rows.results.map((r) => r.id));
  }
  for (const path of paths) {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM fts_docs WHERE rowid = (SELECT id FROM docs WHERE path = ?)").bind(path),
      env.DB.prepare("DELETE FROM docs WHERE path = ?").bind(path),
      env.DB.prepare("DELETE FROM chunks WHERE doc_path = ?").bind(path),
      ...observationDeletes(env, path),
      env.DB.prepare("DELETE FROM obs_sets WHERE doc_path = ?").bind(path),
    ]);
  }
  await deleteVectors(env, chunkIds.concat(obsIds));
  return paths.length;
}

function observationDeletes(env: Env, docPath: string): D1PreparedStatement[] {
  const db = env.DB;
  return [
    db
      .prepare(
        "DELETE FROM observation_topics WHERE observation_id IN (SELECT id FROM observations WHERE doc_path = ?)",
      )
      .bind(docPath),
    db
      .prepare("DELETE FROM fts_observations WHERE rowid IN (SELECT rid FROM observations WHERE doc_path = ?)")
      .bind(docPath),
    db.prepare("DELETE FROM observations WHERE doc_path = ?").bind(docPath),
  ];
}

/**
 * Replace a document's observations with `items` (the complete set), the
 * same semantics as `memex backfill obs --replace` and the .obs.jsonl
 * sidecars: what is pushed for a doc is everything that doc has.
 */
export async function replaceObservations(
  env: Env,
  docPath: string,
  items: ObservationInput[],
  setHash: string,
  meta: { project: string; type: string; date: string },
  writes?: VectorWrites,
): Promise<void> {
  const db = env.DB;
  const old = await db
    .prepare("SELECT id FROM observations WHERE doc_path = ?")
    .bind(docPath)
    .all<{ id: string }>();
  const stmts: D1PreparedStatement[] = [...observationDeletes(env, docPath)];
  const vectors: VectorizeVector[] = [];
  const vmeta: VectorMeta = {
    kind: "obs",
    path: docPath,
    project: meta.project,
    type: meta.type,
    date: dateInt(meta.date),
  };
  for (const o of items) {
    const id = observationId(o.content_hash);
    stmts.push(
      db
        .prepare(
          `INSERT INTO observations (id, doc_path, content, obs_type, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET doc_path = excluded.doc_path, content = excluded.content,
             obs_type = excluded.obs_type, confidence = excluded.confidence, created_at = excluded.created_at`,
        )
        .bind(id, docPath, o.content, o.obs_type ?? "explicit", o.confidence ?? "high", o.created_at ?? ""),
      db.prepare("DELETE FROM fts_observations WHERE rowid = (SELECT rid FROM observations WHERE id = ?)").bind(id),
      db
        .prepare("INSERT INTO fts_observations (rowid, content) SELECT rid, content FROM observations WHERE id = ?")
        .bind(id),
      db.prepare("DELETE FROM observation_topics WHERE observation_id = ?").bind(id),
    );
    for (const topic of new Set(o.topics ?? [])) {
      stmts.push(
        db.prepare("INSERT OR IGNORE INTO observation_topics (observation_id, topic_slug) VALUES (?, ?)").bind(id, topic),
      );
    }
    if (o.vector) {
      checkVector(env, o.vector);
      vectors.push({ id, values: o.vector, metadata: vmeta });
    }
  }
  stmts.push(
    db
      .prepare(
        "INSERT INTO obs_sets (doc_path, set_hash) VALUES (?, ?) ON CONFLICT(doc_path) DO UPDATE SET set_hash = excluded.set_hash",
      )
      .bind(docPath, setHash),
  );
  await db.batch(stmts);
  const sink = writes ?? new VectorWrites();
  sink.upserts.push(...vectors);
  sink.deletes.push(...old.results.map((r) => r.id));
  if (!writes) await sink.flush(env);
}

export async function setMeta(env: Env, values: Record<string, string>): Promise<void> {
  await env.DB.batch(
    Object.entries(values).map(([k, v]) =>
      env.DB.prepare("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(k, v),
    ),
  );
}
