// Every sidebar item and command-palette destination must resolve to a route in
// App.tsx, and the major SANA GTM features must be discoverable from Ctrl+K.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { NAV, destinations, locate } from "../src/shell/nav.ts";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const routes = new Set(
  [...app.matchAll(/<Route\s+(?:index|path="([^"]*)")/g)].map((m) => (m[1] === undefined ? "/" : `/${m[1]}`)),
);

function routed(to: string): boolean {
  const path = to.split("?")[0];
  return routes.has(path);
}

test("every sidebar item has a route", () => {
  for (const group of NAV) {
    if (group.to) assert.ok(routed(group.to), `group ${group.key} -> ${group.to}`);
    for (const item of group.items) assert.ok(routed(item.to), `${item.label} -> ${item.to}`);
  }
});

test("every palette destination has a route", () => {
  for (const d of destinations()) assert.ok(routed(d.to), `${d.label} -> ${d.to}`);
});

test("major features are discoverable from the command palette", () => {
  const labels = destinations().map((d) => d.label);
  for (const feature of [
    "Companies", "Contacts", "Deals", "Tasks", "Activities", "Prospecting", "Lists", "Campaigns", "Sequences",
    "Email Validation", "Templates", "Suppression list", "Hiring Intelligence", "Signals", "Research Agent",
    "AI Scraper", "Workflows", "Monitors", "Analytics", "Imports", "Internal Data", "Exports", "Sources",
    "Email & Sending", "Integrations", "Users & Permissions", "Audit Log", "Settings", "Background jobs",
    "Notifications", "Segments", "Credits", "AI workspace (Control Room)",
  ]) {
    assert.ok(labels.includes(feature), `missing from palette: ${feature}`);
  }
});

test("new settings pages own their breadcrumbs", () => {
  assert.equal(locate("/settings/users")?.item?.label, "Users & Permissions");
  assert.equal(locate("/settings/audit")?.item?.label, "Audit Log");
  assert.equal(locate("/settings/sending")?.item?.label, "Email & Sending");
  assert.equal(locate("/settings/integrations")?.item?.label, "Integrations");
  assert.equal(locate("/settings")?.item?.label, "Settings");
  assert.equal(locate("/email-validation/evj_1")?.item?.label, "Email Validation");
  assert.equal(locate("/suppressions")?.item?.label, "Campaigns");
});

test("palette destinations are unique by route and label (React keys)", () => {
  const ids = destinations().map((d) => `${d.to}:${d.label}`);
  assert.equal(new Set(ids).size, ids.length);
});

test("job source monitor routing (D1): /jobs is the Jobs area, crawls moved under Settings", () => {
  assert.equal(locate("/jobs")?.item?.label, "Jobs");
  assert.equal(locate("/jobs/jp_1")?.item?.label, "Jobs");
  assert.equal(locate("/jobs/import")?.item?.label, "Jobs");
  assert.equal(locate("/postings")?.item?.label, "Jobs");
  assert.equal(locate("/monitors/jm_1")?.item?.label, "Monitors");
  assert.equal(locate("/settings/crawls")?.item?.label, "Settings");
  assert.equal(locate("/settings/crawls/job_1")?.item?.label, "Settings");
  const palette = destinations();
  assert.equal(palette.find((d) => d.label === "Jobs")?.to, "/jobs");
  assert.equal(palette.find((d) => d.label === "Crawls")?.to, "/settings/crawls");
  for (const path of ["/jobs", "/jobs/import", "/jobs/keywords", "/jobs/:jobId", "/monitors", "/monitors/:monitorId", "/postings", "/settings/crawls", "/settings/crawls/:jobId"]) {
    assert.ok(routes.has(path), `missing route ${path}`);
  }
  const intel = NAV.find((g) => g.key === "intel")!;
  assert.ok(intel.items.some((i) => i.to === "/jobs" && i.label === "Jobs"));
  assert.ok(intel.items.some((i) => i.to === "/monitors" && i.label === "Monitors"));
});
