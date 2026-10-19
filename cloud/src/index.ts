import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { createMcpHandler } from "agents/mcp/server";
import { handleAuthorize, SCOPES } from "./auth";
import { buildServer } from "./mcp";
import { handleSync, isSyncRequestAuthorized } from "./sync";

// Routes:
//   /mcp                     MCP (Streamable HTTP), OAuth or SYNC_TOKEN bearer
//   /authorize               owner sign-in + consent
//   /oauth/token, /oauth/register, /.well-known/*   served by the OAuth provider
//   /api/sync/*              `memex cloud push|pull`, SYNC_TOKEN bearer
//   /health                  liveness + what is indexed

interface Props {
  user: string;
  scopes: string[];
  client?: string;
}

const mcpApi = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const props = ((ctx as ExecutionContext & { props?: Props }).props ?? { user: "owner", scopes: [] }) as Props;
    const handler = createMcpHandler(() => buildServer(env, { scopes: props.scopes, client: props.client ?? "" }), {
      route: "/mcp",
      allowedHostnames: [new URL(request.url).hostname],
    });
    return handler(request, env, ctx);
  },
};

const defaultHandler = {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (url.pathname === "/authorize") return handleAuthorize(request, env);
    if (url.pathname === "/health") return health(env);
    if (url.pathname === "/") {
      return new Response(
        `memex cloud\n\nMCP endpoint: ${url.origin}/mcp\nAdd it as a custom connector in claude.ai, or: claude mcp add --transport http memex ${url.origin}/mcp\n`,
        { headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }
    return new Response("Not found", { status: 404 });
  },
};

async function health(env: Env): Promise<Response> {
  const counts = await env.DB.prepare(
    `SELECT (SELECT count(*) FROM docs) AS docs, (SELECT count(*) FROM observations) AS observations,
            (SELECT count(*) FROM inbox WHERE pulled_at IS NULL) AS inbox_pending,
            (SELECT value FROM meta WHERE key = 'last_push') AS last_push`,
  ).first();
  return Response.json({ ok: true, ...counts, semantic_search: !!env.GEMINI_API_KEY && !!env.VECTORIZE });
}

// The provider needs the canonical URL of the MCP resource, which a
// one-click deployer doesn't know at build time (it is their workers.dev
// subdomain or custom domain). Build one provider per origin, lazily.
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string): OAuthProvider<Env> {
  let provider = providers.get(origin);
  if (!provider) {
    const resource = `${origin}/mcp`;
    provider = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: mcpApi,
      defaultHandler,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/oauth/token",
      clientRegistrationEndpoint: "/oauth/register", // DCR: deprecated in MCP 2026-07-28 but still what many clients use
      clientIdMetadataDocumentEnabled: true, // CIMD: what claude.ai prefers when offered
      scopesSupported: [...SCOPES, "offline_access"],
      requiredScopes: ["memex:read"],
      resourceMetadata: { resource, authorization_servers: [origin], resource_name: "memex" },
      refreshTokenIdleTTL: 60 * 60 * 24 * 30, // a connector in use stays signed in
      // Agents that can only send a header (scripts, other harnesses) may use
      // SYNC_TOKEN as a bearer for /mcp too. It grants read and write.
      resolveExternalToken: async ({ request, env }) =>
        (await isSyncRequestAuthorized(request, env))
          ? { props: { user: "owner", scopes: SCOPES, client: "token" } satisfies Props, audience: resource }
          : null,
      onError: ({ code, description, status }) => {
        console.warn("oauth error", status, code, description);
      },
    });
    providers.set(origin, provider);
  }
  return provider;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/sync/")) return handleSync(request, env);
    const origin = env.PUBLIC_URL ? new URL(env.PUBLIC_URL).origin : url.origin;
    return providerFor(origin).fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
