// Offline tests for cloud/web/functions/api/[[path]].js (the sanagtm.pages.dev /api proxy).
//   node --test deploy/windows/installer/tests/proxy_function.test.mjs
// fetch is replaced by a fake: no network, nothing deployed.

import assert from "node:assert/strict";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const modUrl = pathToFileURL(resolve("cloud/web/functions/api/[[path]].js")).href;
const ENV = { UPSTASH_REDIS_REST_URL: "https://db.upstash.io", UPSTASH_REDIS_REST_TOKEN: "tok" };

async function load(tag) {
  // A fresh module instance per test: the origin cache is module state.
  return import(`${modUrl}?${tag}`);
}

function fakeFetch(origin, upstream) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).startsWith("https://db.upstash.io/get/")) {
      return new Response(JSON.stringify({ result: origin }), { status: 200 });
    }
    return upstream(String(url), init);
  };
  return calls;
}

test("forwards path, query, method and body to the published origin", async () => {
  const { onRequest } = await load("a");
  const calls = fakeFetch("https://abc-def.trycloudflare.com", async (url, init) =>
    new Response(JSON.stringify({ ok: true, url, method: init.method }), { status: 201 }));
  const req = new Request("https://sanagtm.pages.dev/api/v1/jobs?x=1", {
    method: "POST", body: "{}", headers: { Authorization: "Bearer t", Cookie: "c=1" },
  });
  const res = await onRequest({ request: req, env: ENV });
  assert.equal(res.status, 201);
  const up = calls[1];
  assert.equal(up.url, "https://abc-def.trycloudflare.com/api/v1/jobs?x=1");
  assert.equal(up.init.method, "POST");
  assert.equal(up.init.headers.get("Authorization"), "Bearer t");
  assert.equal(up.init.headers.get("Cookie"), null);
  assert.equal(calls[0].url, "https://db.upstash.io/get/sanagtm%3Astaging%3Afrontend%3Aapi_origin");
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
});

test("offline (503) when no origin is published", async () => {
  const { onRequest } = await load("b");
  fakeFetch(null, async () => assert.fail("must not call upstream"));
  const res = await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/health"), env: ENV });
  assert.equal(res.status, 503);
  assert.match((await res.json()).detail, /offline/);
});

test("refuses origins that are not a tunnel or an allowed host", async () => {
  for (const [i, bad] of ["https://evil.example.com", "http://abc.trycloudflare.com",
    "https://abc.trycloudflare.com/path", "https://user@abc.trycloudflare.com", "javascript:alert(1)"].entries()) {
    const { onRequest } = await load(`c${i}`);
    fakeFetch(bad, async () => assert.fail("must not call " + bad));
    const res = await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/health"), env: ENV });
    assert.equal(res.status, 503, bad);
  }
  const { onRequest } = await load("c-extra");
  fakeFetch("https://api.example.com", async () => new Response("ok"));
  const res = await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/health"),
    env: { ...ENV, SANA_API_HOSTS: "api.example.com" } });
  assert.equal(res.status, 200);
});

test("tunnel errors become 503 and force a fresh lookup", async () => {
  const { onRequest } = await load("d");
  const calls = fakeFetch("https://abc.trycloudflare.com", async () => new Response("bad", { status: 530 }));
  const r1 = await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/me"), env: ENV });
  assert.equal(r1.status, 503);
  await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/me"), env: ENV });
  assert.equal(calls.filter((c) => c.url.includes("upstash")).length, 2);
});

test("origin lookup is cached between requests", async () => {
  const { onRequest } = await load("e");
  const calls = fakeFetch("https://abc.trycloudflare.com", async () => new Response("ok"));
  for (let i = 0; i < 3; i++) {
    await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/me"), env: ENV });
  }
  assert.equal(calls.filter((c) => c.url.includes("upstash")).length, 1);
});

test("500 when the proxy is not configured", async () => {
  const { onRequest } = await load("f");
  const res = await onRequest({ request: new Request("https://sanagtm.pages.dev/api/v1/me"), env: {} });
  assert.equal(res.status, 500);
});
