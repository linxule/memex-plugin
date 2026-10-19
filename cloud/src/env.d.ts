import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

// Secrets (set by the Deploy button prompt, `wrangler secret put`, or .dev.vars)
// and the helper the OAuth provider injects. Bindings and vars come from
// `wrangler types` (worker-configuration.d.ts).
interface MemexSecrets {
  OWNER_PASSWORD: string;
  SYNC_TOKEN: string;
  CONSENT_SECRET: string;
  GEMINI_API_KEY?: string;
  OAUTH_PROVIDER: OAuthHelpers;
}

declare global {
  interface Env extends MemexSecrets {}
  namespace Cloudflare {
    interface Env extends MemexSecrets {}
  }
}

export {};
