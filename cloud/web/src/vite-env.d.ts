/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_DEPLOY_ENV?: "development" | "staging" | "production";
  readonly VITE_AUTH_MODE?: "supabase" | "dev";
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_ANON_KEY?: string;
}
