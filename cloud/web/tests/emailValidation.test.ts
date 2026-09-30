import { test } from "node:test";
import assert from "node:assert/strict";
import {
  RESULT_TABS,
  exportPath,
  initialColumn,
  itemQuery,
  progressPercent,
  providerState,
  reasonFor,
  tabCount,
  uploadProblem,
} from "../src/platform/logic/emailValidation.ts";

const tab = (key: string) => RESULT_TABS.find((t) => t.key === key)!;

test("upload accepts only CSV/XLSX within the size limit", () => {
  assert.equal(uploadProblem("leads.csv", 10), null);
  assert.equal(uploadProblem("LEADS.XLSX", 10), null);
  assert.match(uploadProblem("leads.pdf", 10)!, /CSV or XLSX/);
  assert.match(uploadProblem("leads.csv", 0)!, /empty/);
  assert.match(uploadProblem("leads.csv", 26 * 1024 * 1024)!, /25 MB/);
});

test("tabs cover the required result groups and count correctly", () => {
  const keys = RESULT_TABS.map((t) => t.key);
  for (const k of ["all", "valid", "invalid", "risky", "role", "disposable", "unknown"]) assert.ok(keys.includes(k), k);
  const counts = { total: 10, VALID: 2, UNKNOWN: 5, ROLE: 3 };
  assert.equal(tabCount(tab("all"), counts), 10);
  assert.equal(tabCount(tab("unknown"), counts), 5);
  assert.equal(tabCount(tab("risky"), counts), 0);
});

test("progress never reports 100% early", () => {
  assert.equal(progressPercent({ total: 0 }), 0);
  assert.equal(progressPercent({ total: 1000, processed: 999 }), 99);
  assert.equal(progressPercent({ total: 4, processed: 4 }), 100);
});

test("item filters become store filters", () => {
  const q = itemQuery(tab("valid"), { email: " ann ", domain: "ACME.com", provider: "local", from: "2026-09-01", to: "2026-09-02" }, 50, 100);
  assert.deepEqual(q, {
    limit: 50,
    offset: 100,
    status: "VALID",
    email__ilike: "ann",
    domain: "acme.com",
    provider: "local",
    validated_at__gte: "2026-09-01",
    validated_at__lte: "2026-09-02T23:59:59",
  });
  assert.deepEqual(itemQuery(tab("all"), {}, 25, 0), { limit: 25, offset: 0 });
});

test("export path carries the tab's statuses", () => {
  assert.equal(exportPath("evj_1", "csv", tab("all")), "/email/jobs/evj_1/export?format=csv");
  assert.equal(exportPath("evj_1", "xlsx", tab("role")), "/email/jobs/evj_1/export?format=xlsx&status_filter=ROLE");
});

test("EmailListVerify is active only when verified", () => {
  assert.equal(providerState(null).label, "Not configured");
  assert.equal(providerState({ configured: true, verified: false }).label, "Configured but not verified");
  assert.equal(providerState({ configured: true, verified: false }).active, false);
  assert.equal(providerState({ configured: true, verified: true }).active, true);
});

test("reasons stay honest about UNKNOWN", () => {
  assert.equal(reasonFor("UNKNOWN", { mx: true }), "Mail server exists; mailbox not verified");
  assert.equal(reasonFor("INVALID", { syntax: false }), "Not a valid email address");
  assert.equal(reasonFor("INVALID", { empty: true }), "No email address in this row");
});

test("initial column prefers the saved choice, then the best candidate", () => {
  const cols = ["Name", "Email"];
  assert.equal(initialColumn("Name", [{ column: "Email", score: 90, reason: "" }], cols), "Name");
  assert.equal(initialColumn(null, [{ column: "Email", score: 90, reason: "" }], cols), "Email");
  assert.equal(initialColumn(null, [], cols), "");
});
