import { embed } from "./embed";
import { replaceObservations, upsertDoc } from "./store";
import { isSafeSegment, sha256Hex, slugify } from "./text";

export interface MemoObservation {
  content: string;
  obs_type: string;
  confidence: string;
  topics: string[];
}

export interface MemoInput {
  title: string;
  project: string;
  content: string;
  topics: string[];
  observations?: MemoObservation[];
  date?: string;
  client: string;
}

/**
 * Keep only slugs that name an existing topic note (topics/<slug>.md in the
 * mirror). The local index accepts any string, and an unknown slug becomes an
 * orphan row that breaks topic clustering.
 */
async function knownTopics(env: Env, slugs: string[]): Promise<Set<string>> {
  const candidates = [...new Set(slugs.filter((t) => /^[a-z0-9][a-z0-9-]*$/.test(t)))];
  if (candidates.length === 0) return new Set();
  const rows = await env.DB.prepare(
    `SELECT path FROM docs WHERE path IN (${candidates.map(() => "?").join(",")})`,
  )
    .bind(...candidates.map((t) => `topics/${t}.md`))
    .all<{ path: string }>();
  return new Set(rows.results.map((r) => r.path.slice("topics/".length, -".md".length)));
}

function yamlString(value: string): string {
  return JSON.stringify(value); // JSON strings are valid YAML double-quoted scalars
}

/**
 * Write a memo into the inbox and make it searchable now. The file it will
 * become in the vault is rendered here, frontmatter included, so `memex cloud
 * pull` writes these bytes verbatim and the next push replaces this row with
 * the vault's copy (same path, so no duplicate).
 */
function droppedTopics(input: MemoInput, known: Set<string>): string[] {
  const all = [...input.topics, ...(input.observations ?? []).flatMap((o) => o.topics)];
  return [...new Set(all.filter((t) => !known.has(t)))].sort();
}

export async function saveMemo(
  env: Env,
  input: MemoInput,
): Promise<{ path: string; embedded: boolean; observations: number; dropped_topics: string[] }> {
  if (!isSafeSegment(input.project)) throw new Error(`invalid project name: ${input.project}`);
  const observations = input.observations ?? [];
  const known = await knownTopics(env, [...input.topics, ...observations.flatMap((o) => o.topics)]);
  const topics = input.topics.filter((t) => known.has(t));
  const date = input.date ?? new Date().toISOString().slice(0, 10);
  const base = `projects/${input.project}/memos/${date}-${slugify(input.title)}`;

  let path = `${base}.md`;
  for (let n = 2; ; n++) {
    const taken = await env.DB.prepare("SELECT 1 FROM docs WHERE path = ? UNION SELECT 1 FROM inbox WHERE path = ?")
      .bind(path, path)
      .first();
    if (!taken) break;
    path = `${base}-${n}.md`;
  }

  const body = input.content.trimStart().startsWith("# ") ? input.content.trim() : `# ${input.title}\n\n${input.content.trim()}`;
  const front = [
    "---",
    "type: memo",
    `title: ${yamlString(input.title)}`,
    `project: ${input.project}`,
    `date: ${date}`,
    ...(topics.length ? ["topics:", ...topics.map((t) => `  - ${t}`)] : []),
    "manual: true",
    "source: memex-cloud",
    ...(input.client ? [`client: ${yamlString(input.client)}`] : []),
    "---",
    "",
    "",
  ].join("\n");
  const content = `${front}${body}\n`;

  const createdAt = new Date().toISOString().replace("T", " ").slice(0, 19);
  const obs = [];
  for (const o of observations) {
    const text = o.content.trim();
    if (!text) continue;
    obs.push({
      content: text,
      content_hash: await sha256Hex(text),
      obs_type: o.obs_type,
      confidence: o.confidence,
      topics: [...new Set(o.topics.filter((t) => known.has(t)))].sort(),
      created_at: createdAt,
    });
  }

  await env.DB.prepare("INSERT INTO inbox (path, content, client, observations) VALUES (?, ?, ?, ?)")
    .bind(path, content, input.client, JSON.stringify(obs))
    .run();

  // Embed as one chunk the way chunk_whole_doc does for short memos; longer
  // memos stay keyword-searchable until the vault re-pushes them chunked.
  const chunkText = `[Project: ${input.project} | Title: ${input.title}]\n\n${content}`;
  const vector = chunkText.length <= 7000 ? await embed(env, chunkText, "document") : null;
  await upsertDoc(
    env,
    {
      path,
      title: input.title,
      type: "memo",
      project: input.project,
      date,
      content,
      content_hash: await sha256Hex(content),
      chunks: [{ index: 0, content: chunkText, vector }],
    },
    "inbox",
  );
  // Keyword-searchable now; vectors arrive when the vault re-pushes the
  // ingested sidecar. The "inbox" set hash never matches a local one, so that
  // push replaces this set.
  if (obs.length) await replaceObservations(env, path, obs, "inbox", { project: input.project, type: "memo", date });
  return { path, embedded: vector !== null, observations: obs.length, dropped_topics: droppedTopics(input, known) };
}
