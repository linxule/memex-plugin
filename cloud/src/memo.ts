import { embed } from "./embed";
import { upsertDoc } from "./store";
import { isSafeSegment, sha256Hex, slugify } from "./text";

export interface MemoInput {
  title: string;
  project: string;
  content: string;
  topics: string[];
  date?: string;
  client: string;
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
export async function saveMemo(env: Env, input: MemoInput): Promise<{ path: string; embedded: boolean }> {
  if (!isSafeSegment(input.project)) throw new Error(`invalid project name: ${input.project}`);
  const topics = input.topics.filter((t) => /^[a-z0-9][a-z0-9-]*$/.test(t));
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

  await env.DB.prepare("INSERT INTO inbox (path, content, client) VALUES (?, ?, ?)")
    .bind(path, content, input.client)
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
  return { path, embedded: vector !== null };
}
