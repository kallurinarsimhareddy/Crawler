// Build the CareerCloud STAGING dashboard for Cloudflare Pages.
//
//   npm run build:staging        (reads .env.staging / .env.staging.local)
//
// Refuses to build unless the configuration is unmistakably staging, then writes
// dist/_headers (strict CSP, noindex), dist/_redirects (SPA routing) and
// dist/robots.txt, and scans the bundle for localhost or non-staging URLs.

import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { loadEnv } from "vite";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const env = loadEnv("staging", root, "VITE_");
const problems = [];
const need = (ok, message) => ok || problems.push(message);

need(env.VITE_DEPLOY_ENV === "staging", "VITE_DEPLOY_ENV must be staging");
need(env.VITE_AUTH_MODE === "supabase", "VITE_AUTH_MODE must be supabase");
need(/^https:\/\/[a-z0-9.-]+$/.test(env.VITE_API_URL ?? ""), "VITE_API_URL must be an https:// origin with no path");
const supabase = /^https:\/\/([a-z0-9]{8,40})\.supabase\.co$/.exec(env.VITE_SUPABASE_URL ?? "");
need(Boolean(supabase), "VITE_SUPABASE_URL must be https://<ref>.supabase.co");
need((env.VITE_SUPABASE_ANON_KEY ?? "").length > 20, "VITE_SUPABASE_ANON_KEY is required (the public anon/publishable key)");
for (const [key, value] of Object.entries(env)) {
  if (/localhost|127\.0\.0\.1/.test(value)) problems.push(`${key} points at this machine`);
  if (/(^|[^a-z])prod/i.test(value)) problems.push(`${key} looks like a production value`);
  if (/service_role|sb_secret_/i.test(value)) problems.push(`${key} looks like a secret key; only the public anon key belongs in the browser`);
}

// Optional: the same resource registry the API uses.
if (env.VITE_RESOURCE_REGISTRY && existsSync(env.VITE_RESOURCE_REGISTRY)) {
  const registry = JSON.parse(readFileSync(env.VITE_RESOURCE_REGISTRY, "utf8"));
  const staging = registry.staging ?? {};
  const production = registry.production ?? {};
  need((staging.api_origins ?? []).includes(env.VITE_API_URL), "VITE_API_URL is not registered for staging");
  need(!(production.api_origins ?? []).includes(env.VITE_API_URL), "VITE_API_URL is a production origin");
  if (supabase) {
    need((staging.supabase_refs ?? []).includes(supabase[1]), "Supabase project is not registered for staging");
    need(!(production.supabase_refs ?? []).includes(supabase[1]), "Supabase project is the production project");
  }
}

if (problems.length) {
  console.error("refusing to build the staging dashboard:\n  - " + problems.join("\n  - "));
  process.exit(1);
}

execFileSync(process.execPath, [join(root, "node_modules/typescript/bin/tsc"), "--noEmit"], { cwd: root, stdio: "inherit" });
execFileSync(process.execPath, [join(root, "node_modules/vite/bin/vite.js"), "build", "--mode", "staging"], { cwd: root, stdio: "inherit" });

const dist = join(root, "dist");
const ref = supabase[1];
const csp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self' ${env.VITE_API_URL} https://${ref}.supabase.co wss://${ref}.supabase.co`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
  "upgrade-insecure-requests",
].join("; ");

writeFileSync(
  join(dist, "_headers"),
  `/*
  Content-Security-Policy: ${csp}
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  Cross-Origin-Opener-Policy: same-origin
  Strict-Transport-Security: max-age=31536000
  X-Robots-Tag: noindex, nofollow
/assets/*
  Cache-Control: public, max-age=31536000, immutable
`,
);
writeFileSync(join(dist, "_redirects"), "/* /index.html 200\n");
writeFileSync(join(dist, "robots.txt"), "User-agent: *\nDisallow: /\n");

// The bundle must not contain a local or non-staging API address.
const assets = join(dist, "assets");
for (const file of readdirSync(assets)) {
  const text = readFileSync(join(assets, file), "utf8");
  for (const bad of ["localhost:8000", "127.0.0.1:8000", "http://127.0.0.1", "service_role"]) {
    if (text.includes(bad)) {
      console.error(`bundle ${file} contains ${bad}`);
      process.exit(1);
    }
  }
  if (!text.includes(env.VITE_API_URL) && file.endsWith(".js") && text.includes("/api/v1/")) {
    console.error(`bundle ${file} does not reference the staging API URL`);
    process.exit(1);
  }
}
console.log(`staging dashboard built in ${dist}\n  API ${env.VITE_API_URL}\n  Supabase ${env.VITE_SUPABASE_URL}\n  CSP connect-src restricted to those origins`);
