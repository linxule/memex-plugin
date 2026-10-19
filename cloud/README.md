# memex cloud

Your memex as a remote MCP server on Cloudflare, so agents that don't run on your
machine (claude.ai connectors, Claude Projects, Claude Code on the web, other
harnesses, their subagents) can search it, read it and save memos to it.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/linxule/memex-plugin/tree/main/cloud)

## How it fits

The vault on your machine stays the source of truth. The cloud is a **mirror**
of it plus an **inbox**:

```
 your machine                                   Cloudflare (your account)
 ────────────                                   ─────────────────────────
 vault (markdown, .obs.jsonl)                   Worker  /mcp  ← claude.ai, Claude Code, agents
   │ memex index rebuild                          │  search · read · timeline · projects
 ~/.memex/_index.sqlite ── memex cloud push ──▶   │  observations · save_memo
   (docs, chunks, observations, vectors)          ├─ D1: docs, chunks, observations, FTS5
                                                  ├─ Vectorize: chunk + observation vectors
 vault ◀────────────── memex cloud pull ───────── └─ inbox: memos saved by remote agents
```

- **Push reuses your vectors.** The CLI sends the embeddings your local index
  already has, so nothing is re-embedded. With `GEMINI_API_KEY` set on the
  Worker, queries are embedded with the same model your push recorded, and
  search is hybrid (BM25 + vectors, RRF k=60) exactly like `memex search`.
  Without it, search is keyword-only, as it is locally without a key.
- **Transcripts stay home** unless you push with `--include-transcripts`.
- **Remote memos are inbox items.** `save_memo` renders a normal memo file
  (frontmatter included). It is searchable immediately and becomes a file in
  your vault on the next `memex cloud pull`. Nothing remote ever edits an
  existing vault file.
- **Nothing to back up.** Everything in D1 and Vectorize can be rebuilt with
  `memex cloud push --full`, except inbox memos you haven't pulled yet.

## Tools

| Tool | What it does |
|------|--------------|
| `search` | Hybrid search over memos, project overviews and topics, with project / type / since filters |
| `read` | Full markdown of a document by path (paged for long ones) |
| `timeline` | Documents in a date window, newest first |
| `projects` | Projects with counts and latest activity |
| `observations` | Search atomic observations, or list a topic's cluster |
| `save_memo` | Save a memo to the inbox (needs the `memex:write` scope) |

## Deploy

### One click

Press the button above. Cloudflare copies this folder into a new repo in your
GitHub account, creates the D1 database, KV namespace and Vectorize index, and
asks for the secrets:

| Secret | What to put |
|--------|-------------|
| `OWNER_PASSWORD` | What you type when a client asks you to sign in. Use 16+ random characters. |
| `SYNC_TOKEN` | Bearer token for the CLI: `openssl rand -hex 32` |
| `CONSENT_SECRET` | Signs "remember this app" cookies: `openssl rand -hex 32` |
| `GEMINI_API_KEY` | Optional. Enables semantic search for queries. |

You need the Workers Paid plan for an index of any real size (Vectorize and D1
free allowances are small).

### From a checkout

```bash
cd cloud
npm install
npx wrangler login
npx wrangler secret put OWNER_PASSWORD   # and SYNC_TOKEN, CONSENT_SECRET, GEMINI_API_KEY
npm run deploy    # creates the Vectorize index + metadata indexes, deploys, applies D1 migrations
```

`VECTOR_DIMENSIONS` in `wrangler.jsonc` must match the vectors your local index
stores: 768 if you set `embeddings.index_dimensions: 768` (recommended), or set
it to 1536 or less otherwise. Vectorize's maximum is 1536, so an untruncated
3072d index has to be truncated locally first (`memex index migrate-vec`, no
re-embedding). On a custom domain, set `PUBLIC_URL` too.

## Connect your machine

```bash
memex cloud login --url https://memex-cloud.<you>.workers.dev   # paste SYNC_TOKEN when asked
memex cloud push --dry-run
memex cloud push           # first push sends everything; later pushes send what changed
memex cloud status
memex cloud pull           # bring remote memos home, then rebuild + push
```

A cron or launchd entry running `memex index rebuild --incremental && memex
cloud pull && memex cloud push` keeps both sides current.

## Connect agents

- **claude.ai / Claude Desktop / Claude Projects:** Settings → Connectors → Add
  custom connector, URL `https://<your-worker>/mcp`. You will be sent to the
  sign-in page; enter `OWNER_PASSWORD` and tick what the app may do.
- **Claude Code:** `claude mcp add --transport http memex https://<your-worker>/mcp`,
  then sign in through `/mcp`.
- **Anything that can only send a header:** `Authorization: Bearer <SYNC_TOKEN>`
  works on `/mcp` too and grants read and write. Treat it like a password.

## Security notes

- Single owner. The OAuth server (`@cloudflare/workers-oauth-provider`) handles
  dynamic client registration, Client ID Metadata Documents, PKCE and rotating
  refresh tokens; the consent page shows which app is asking and where tokens
  go, and requires the owner password.
- A wrong password ends that authorization attempt after a one-second delay.
  There is no lockout, so the password's length is the defence: use a random one.
- `SYNC_TOKEN` grants full read and write. Rotate it with
  `wrangler secret put SYNC_TOKEN` and `memex cloud login` again.
- Everything you push is readable by anyone who can sign in. Keep transcripts
  out unless you mean to share them with your agents.

## Development

```bash
npm install
cp .dev.vars.example .dev.vars        # fill in
npx wrangler d1 migrations apply DB --local
npm run dev                           # http://localhost:8787 (Vectorize is remote-only; keyword search works locally)
npm run typecheck && npm test
```

Built on the stateless `createMcpHandler` from the Agents SDK and MCP SDK v2
(`@modelcontextprotocol/server`). No Durable Objects are needed. Tested locally
with the SDK v2 client and raw HTTP, both negotiating protocol 2025-11-25.
