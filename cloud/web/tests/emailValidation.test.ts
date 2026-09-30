import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ACTION_GROUPS,
  COPY_GROUPS,
  FINAL_LABELS,
  FINAL_STATUSES,
  MAX_PASTED,
  MAX_UPLOAD_ROWS,
  NOT_VERIFIED_STATUSES,
  RESULT_TABS,
  SUMMARY_LABELS,
  collectEmails,
  copyTarget,
  emailsToText,
  estimatedCredits,
  exportPath,
  finalStatus,
  initialColumn,
  itemQuery,
  parseContactRows,
  parsePastedEmails,
  pastedRows,
  progressPercent,
  providerState,
  reasonFor,
  signalTone,
  sourceLabel,
  tabCount,
  uploadProblem,
} from "../src/platform/logic/emailValidation.ts";

const tab = (key: string) => RESULT_TABS.find((t) => t.key === key)!;

// --- upload, tabs, filters ----------------------------------------------------------------

test("upload accepts CSV/XLSX up to 100 MB; a million rows need no manual splitting", () => {
  assert.equal(uploadProblem("leads.csv", 10), null);
  assert.equal(uploadProblem("LEADS.XLSX", 90 * 1024 * 1024), null);
  assert.match(uploadProblem("leads.pdf", 10)!, /CSV or XLSX/);
  assert.match(uploadProblem("leads.csv", 0)!, /empty/);
  assert.match(uploadProblem("leads.csv", 101 * 1024 * 1024)!, /100 MB/);
  assert.equal(MAX_UPLOAD_ROWS, 1_000_000);
});

test("strict 3-status UI: the main filters are All, Valid, Invalid, Not Verified only", () => {
  assert.deepEqual(RESULT_TABS.map((t) => t.label), ["All", "Valid", "Invalid", "Not Verified"]);
  assert.deepEqual([...FINAL_STATUSES], ["VALID", "INVALID", "NOT_VERIFIED"]);
  assert.deepEqual(Object.values(FINAL_LABELS), ["Valid", "Invalid", "Not verified"]);
  for (const hidden of ["Risky", "Role", "Disposable", "Free provider", "Unknown", "Catch-all", "SPF", "MX", "SMTP"]) {
    assert.ok(!RESULT_TABS.some((t) => t.label === hidden), `${hidden} must not be a top-level filter`);
  }
  const counts = { total: 10, processed: 10, final_valid: 2, final_invalid: 3, final_not_verified: 5, UNKNOWN: 4, ROLE: 1 };
  assert.deepEqual(RESULT_TABS.map((t) => tabCount(t, counts)), [10, 2, 3, 5]);
});

test("Not Verified covers every internal signal status", () => {
  assert.deepEqual(NOT_VERIFIED_STATUSES, ["UNKNOWN", "RISKY", "ROLE", "DISPOSABLE", "FREE_PROVIDER"]);
  assert.deepEqual(itemQuery(tab("not_verified"), {}, 50, 0), { limit: 50, offset: 0, status__in: "UNKNOWN,RISKY,ROLE,DISPOSABLE,FREE_PROVIDER" });
  assert.deepEqual(itemQuery(tab("valid"), {}, 50, 0), { limit: 50, offset: 0, status: "VALID", provider: "emaillistverify" });
  assert.equal(exportPath("evj_1", "csv", tab("valid")), "/email/jobs/evj_1/export?format=csv&status_filter=VALID&provider_filter=emaillistverify");
  assert.equal(exportPath("evj_1", "csv", tab("all")), "/email/jobs/evj_1/export?format=csv");
});

test("final status mirrors the server: VALID only from a mailbox verifier", () => {
  assert.equal(finalStatus("VALID", "emaillistverify"), "VALID");
  assert.equal(finalStatus("VALID", "local"), "NOT_VERIFIED");
  assert.equal(finalStatus("INVALID", "local"), "INVALID");
  for (const s of NOT_VERIFIED_STATUSES) assert.equal(finalStatus(s, "emaillistverify"), "NOT_VERIFIED", s);
});

test("progress never reports 100% early", () => {
  assert.equal(progressPercent({ total: 0 }), 0);
  assert.equal(progressPercent({ total: 1000, processed: 999 }), 99);
  assert.equal(progressPercent({ total: 4, processed: 4 }), 100);
});

test("item filters become store filters", () => {
  const q = itemQuery(tab("all"), { email: " ann ", domain: "ACME.com", provider: "local", from: "2026-09-01", to: "2026-09-02" }, 50, 100);
  assert.deepEqual(q, {
    limit: 50,
    offset: 100,
    email__ilike: "ann",
    domain: "acme.com",
    provider: "local",
    validated_at__gte: "2026-09-01",
    validated_at__lte: "2026-09-02T23:59:59",
  });
});

