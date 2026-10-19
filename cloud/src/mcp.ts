import { McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";
import { searchDocs, searchObservations } from "./search";
import { saveMemo } from "./memo";
import { sinceCutoff } from "./text";

export interface Caller {
  scopes: string[];
  client: string;
}

const INSTRUCTIONS = `Memex is a shared long-term memory of past work sessions: memos (what happened, decisions, tensions, open threads), project overviews (projects/<name>/_project.md), topic notes (topics/*.md) and observations (atomic facts extracted from memos).
Search it before answering questions about past work ("why did we…", "what did we decide about…"), then read the documents you cite. Cite paths.
Memos you save land in an inbox; the owner's vault imports them on its next pull. Save one when a session produced decisions or findings worth keeping, written from your own experience of the session.`;

function text(value: unknown) {
  return { content: [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] };
}

const filters = {
  project: z.string().optional().describe("Only this project (folder name under projects/)."),
  type: z.string().optional().describe("Only this document type: memo, project, topic, transcript, …"),
  since: z.string().optional().describe("Only documents dated on/after this: 7d, 2w, 3m, 1y or YYYY-MM-DD."),
};

export function buildServer(env: Env, caller: Caller): McpServer {
  const server = new McpServer({ name: "memex", version: "0.1.0" }, { instructions: INSTRUCTIONS });

  server.registerTool(
    "search",
    {
      title: "Search memex",
      description:
        "Hybrid keyword + semantic search over memos, project overviews and topic notes. Returns paths, titles, dates and snippets; use `read` for full text.",
      inputSchema: z.object({
        query: z.string().min(1).describe("Natural-language question or keywords."),
        ...filters,
        limit: z.number().int().min(1).max(50).default(10),
      }),
      annotations: { readOnlyHint: true },
    },
    async ({ query, project, type, since, limit }) => {
      const { hits, semantic } = await searchDocs(env, query, { project, type, since }, limit);
      return text({ mode: semantic ? "hybrid" : "keyword-only", results: hits });
    },
  );

  server.registerTool(
    "read",
    {
      title: "Read a memex document",
      description: "Full markdown of one document by its vault path, e.g. projects/memex/_project.md.",
      inputSchema: z.object({
        path: z.string().min(1),
        offset: z.number().int().min(0).default(0).describe("Character offset, for long documents."),
        max_chars: z.number().int().min(1000).max(200_000).default(60_000),
      }),
      annotations: { readOnlyHint: true },
    },
    async ({ path, offset, max_chars }) => {
      const row = await env.DB.prepare("SELECT content, source FROM docs WHERE path = ?")
        .bind(path.replace(/^\/+/, ""))
        .first<{ content: string; source: string }>();
      if (!row) return { ...text(`No document at ${path}. Use search or timeline to find paths.`), isError: true };
      const body = row.content.slice(offset, offset + max_chars);
      const rest = row.content.length - offset - body.length;
      const note = rest > 0 ? `\n\n[… ${rest} more characters; call read again with offset=${offset + body.length}]` : "";
      const inbox = row.source === "inbox" ? "[saved remotely; not yet pulled into the vault]\n\n" : "";
      return text(inbox + body + note);
    },
  );

  server.registerTool(
    "timeline",
    {
      title: "Browse memex by date",
      description: "Documents in a date window, newest first. Answers 'what did I work on last week?'",
      inputSchema: z.object({
        since: z.string().default("7d").describe("7d, 2w, 3m, 1y or YYYY-MM-DD."),
        before: z.string().optional().describe("YYYY-MM-DD, exclusive."),
        project: filters.project,
        type: z.string().default("memo"),
        limit: z.number().int().min(1).max(200).default(50),
      }),
      annotations: { readOnlyHint: true },
    },
    async ({ since, before, project, type, limit }) => {
      const cutoff = sinceCutoff(since) ?? "0000-00-00";
      const parts = ["date >= ?", "type = ?"];
      const params: (string | number)[] = [cutoff, type];
      if (before) {
        parts.push("date < ?");
        params.push(before);
      }
      if (project) {
        parts.push("project = ?");
        params.push(project);
      }
      const rows = await env.DB.prepare(
        `SELECT path, title, project, date FROM docs WHERE ${parts.join(" AND ")} ORDER BY date DESC, path LIMIT ?`,
      )
        .bind(...params, limit)
        .all();
      return text(rows.results);
    },
  );

  server.registerTool(
    "projects",
    {
      title: "List memex projects",
      description: "Projects with document counts and latest activity. Each project's overview is at projects/<name>/_project.md.",
      inputSchema: z.object({}),
      annotations: { readOnlyHint: true },
    },
    async () => {
      const rows = await env.DB.prepare(
        `SELECT project, count(*) AS docs, max(date) AS latest,
                max(path = 'projects/' || project || '/_project.md') AS has_overview
           FROM docs WHERE project <> '' GROUP BY project ORDER BY latest DESC`,
      ).all();
      return text(rows.results);
    },
  );

  server.registerTool(
    "observations",
    {
      title: "Search memex observations",
      description:
        "Atomic facts extracted from memos. Give a query to search them, a topic slug (a topics/<slug>.md name) to list that topic's cluster, or both.",
      inputSchema: z.object({
        query: z.string().optional(),
        topic: z.string().optional(),
        ...filters,
        limit: z.number().int().min(1).max(100).default(20),
      }),
      annotations: { readOnlyHint: true },
    },
    async ({ query, topic, project, type, since, limit }) => {
      if (!query && !topic) return { ...text("Give a query, a topic, or both."), isError: true };
      return text(await searchObservations(env, query, topic, { project, type, since }, limit));
    },
  );

  if (caller.scopes.includes("memex:write")) {
    server.registerTool(
      "save_memo",
      {
        title: "Save a memo to memex",
        description:
          "Save a session memo. It is searchable at once and lands in the owner's vault on their next `memex cloud pull`. Write from your experience of the session: what was decided and why, what was tried and failed, surprises, tensions between you and the user, open threads. Use markdown sections: For Future Context, Summary, What Happened, Perspectives & Tensions, Open Threads.",
        inputSchema: z.object({
          title: z.string().min(3).max(200).describe("Specific, searchable title."),
          project: z.string().min(1).describe("Project folder name; use `projects` to see existing ones."),
          content: z.string().min(50).max(200_000).describe("Markdown body, without frontmatter."),
          topics: z.array(z.string()).default([]).describe("Topic slugs (topics/<slug>.md names) this memo relates to."),
          date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional().describe("Defaults to today (UTC)."),
        }),
        annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false },
      },
      async (args) => {
        const saved = await saveMemo(env, { ...args, client: caller.client });
        return text({ saved: saved.path, semantic: saved.embedded, note: "In the inbox until the next `memex cloud pull`." });
      },
    );
  }

  return server;
}
