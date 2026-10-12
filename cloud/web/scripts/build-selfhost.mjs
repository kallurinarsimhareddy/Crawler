// Build the SANA GTM dashboard for the self-hosted install (deploy/selfhost/web/Dockerfile).
//
//   VITE_SUPABASE_URL=https://<ref>.supabase.co VITE_SUPABASE_ANON_KEY=<public key> node scripts/build-selfhost.mjs
//
// The dashboard and the API share one origin (nginx serves dist/ and proxies /api to the API),
// so VITE_API_URL stays empty. Authentication is Supabase Auth (or a compatible GoTrue at any
// https URL). Security headers come from nginx, not from files in dist/.

import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { loadEnv } from "vite";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const env = loadEnv("selfhost", root, "VITE_");
const problems = [];
const need = (ok, message) => ok || problems.push(message);

need(env.VITE_AUTH_MODE === "supabase", "VITE_AUTH_MODE must be supabase");
need((env.VITE_API_URL ?? "") === "", "VITE_API_URL must be empty: the API is served on the dashboard's own origin");
need(/^https:\/\/[a-z0-9.-]+(:\d+)?$/.test(env.VITE_SUPABASE_URL ?? ""), "VITE_SUPABASE_URL must be an https:// origin");
need((env.VITE_SUPABASE_ANON_KEY ?? "").length > 20, "VITE_SUPABASE_ANON_KEY is required (the public anon/publishable key)");
for (const [key, value] of Object.entries(env)) {
  if (/localhost|127\.0\.0\.1/.test(value)) problems.push(`${key} points at a local machine`);
  if (/service_role|sb_secret_/i.test(value)) problems.push(`${key} looks like a secret key; only the public anon key belongs in the browser`);
}
if (problems.length) {
  console.error("refusing to build the self-hosted dashboard:\n  - " + problems.join("\n  - "));
  process.exit(1);
}

execFileSync(process.execPath, [join(root, "node_modules/typescript/bin/tsc"), "--noEmit"], { cwd: root, stdio: "inherit" });
execFileSync(process.execPath, [join(root, "node_modules/vite/bin/vite.js"), "build", "--mode", "selfhost"], { cwd: root, stdio: "inherit" });

const dist = join(root, "dist");
writeFileSync(join(dist, "robots.txt"), "User-agent: *\nDisallow: /\n");
const assets = join(dist, "assets");
for (const file of readdirSync(assets)) {
  const text = readFileSync(join(assets, file), "utf8");
  for (const bad of ["localhost:8000", "127.0.0.1:8000", "http://127.0.0.1", "service_role", "trycloudflare.com"]) {
    if (text.includes(bad)) {
      console.error(`bundle ${file} contains ${bad}`);
      process.exit(1);
    }
  }
}
console.log(`self-hosted dashboard built in ${dist}\n  API same origin (/api)\n  Auth ${env.VITE_SUPABASE_URL}`);
