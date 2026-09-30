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

// --- pasted emails --------------------------------------------------------------------

import {
  COPY_GROUPS,
  MAX_PASTED,
  collectEmails,
  emailsToText,
  estimatedCredits,
  parsePastedEmails,
  pastedRows,
} from "../src/platform/logic/emailValidation.ts";

test("paste: newline-separated emails", () => {
  const p = parsePastedEmails("ann@acme.com\nbob@acme.com\r\ncara@acme.io\n");
  assert.deepEqual(p.emails, ["ann@acme.com", "bob@acme.com", "cara@acme.io"]);
  assert.equal(p.total, 3);
});

test("paste: comma-separated emails", () => {
  assert.deepEqual(parsePastedEmails("ann@acme.com,bob@acme.com, cara@acme.io").emails, ["ann@acme.com", "bob@acme.com", "cara@acme.io"]);
});

test("paste: semicolon-separated emails (Outlook style)", () => {
  assert.deepEqual(parsePastedEmails("ann@acme.com; bob@acme.com;<cara@acme.io>;").emails, ["ann@acme.com", "bob@acme.com", "cara@acme.io"]);
});

test("paste: spaces, tabs and blank lines are separators and are trimmed", () => {
  const p = parsePastedEmails("  ann@acme.com\t\tbob@acme.com   \n\n\t cara@acme.io  ");
  assert.deepEqual(p.emails, ["ann@acme.com", "bob@acme.com", "cara@acme.io"]);
  assert.equal(p.total, 3);
});

test("paste: duplicates are counted and removed, first occurrence kept", () => {
  const p = parsePastedEmails("ann@acme.com\nbob@acme.com\nann@acme.com\nann@acme.com");
  assert.deepEqual([p.total, p.unique, p.duplicates], [4, 2, 2]);
  assert.deepEqual(p.emails, ["ann@acme.com", "bob@acme.com"]);
});

test("paste: upper case is normalized, so case variants are duplicates", () => {
  const p = parsePastedEmails("Ann.Lee@ACME.com\nann.lee@acme.COM\nMAILTO:Bob@Acme.com");
  assert.deepEqual(p.emails, ["ann.lee@acme.com", "bob@acme.com"]);
  assert.equal(p.duplicates, 1);
});

test("paste: obviously malformed entries are reported and not sent", () => {
  const p = parsePastedEmails("not-an-email\nann@\n@acme.com\nann@acme\nann@@acme.com\nann@acme.com");
  assert.deepEqual(p.malformed, ["not-an-email", "ann@", "@acme.com", "ann@acme", "ann@@acme.com"]);
  assert.deepEqual(p.emails, ["ann@acme.com"]);
  assert.deepEqual(pastedRows(p), [{ email: "ann@acme.com" }]);
});

test("paste: empty input has nothing to validate", () => {
  for (const text of ["", "   ", "\n\t,;\n", undefined as unknown as string]) {
    const p = parsePastedEmails(text);
    assert.deepEqual([p.total, p.unique, p.duplicates, p.malformed.length, p.emails.length], [0, 0, 0, 0, 0]);
  }
});

test("paste: mixed input matches the preview arithmetic", () => {
  const valid = Array.from({ length: 116 }, (_, i) => `person${i}@acme-test.com`);
  const text = [...valid, ...valid.slice(0, 8).map((e) => e.toUpperCase()), "bad", "worse@", "x@y"].join(", ");
  const p = parsePastedEmails(text);
  // Pasted 127 = 116 + 8 duplicates + 3 malformed; unique 119; to validate 116.
  assert.deepEqual([p.total, p.unique, p.duplicates, p.malformed.length, p.emails.length], [127, 119, 8, 3, 116]);
});

test("paste: a large list parses quickly and is capped like an upload", () => {
  const big = Array.from({ length: MAX_PASTED + 25 }, (_, i) => `user${i}@example-corp.com`).join("\n");
  const started = Date.now();
  const p = parsePastedEmails(big);
  assert.ok(Date.now() - started < 2000, "parsing 50k addresses should take well under 2s");
  assert.equal(p.emails.length, MAX_PASTED);
  assert.equal(p.overLimit, 25);
  assert.equal(p.unique, MAX_PASTED + 25);
});

test("estimated credits only when paid checks are on", () => {
  assert.equal(estimatedCredits(116, 1, false), 0);
  assert.equal(estimatedCredits(116, 1, true), 116);
  assert.equal(estimatedCredits(3, 0.5, true), 2);
  assert.equal(estimatedCredits(0, 1, true), 0);
});

test("copy valid emails: pages through the Valid results and joins one per line", async () => {
  const queries: Record<string, string | number>[] = [];
  const pages = [
    { items: [{ email: "a@x.com" }, { email: "b@x.com" }], has_more: true },
    { items: [{ email: "c@x.com" }, { email: "a@x.com" }, { email: null }], has_more: false },
  ];
  const text = await collectEmails(async (q) => { queries.push(q); return pages[queries.length - 1]; }, COPY_GROUPS.find((g) => g.key === "valid")!.statuses, 2);
  assert.equal(text, "a@x.com\nb@x.com\nc@x.com");
  assert.deepEqual(queries, [{ limit: 2, offset: 0, status: "VALID" }, { limit: 2, offset: 2, status: "VALID" }]);
  assert.deepEqual(COPY_GROUPS.map((g) => g.label), ["Copy Valid Emails", "Copy Invalid Emails", "Copy Risky Emails"]);
  assert.equal(emailsToText([" a@x.com ", "", "a@x.com", "b@x.com"]), "a@x.com\nb@x.com");
});
