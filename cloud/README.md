# memex cloud

Memex cloud runs your memex as a remote MCP server on Cloudflare. Agents that do not run on your machine can then search it, read it and save memos to it. These agents include claude.ai connectors, Claude Projects, Claude Code on the web, other harnesses and their subagents.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/linxule/memex-plugin/tree/main/cloud)

## How it fits

The vault on your machine stays the source of truth. The cloud holds a mirror of it, plus an inbox:

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

### Push reuses your vectors

The CLI sends the embeddings your local index already has, so nothing is embedded again.

If you set `GEMINI_API_KEY` on the Worker, the Worker embeds queries with the same model your push recorded. Search is then hybrid, exactly like `memex search` (BM25 and vectors, RRF k=60).

Without `GEMINI_API_KEY`, search is keyword-only. Local search works the same way without a key.

### Transcripts stay on your machine

Transcripts stay on your machine unless you push with `--include-transcripts`.

### Remote memos are inbox items

The `save_memo` tool renders a normal memo file, including frontmatter. The memo is searchable immediately. It becomes a file in your vault on the next `memex cloud pull`.

The agent can send the memo's observations with it, extracted by the same rules `/memex:save` uses. The pull writes them as the memo's `.obs.jsonl` sidecar, and your next index rebuild adds and embeds them. Your machine never calls a model to import a memo.

Nothing remote ever edits an existing vault file.

Vectorize applies writes asynchronously. A remote memo therefore matches keyword queries at once, and semantic queries 1 or 2 minutes later.

### Backups

You do not need to back up D1 or Vectorize. You can rebuild everything in them with `memex cloud push --full`. The exception is inbox memos you have not pulled yet.

## Tools

The server offers these tools:

| Tool | What it does |
|------|--------------|
| `search` | Hybrid search over memos, project overviews and topics, with project, type and since filters |
| `read` | Full markdown of a document by path, in pages for long documents |
| `timeline` | Documents in a date window, newest first |
| `projects` | Projects with counts and latest activity |
| `observations` | Search atomic observations, or list a topic's cluster |
| `save_memo` | Save a memo to the inbox. Needs the `memex:write` scope |

## Deploy

### One click

Select the **Deploy to Cloudflare** button above. Cloudflare copies this folder into a new repo in your GitHub account. It also creates the D1 database, KV namespace and Vectorize index. It then asks for a few values:

| Field | What to put |
|-------|-------------|
| Vectorize **Dimensions** and **Metric** | Enter `768` and `cosine`. These must match the vectors that `memex cloud push` sends. The form cannot pre-fill them. If you entered something else, the deploy recreates the index while it is still empty. |
| `OWNER_PASSWORD` | The password you type when a client asks you to sign in. Use 16 or more random characters. |
| `SYNC_TOKEN` | Bearer token for the CLI. Generate a token with `openssl rand -hex 32`. |
| `CONSENT_SECRET` | Signs the "remember this app" cookies. Generate a secret with `openssl rand -hex 32`. |

Set the rest of the form as follows.

If you already have a memex deployment in this account, choose **+ Create new** for D1 and for Vectorize. The form preselects an existing resource with the same name. Rename the KV namespace too. Otherwise the new Worker shares the old Worker's data.

Leave `VECTOR_DIMENSIONS` at 768 unless your local index stores another size. If it does, enter that size in `VECTOR_DIMENSIONS` and in the Vectorize **Dimensions** field. The 2 must match.

Leave the build command empty. Keep the deploy command as `npm run deploy`, which the form pre-fills. This command creates the Vectorize metadata indexes and applies the D1 migrations.

Turn off **Enable Preview builds**. Previews share the production database, KV and sign-ins. A single-owner server gains nothing from them.

Every secret on the form is required, so you add the optional secret afterwards. To turn on semantic search, add `GEMINI_API_KEY` under your Worker's **Settings → Variables and Secrets**. You can also run `npx wrangler secret put GEMINI_API_KEY`. Without it, search is keyword-only.

You need the Workers Paid plan for an index of any real size, because the Vectorize and D1 free allowances are small.

### From a checkout

Run these commands in a checkout of this repo:

```bash
cd cloud
npm install
npx wrangler login
npx wrangler secret put OWNER_PASSWORD   # and SYNC_TOKEN, CONSENT_SECRET, GEMINI_API_KEY
npm run deploy    # creates the Vectorize index + metadata indexes, deploys, applies D1 migrations
```

Set `VECTOR_DIMENSIONS` in `wrangler.jsonc` to match the vectors your local index stores. Use 768 if you set `embeddings.index_dimensions: 768`, which is the recommended setting. Otherwise, set it to 1536 or less.

Vectorize's maximum is 1536. You must therefore truncate an untruncated 3072d index locally first, with `memex index migrate-vec`. This command does not re-embed anything.

If you use a custom domain, add `"PUBLIC_URL": "https://your.domain"` to `vars`.

### Keyword-only deployment

To run the server on D1 alone, delete the `vectorize` block from `wrangler.jsonc`. Also drop `node scripts/setup-vectorize.mjs &&` from the deploy script.

With those changes:

- search uses BM25 only
- pushes skip vectors
- `/health` reports `semantic_search: false`

Later, add the block back and run `memex cloud push --full` to fill the index.

## Updating

