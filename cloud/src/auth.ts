import { AuthorizationError, CimdFetchError, type ConsentDescription } from "@cloudflare/workers-oauth-provider";
import { escapeHtml, safeEqual } from "./text";

// The OAuth authorization page. Memex cloud is single-owner, so "signing in"
// is typing OWNER_PASSWORD on the consent page: no third-party identity
// provider for a one-click deployer to set up first.

export const SCOPES = ["memex:read", "memex:write"];
const OWNER = "owner";

function page(details: ConsentDescription, handle: string, error?: string): string {
  const name = escapeHtml(details.clientName);
  const origin = details.clientDomain
    ? `Published by <strong>${escapeHtml(details.clientDomain)}</strong>.`
    : "This app registered itself; its name is not verified.";
  const requested = new Set(details.scope.length ? details.scope : SCOPES);
  const labels: Record<string, string> = {
    "memex:read": "Search and read your memex",
    "memex:write": "Save new memos to your memex inbox",
  };
  const scopes = SCOPES.map(
    (s) =>
      `<label><input type="checkbox" name="scope" value="${s}"${requested.has(s) ? " checked" : ""}> ${labels[s]}</label>`,
  ).join("");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect ${name} to memex</title>
<style>
  :root { color-scheme: light dark; --bg: #faf9f6; --fg: #1f1d1a; --muted: #6b665e; --line: #ddd8cf; --accent: #7a4b2a; --warn: #9a3b1b; }
  @media (prefers-color-scheme: dark) { :root { --bg: #1b1a18; --fg: #ece8e1; --muted: #a39d93; --line: #3a3732; --accent: #d9a273; --warn: #f08a63; } }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; }
  main { max-width: 26rem; margin: 10vh auto; padding: 0 16px; }
  h1 { font-size: 1.35rem; margin: 0 0 .5rem; }
  p { color: var(--muted); margin: .5rem 0; }
  strong { color: var(--fg); }
  .warn { color: var(--warn); }
  fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 1.25rem 0; padding: .75rem 1rem; }
  label { display: block; margin: .35rem 0; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: .6rem; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--fg); font: inherit; }
  .row { display: flex; gap: .5rem; margin-top: 1rem; }
  button { flex: 1; padding: .6rem; border-radius: 6px; border: 1px solid var(--line); background: transparent; color: var(--fg); font: inherit; cursor: pointer; }
  button[value=approve] { background: var(--accent); border-color: var(--accent); color: var(--bg); }
</style></head>
<body><main>
<h1>Connect ${name} to your memex?</h1>
<p>${origin} Access will be sent to <strong>${escapeHtml(details.redirectHost)}</strong>.</p>
${details.redirectIsLoopback ? '<p class="warn">This sends access to an app on your computer. Continue only if you just started connecting from it.</p>' : ""}
<form method="post">
  <input type="hidden" name="handle" value="${escapeHtml(handle)}">
  <fieldset>${scopes}</fieldset>
  <label for="password">Owner password</label>
  <input id="password" type="password" name="password" autocomplete="current-password" autofocus>
  ${error ? `<p class="warn">${escapeHtml(error)}</p>` : ""}
  <div class="row"><button name="decision" value="deny">Deny</button><button name="decision" value="approve">Allow</button></div>
</form>
</main></body></html>`;
}

function html(body: string, headers: Headers, status = 200): Response {
  headers.set("Content-Type", "text/html; charset=utf-8");
  return new Response(body, { status, headers });
}

/** A name for memo provenance (`client:` in frontmatter): the registered app name, else its client id. */
async function clientLabel(oauth: Env["OAUTH_PROVIDER"], clientId: string): Promise<string> {
  try {
    return (await oauth.lookupClient(clientId))?.clientName || clientId;
  } catch {
    return clientId;
  }
}

function plain(message: string, status = 400): Response {
  return new Response(message, { status, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

export async function handleAuthorize(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  try {
    if (request.method === "GET") {
      const authRequest = await oauth.parseAuthRequest(request);
      const remember = { secret: env.CONSENT_SECRET, subject: OWNER };
      if (env.CONSENT_SECRET && (await oauth.isConsentRemembered(request, authRequest, remember))) {
        const scope = authRequest.scope.filter((s: string) => SCOPES.includes(s));
        const { redirectTo } = await oauth.completeAuthorization({
          request: authRequest,
          userId: OWNER,
          metadata: {},
          scope,
          props: { user: OWNER, scopes: scope, client: await clientLabel(oauth, authRequest.clientId) },
        });
        return Response.redirect(redirectTo, 302);
      }
      const details = await oauth.describeConsent(authRequest); // before beginConsent: a failed lookup leaves nothing in KV
      const consent = await oauth.beginConsent(authRequest);
      return html(page(details, consent.handle), consent.headers);
    }

    if (request.method === "POST") {
      const form = await request.formData();
      const handle = String(form.get("handle") ?? "");
      if (form.get("decision") !== "approve") {
        const denied = await oauth.denyConsent(request, handle);
        return new Response(null, { status: 302, headers: denied.headers });
      }
      if (!env.OWNER_PASSWORD || env.OWNER_PASSWORD.length < 16) {
        return plain("Sign-in is disabled until OWNER_PASSWORD is set to at least 16 characters.", 503);
      }
      if (!(await safeEqual(String(form.get("password") ?? ""), env.OWNER_PASSWORD))) {
        // The handle is single-use, so a wrong password restarts the flow from the client.
        await new Promise((r) => setTimeout(r, 1000));
        const denied = await oauth.denyConsent(request, handle, { description: "Wrong owner password" });
        return new Response(null, { status: 302, headers: denied.headers });
      }
      const scope = form.getAll("scope").map(String).filter((s) => SCOPES.includes(s));
      const remember =
        env.CONSENT_SECRET && env.CONSENT_SECRET.length >= 32 ? { secret: env.CONSENT_SECRET, subject: OWNER } : undefined;
      const approved = await oauth.approveConsent(request, handle, { scope, remember });
      const { redirectTo } = await oauth.completeAuthorization({
        request: approved.request,
        userId: OWNER,
        metadata: {},
        scope: approved.request.scope,
        props: {
          user: OWNER,
          scopes: approved.request.scope,
          client: await clientLabel(oauth, approved.request.clientId),
        },
      });
      approved.headers.set("Location", redirectTo);
      return new Response(null, { status: 302, headers: approved.headers });
    }

    return plain("Method not allowed", 405);
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
    if (error instanceof AuthorizationError) return plain(error.description ?? "Authorization failed. Start again from your app.");
    if (error instanceof CimdFetchError) return plain("This app could not be verified.");
    throw error;
  }
}
