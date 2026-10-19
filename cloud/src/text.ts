// Pure helpers shared by search, sync and tests. No bindings here.

// Mirrors STOP_WORDS in memex/scripts/hybrid_search.py so keyword search
// behaves the same in the cloud as it does locally.
const STOP_WORDS = new Set([
  "why", "what", "how", "when", "where", "who", "which",
  "did", "do", "does", "is", "are", "was", "were", "have", "has", "had",
  "can", "could", "would", "should", "will", "shall", "be", "been", "being",
  "get", "got", "make", "made", "go", "went",
  "we", "you", "i", "they", "he", "she", "it", "me", "us", "them",
  "the", "a", "an", "of", "to", "for", "with", "on", "at", "by", "from",
  "in", "out", "about", "into", "through", "during", "before", "after",
  "that", "this", "these", "those", "there", "here",
]);

/**
 * Natural-language query -> FTS5 MATCH expression, as `extract_fts_keywords`
 * does locally: drop punctuation and stop words, keep at most 7 terms, OR them.
 * Each term is quoted so FTS5 never parses user text as query syntax.
 */
export function ftsQuery(query: string): string {
  const words = query
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_\s]/gu, " ")
    .split(/\s+/)
    .filter((w) => /[\p{L}\p{N}]/u.test(w));
  let keywords = words.filter((w) => !STOP_WORDS.has(w) && w.length > 1);
  if (keywords.length === 0) keywords = words.slice(0, 5);
  return keywords
    .slice(0, 7)
    .map((w) => `"${w.replace(/"/g, '""')}"`)
    .join(" OR ");
}

export interface Ranked {
  key: string;
}

/**
 * Reciprocal rank fusion, k=60, over already-ordered result lists (best first).
 * Returns keys with fused scores, best first. Same formula as the local
 * `reciprocal_rank_fusion`.
 */
export function rrf(lists: string[][], k = 60): { key: string; score: number; hits: number }[] {
  const scores = new Map<string, { score: number; hits: number }>();
  for (const list of lists) {
    const seen = new Set<string>();
    let rank = 0;
    for (const key of list) {
      if (seen.has(key)) continue; // a doc counts once per list, at its best rank
      seen.add(key);
      const entry = scores.get(key) ?? { score: 0, hits: 0 };
      entry.score += 1 / (k + rank + 1);
      entry.hits += 1;
      scores.set(key, entry);
      rank += 1;
    }
  }
  return [...scores.entries()]
    .map(([key, v]) => ({ key, ...v }))
    .sort((a, b) => b.score - a.score);
}

/**
 * Matryoshka truncation + L2 renormalisation, as `truncate_unit_vector` does
 * locally. Gemini Embedding 2 is MRL-trained, so the first N dims of the
 * native vector are a usable embedding once renormalised.
 */
export function truncateUnit(vector: number[], dims: number): number[] {
  const head = dims > 0 && dims < vector.length ? vector.slice(0, dims) : vector.slice();
  const norm = Math.sqrt(head.reduce((s, x) => s + x * x, 0));
  return norm > 0 ? head.map((x) => x / norm) : head;
}

/** 'YYYY-MM-DD...' -> YYYYMMDD integer for Vectorize range filters; 0 when unknown. */
export function dateInt(value: string | null | undefined): number {
  const m = /(\d{4})-(\d{2})-(\d{2})/.exec(value ?? "");
  return m ? Number(`${m[1]}${m[2]}${m[3]}`) : 0;
}

/** '7d' | '2w' | '3m' | '1y' | 'YYYY-MM-DD' -> 'YYYY-MM-DD' cutoff, or null. */
export function sinceCutoff(since: string | undefined, now = new Date()): string | null {
  if (!since) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(since)) return since;
  const m = /^(\d+)\s*([dwmy])$/i.exec(since.trim());
  if (!m) return null;
  const n = Number(m[1]);
  const days = { d: 1, w: 7, m: 30, y: 365 }[m[2]!.toLowerCase() as "d" | "w" | "m" | "y"];
  const cutoff = new Date(now.getTime() - n * days * 86_400_000);
  return cutoff.toISOString().slice(0, 10);
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Vectorize ids are capped at 64 bytes, so chunk ids hash the path. */
export async function chunkId(path: string, index: number): Promise<string> {
  return `c:${(await sha256Hex(path)).slice(0, 40)}:${index}`;
}

export function observationId(contentHash: string): string {
  return `o:${contentHash.slice(0, 60)}`;
}

export { sha256Hex };

/** Constant-time string comparison via digests (lengths may differ). */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [da, db] = await Promise.all([sha256Hex(a), sha256Hex(b)]);
  let diff = 0;
  for (let i = 0; i < da.length; i++) diff |= da.charCodeAt(i) ^ db.charCodeAt(i);
  return diff === 0 && a.length > 0;
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function stripFrontmatter(content: string): string {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
}

/** Short plain-text snippet around the first query term, for result lists. */
export function snippet(content: string, query: string, max = 240): string {
  const body = stripFrontmatter(content).replace(/\s+/g, " ").trim();
  const terms = ftsQuery(query)
    .split(" OR ")
    .map((t) => t.replace(/"/g, ""))
    .filter(Boolean);
  const lower = body.toLowerCase();
  const at = terms.map((t) => lower.indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0] ?? 0;
  const start = Math.max(0, at - Math.floor(max / 3));
  const out = body.slice(start, start + max);
  return (start > 0 ? "…" : "") + out + (start + max < body.length ? "…" : "");
}

/** Lowercase slug for memo filenames, as memo-writing does: 2026-10-19-some-title.md */
export function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^\p{L}\p{N}\s-]/gu, "")
      .trim()
      .replace(/[\s-]+/g, "-")
      .slice(0, 80)
      .replace(/-+$/, "") || "memo"
  );
}

/** A project name is one safe path segment. */
export function isSafeSegment(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(value) && !value.includes("..");
}