A deployment made with the button is a copy. Cloudflare put this folder into a repo of your own, with no link back to this repo. Cloudflare redeploys from your repo.

Your copy stays current in 2 ways: the update workflow and notices from the `memex` CLI.

### Update workflow

Your repo includes `.github/workflows/update-memex-cloud.yml`. Every Monday, or when you run it from the Actions tab, the workflow looks for a newer `cloud-v*` release. It opens a pull request with the release.

Merging the pull request redeploys the Worker. The deploy script applies any new D1 migrations.

For the pull request to open, enable **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests**. Otherwise the run summary links to the branch.

The workflow leaves these 3 things alone:

- your `wrangler.jsonc` (resource names and IDs)
- your package name
- the workflow files

When a release changes one of them, the pull request shows the change for you to apply by hand.

### Notices from the memex CLI

The commands `memex cloud status`, `push` and `pull` compare the server's version with the version the CLI was released with. An older server gets a note. A server too old to work with this CLI stops `push` and `pull` until you update it.

The commands also say when the server's database is behind its code, for example after a deploy whose migrations did not run.

### Update by hand

To update by hand from a clone of your repo, run `node scripts/update-from-upstream.mjs`. Add `--check` to see what would change. Then commit and push.

To update from a checkout of this repo instead, run `git pull` and `npm run deploy`.

### Release notes

The release notes for each `cloud-v*` tag say whether the server update is required, recommended or not needed.

## Connect your machine

Run these commands on your machine:

```bash
memex cloud login --url https://memex-cloud.<you>.workers.dev   # paste SYNC_TOKEN when asked
memex cloud push --dry-run
memex cloud push           # first push sends everything; later pushes send what changed
memex cloud status
memex cloud pull           # bring remote memos home, then rebuild + push
```

To keep both sides current, run `memex index rebuild --incremental && memex cloud pull && memex cloud push` from a cron or launchd entry.

## Connect agents

Every client connects to your Worker's `/mcp` endpoint.

### claude.ai, Claude Desktop and Claude Projects

1. Open **Settings**, then **Connectors**.
2. Select **Add custom connector**.
3. Enter the URL `https://<your-worker>/mcp`.
4. When the client sends you to the sign-in page, enter `OWNER_PASSWORD`.
5. Select what the app may do.

### Claude Code

Run `claude mcp add --transport http memex https://<your-worker>/mcp`. Then sign in through `/mcp`.

### Clients that can only send a header

If a client can only send a header, send `Authorization: Bearer <SYNC_TOKEN>`. This works on `/mcp` too and grants read and write. Treat the token like a password.

## Security notes

The server has a single owner. The OAuth server (`@cloudflare/workers-oauth-provider`) handles:

- dynamic client registration
- Client ID Metadata Documents
- PKCE
- rotating refresh tokens

The consent page shows which app is asking and where tokens go. It also requires the owner password.

A wrong password ends that authorization attempt after a one-second delay. There is no lockout, so the length of the password is what protects you. Use a random password.

Workers KV caches reads for up to 60 seconds. A refresh token used within a minute of being issued can therefore be refused (`invalid_grant`) while the edge still holds the previous grant. Clients refresh when the hour-long access token expires, so this only shows up in scripted tests. Sign-in itself was not affected in testing.

`SYNC_TOKEN` grants full read and write access. To rotate it, run `wrangler secret put SYNC_TOKEN`. Then log in again with `memex cloud login`.

Anyone who can sign in can read everything you push. Keep transcripts out unless you want to share them with your agents.

## Known limitations

### Same-path race

A race can happen when an agent saves a memo at the same moment a push sends a vault memo. This applies when both memos have the same date and title slug. The inbox memo can then replace the cloud copy of the vault memo.

Your vault file is never touched. The next `memex cloud pull` reports the conflict, and `memex cloud push --full` restores the cloud copy.

### Orphaned vectors after an interrupted push

Vectors are written before the database, so a push that fails partway is simply sent again. Vectors become orphaned in one case. The failed push had added a brand-new document, and you delete that document locally before any push succeeds. The vectors for that document then stay in Vectorize.

Search ignores vectors whose document is gone, so orphaned vectors cost storage, not results.

### The update workflow cannot update itself

GitHub does not let a workflow's token change workflow files. The updater skips them on purpose.

When a release changes `update-memex-cloud.yml` or `wrangler.jsonc`, the update pull request shows the change. You apply the change by hand.

### Refresh within a minute

A refresh token used right after it was issued can be refused. Workers KV caches reads for up to 60 seconds. Clients refresh hourly, so this shows up only in scripted tests. See [Security notes](#security-notes).

## Development

Run these commands to work on the server locally:

```bash
npm install
cp .dev.vars.example .dev.vars        # fill in
npx wrangler d1 migrations apply DB --local
npm run dev                           # http://localhost:8787 (Vectorize is remote-only; keyword search works locally)
npm run typecheck && npm test
```

The server uses the stateless `createMcpHandler` from the Agents SDK and MCP SDK v2 (`@modelcontextprotocol/server`). It needs no Durable Objects.

Local tests used the SDK v2 client and raw HTTP. Both negotiated protocol 2025-11-25. Tests on a deployed server completed 28 of 28 live sign-ins. Refresh succeeded every time once the grant was more than 60 seconds old.
