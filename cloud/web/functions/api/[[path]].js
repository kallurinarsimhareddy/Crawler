// Cloudflare Pages Function: https://sanagtm.pages.dev/api/* -> the SANA GTM API on the host PC.
//
// The API runs on a Windows PC (SANA-GTM-Setup.exe) behind a Cloudflare tunnel whose
// public URL can change on every restart. The PC's supervisor publishes the current URL
// to Upstash Redis (key <prefix>:frontend:api_origin, 5-minute expiry, refreshed every
// minute); this function reads it, so the dashboard is built once with
// VITE_API_URL=https://sanagtm.pages.dev and never needs a rebuild for a new tunnel URL.
//
// Pages secrets / variables:
//   UPSTASH_REDIS_REST_URL     https://<db>.upstash.io
//   UPSTASH_REDIS_REST_TOKEN   (secret)
//   SANA_API_ORIGIN_KEY        default sanagtm:staging:frontend:api_origin
//   SANA_API_HOSTS             optional comma list of extra allowed API hostnames (named tunnels)

const CACHE_MS = 15_000;
let cached = { origin: null, at: 0 };

function allowed(origin, env) {
  let url;
  try {
    url = new URL(origin);
  } catch {
    return false;
  }
  if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.username) return false;
  if (/^[a-z0-9-]+\.trycloudflare\.com$/.test(url.hostname)) return true;
  const extra = (env.SANA_API_HOSTS || "").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
  return extra.includes(url.hostname);
}

async function apiOrigin(env) {
  const now = Date.now();
  if (now - cached.at < CACHE_MS) return cached.origin;
  const key = env.SANA_API_ORIGIN_KEY || "sanagtm:staging:frontend:api_origin";
  const res = await fetch(`${env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(key)}`, {
    headers: { Authorization: `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}` },
  });
  let origin = null;
  if (res.ok) {
    const value = (await res.json()).result;
    if (typeof value === "string" && allowed(value, env)) origin = value.replace(/\/+$/, "");
  }
  cached = { origin, at: now };
  return origin;
}

function offline(detail, status = 503) {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "Retry-After": "30" },
  });
}

export async function onRequest({ request, env }) {
  if (!env.UPSTASH_REDIS_REST_URL || !env.UPSTASH_REDIS_REST_TOKEN) {
    return offline("SANA GTM API proxy is not configured", 500);
  }
  let origin;
  try {
    origin = await apiOrigin(env);
  } catch {
    return offline("SANA GTM could not look up its API right now; try again shortly");
  }
  if (!origin) {
    return offline("SANA GTM is offline: start SANA GTM on the host PC (SANA GTM control panel > Start)");
  }

  const incoming = new URL(request.url);
  const target = origin + incoming.pathname + incoming.search;
  const headers = new Headers(request.headers);
  headers.delete("Host");
  headers.delete("Cookie"); // the API authenticates with the Authorization header only
  headers.set("X-Forwarded-Host", incoming.host);
  headers.set("X-Forwarded-Proto", "https");
  const init = { method: request.method, headers, redirect: "manual" };
  if (!["GET", "HEAD"].includes(request.method)) init.body = request.body;

  let res;
  try {
    res = await fetch(target, init);
  } catch {
    cached.at = 0;
    return offline("SANA GTM host PC is not reachable; it may be restarting");
  }
  if ([502, 521, 522, 523, 524, 530].includes(res.status)) {
    cached.at = 0; // the tunnel URL may have just changed: look it up again next time
    return offline("SANA GTM host PC is not reachable; it may be restarting");
  }
  const out = new Response(res.body, res);
  out.headers.set("Cache-Control", "no-store");
  return out;
}
