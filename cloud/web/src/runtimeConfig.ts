/**
 * Settings the server may supply at run time through /runtime-config.js (the self-hosted
 * Windows install serves one prebuilt dashboard for every installation), falling back to the
 * values baked in at build time (Cloudflare Pages, local development). Public values only:
 * the auth origin and its anon/publishable key, never a secret.
 */
type Key = "VITE_API_URL" | "VITE_DEPLOY_ENV" | "VITE_AUTH_MODE" | "VITE_SUPABASE_URL" | "VITE_SUPABASE_ANON_KEY";

declare global {
  interface Window {
    __SANA_CONFIG__?: Partial<Record<Key, string>>;
  }
}

const BUILT: Record<Key, string | undefined> = {
  VITE_API_URL: import.meta.env.VITE_API_URL,
  VITE_DEPLOY_ENV: import.meta.env.VITE_DEPLOY_ENV,
  VITE_AUTH_MODE: import.meta.env.VITE_AUTH_MODE,
  VITE_SUPABASE_URL: import.meta.env.VITE_SUPABASE_URL,
  VITE_SUPABASE_ANON_KEY: import.meta.env.VITE_SUPABASE_ANON_KEY,
};

export function runtimeEnv(key: Key): string | undefined {
  const served = typeof window !== "undefined" ? window.__SANA_CONFIG__?.[key] : undefined;
  return served !== undefined && served !== "" ? served : BUILT[key];
}