test("EmailListVerify is active only when verified", () => {
  assert.equal(providerState(null).label, "Not configured");
  assert.equal(providerState({ configured: true, verified: false }).active, false);
  assert.equal(providerState({ configured: true, verified: true }).active, true);
});

test("reasons stay honest", () => {
  assert.equal(reasonFor("UNKNOWN", { mx: true }), "Mail server exists; mailbox not verified");
  assert.equal(reasonFor("UNKNOWN", { mx: true, result_code: "unknown" }), "EmailListVerify could not confirm the mailbox (unknown)");
  assert.equal(reasonFor("INVALID", { syntax: false }), "Not a valid email address");
  assert.equal(reasonFor("INVALID", { empty: true }), "No email address in this row");
});

test("initial column prefers the saved choice, then the best candidate", () => {
  const cols = ["Name", "Email"];
  assert.equal(initialColumn("Name", [{ column: "Email", score: 90, reason: "" }], cols), "Name");
  assert.equal(initialColumn(null, [{ column: "Email", score: 90, reason: "" }], cols), "Email");
  assert.equal(initialColumn(null, [], cols), "");
});

// --- provenance and evidence card -----------------------------------------------------------

test("source labels: Built-in, EmailListVerify, Built-in + EmailListVerify, Public evidence", () => {
  assert.equal(sourceLabel("local", { syntax: true }), "Built-in");
  assert.equal(sourceLabel("emaillistverify", { syntax: true, builtin_status: "UNKNOWN" }), "Built-in + EmailListVerify");
  assert.equal(sourceLabel("emaillistverify", { result_code: "ok" }), "EmailListVerify");
  assert.equal(sourceLabel("local", { evidence: { public: { public_email_evidence: true } } }), "Built-in + Public evidence");
});

test("evidence card lists every signal; risk signals read as bad when present", () => {
  assert.deepEqual(SUMMARY_LABELS.map(([k]) => k), ["technical", "domain", "mx", "spf", "dmarc", "smtp", "catch_all", "role",
    "disposable", "free_provider", "person_match", "public_evidence", "mailbox_verification", "source"]);
  assert.equal(signalTone("mx", "PASS"), "good");
  assert.equal(signalTone("mx", "FAIL"), "bad");
  assert.equal(signalTone("spf", "FAIL"), "neutral"); // missing SPF is a supporting signal, not a failure
  assert.equal(signalTone("role", "YES"), "bad");
  assert.equal(signalTone("catch_all", "NO"), "good");
  assert.equal(signalTone("mailbox_verification", "VERIFIED"), "good");
});

// --- pasted emails -----------------------------------------------------------------------------

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
});

test("paste: duplicates are counted and removed, first occurrence kept", () => {
  const p = parsePastedEmails("ann@acme.com\nbob@acme.com\nann@acme.com\nann@acme.com");
  assert.deepEqual([p.total, p.unique, p.duplicates], [4, 2, 2]);
});

test("paste: upper case is normalized, so case variants are duplicates", () => {
  const p = parsePastedEmails("Ann.Lee@ACME.com\nann.lee@acme.COM\nMAILTO:Bob@Acme.com");
  assert.deepEqual(p.emails, ["ann.lee@acme.com", "bob@acme.com"]);
});

test("paste: obviously malformed entries are reported and not sent", () => {
  const p = parsePastedEmails("not-an-email\nann@\n@acme.com\nann@acme\nann@@acme.com\nann@acme.com");
  assert.deepEqual(p.malformed, ["not-an-email", "ann@", "@acme.com", "ann@acme", "ann@@acme.com"]);
  assert.deepEqual(pastedRows(p), [{ email: "ann@acme.com" }]);
});

test("paste: empty input has nothing to validate", () => {
  for (const text of ["", "   ", "\n\t,;\n", undefined as unknown as string]) {
    const p = parsePastedEmails(text);
    assert.deepEqual([p.total, p.unique, p.emails.length], [0, 0, 0]);
  }
});

test("paste: mixed input matches the preview arithmetic", () => {
  const valid = Array.from({ length: 116 }, (_, i) => `person${i}@acme-test.com`);
  const text = [...valid, ...valid.slice(0, 8).map((e) => e.toUpperCase()), "bad", "worse@", "x@y"].join(", ");
  const p = parsePastedEmails(text);
  assert.deepEqual([p.total, p.unique, p.duplicates, p.malformed.length, p.emails.length], [127, 119, 8, 3, 116]);
});

test("paste: a large list parses quickly and is capped", () => {
  const big = Array.from({ length: MAX_PASTED + 25 }, (_, i) => `user${i}@example-corp.com`).join("\n");
  const started = Date.now();
  const p = parsePastedEmails(big);
  assert.ok(Date.now() - started < 2000);
  assert.deepEqual([p.emails.length, p.overLimit], [MAX_PASTED, 25]);
});

test("credit estimate is zero unless paid checks are on", () => {
  assert.equal(estimatedCredits(116, 1, false), 0);
  assert.equal(estimatedCredits(116, 1, true), 116);
  assert.equal(estimatedCredits(3, 0.5, true), 2);
});

// --- contact mode -------------------------------------------------------------------------------

test("contacts: spreadsheet rows with a header", () => {
  const text = "First Name\tLast Name\tCompany\tTitle\tEmail\nJohn\tSmith\tAcme Corp\tCFO\tJohn.Smith@Acme.com\nAnn\tLee\tAcme Corp\tCTO\tinfo@acme.com\n";
  const c = parseContactRows(text);
  assert.equal(c.hasHeader, true);
  assert.deepEqual(c.rows[0], { first_name: "John", last_name: "Smith", company: "Acme Corp", title: "CFO", email: "john.smith@acme.com" });
  assert.equal(c.rows[1].email, "info@acme.com");
  assert.equal(c.total, 2);
});

test("contacts: no header, comma-separated, email found wherever it sits", () => {
  const c = parseContactRows("John, Smith, Acme, CFO, john@acme.com\njane@widgets.io, Jane, Doe, Widgets, VP\n");
  assert.deepEqual(c.rows.map((r) => [r.first_name, r.last_name, r.company, r.email]),
    [["John", "Smith", "Acme", "john@acme.com"], ["Jane", "Doe", "Widgets", "jane@widgets.io"]]);
});

test("contacts: duplicates, missing and malformed emails", () => {
  const c = parseContactRows("A,B,C,D,a@x.com\nA,B,C,D,A@X.com\nNo,Email,Here,,\nBad,Row,Co,T,bad@@x.com\n");
  assert.deepEqual([c.rows.length, c.duplicates, c.missingEmail, c.malformed.length], [1, 1, 1, 1]);
});

// --- copy actions ----------------------------------------------------------------------------------

test("copy buttons: Copy Valid Emails, Copy Invalid Emails, Copy Not Verified", () => {
  assert.deepEqual(COPY_GROUPS.map((g) => g.label), ["Copy Valid Emails", "Copy Invalid Emails", "Copy Not Verified"]);
  assert.deepEqual(copyTarget(COPY_GROUPS[0]), { statuses: ["VALID"], provider: "emaillistverify" });
  assert.deepEqual(copyTarget(COPY_GROUPS[2]).statuses, NOT_VERIFIED_STATUSES);
  assert.deepEqual(ACTION_GROUPS.map((g) => g.key), ["VALID", "NOT_VERIFIED"]); // Invalid is never offered
});

test("Copy Valid Emails contains only final VALID rows, never Not Verified", async () => {
  const rows = [
    { email: "ok@x.com", status: "VALID", provider: "emaillistverify" },
    { email: "unknown@x.com", status: "UNKNOWN", provider: "local" },
    { email: "catchall@x.com", status: "RISKY", provider: "emaillistverify" },
    { email: "legacy@x.com", status: "VALID", provider: "local" },
    { email: "role@x.com", status: "ROLE", provider: "local" },
    { email: "ok2@x.com", status: "VALID", provider: "emaillistverify" },
  ];
  // A fake items endpoint applying the same status/provider filters as the server.
  const fetchPage = async (q: Record<string, string | number>) => {
    const statuses = q.status__in ? String(q.status__in).split(",") : q.status ? [String(q.status)] : null;
    const hit = rows.filter((r) => (!statuses || statuses.includes(r.status)) && (!q.provider || r.provider === q.provider));
    const offset = Number(q.offset), limit = Number(q.limit);
    return { items: hit.slice(offset, offset + limit), has_more: offset + limit < hit.length };
  };
  const valid = await collectEmails(fetchPage, copyTarget(COPY_GROUPS[0]), 1);
  assert.equal(valid, "ok@x.com\nok2@x.com");
  const notVerified = await collectEmails(fetchPage, copyTarget(COPY_GROUPS[2]));
  assert.equal(notVerified, "unknown@x.com\ncatchall@x.com\nrole@x.com");
  assert.equal(emailsToText([" a@x.com ", "", "a@x.com", "b@x.com"]), "a@x.com\nb@x.com");
});

test("paste: leading, trailing and doubled dots in the local part are malformed", () => {
  const p = parsePastedEmails("bad..dots@domain.com\n.lead@domain.com\ntrail.@domain.com\ngood.dots@domain.com");
  assert.deepEqual(p.malformed, ["bad..dots@domain.com", ".lead@domain.com", "trail.@domain.com"]);
  assert.deepEqual(p.emails, ["good.dots@domain.com"]);
});
