import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ACCOUNT_STATES, LIMITS, createEffect, jobsForCompany, memberLink, parseProvenance, provenanceText, recordName,
  selectionCheck, signalAsCompany, stateLabel, stateTone, taskStopped, toItem, type HiringCompany,
  carryFilters, readView,
} from "../src/platform/logic/accounts.ts";

const row = (over: Partial<HiringCompany> = {}): HiringCompany => ({
  key: "acme", display_name: "Acme Corp", names: ["Acme Corp", "ACME CORP."], jobs: 5, open_jobs: 5, high: 4, review: 1,
  signal_ids: ["hs_1"], job_ids: ["jp_1", "jp_2"], state: "NEW ACCOUNT", crm: [], ...over,
});

test("the five CRM states have labels and badge tones", () => {
  assert.deepEqual([...ACCOUNT_STATES], ["NEW ACCOUNT", "ALREADY IN CRM", "MATCHED", "REVIEW", "LINKED"]);
  assert.equal(stateLabel("ALREADY IN CRM"), "Already in CRM");
  assert.equal(stateTone("REVIEW"), "cancelled");
  assert.equal(stateTone("LINKED"), "completed");
});

test("what Create does for each state — never a silent duplicate", () => {
  assert.equal(createEffect(row()), "Creates a new CRM account");
  assert.match(createEffect(row({ state: "ALREADY IN CRM", crm: [{ id: "co_1", name: "Acme Corp" }] })), /Links to Acme Corp/);
  assert.match(createEffect(row({ state: "REVIEW" })), /review/);
  assert.match(createEffect(row({ state: "LINKED" })), /Already linked/);
});

test("build items carry provenance and an explicit override only when asked", () => {
  assert.deepEqual(toItem(row()), { key: "acme", names: ["Acme Corp", "ACME CORP."], display_name: "Acme Corp",
    signal_ids: ["hs_1"], job_ids: ["jp_1", "jp_2"] });
  assert.equal(toItem(row(), true).create_new, true);
});

test("selection guard mirrors the server limits", () => {
  assert.equal(selectionCheck(0, false).ok, false);
  assert.equal(selectionCheck(LIMITS.syncLimit, false).ok, true);
  assert.equal(selectionCheck(LIMITS.syncLimit, false).background, false);
  assert.equal(selectionCheck(LIMITS.syncLimit + 1, false).background, true);
  assert.equal(selectionCheck(LIMITS.confirmAbove + 1, false).ok, false);
  assert.equal(selectionCheck(LIMITS.confirmAbove + 1, true).ok, true);
  assert.equal(selectionCheck(LIMITS.confirmAbove + 1, true).background, true);
  assert.equal(selectionCheck(LIMITS.maxPerAction + 1, true).ok, false);
});

test("list member provenance from the Account Builder reads as words, not JSON", () => {
  const reason = JSON.stringify({ source: "account_builder", at: "2026-10-05T10:00:00", signal_ids: ["hs_1", "hs_2"], job_ids: ["jp_1"] });
  assert.equal(parseProvenance(reason)?.signals, 2);
  assert.equal(provenanceText(reason), "Account Builder · 2 originating signals · 1 originating job");
  assert.equal(provenanceText("added by a workflow"), "added by a workflow");
  assert.equal(provenanceText(null), "—");
  assert.equal(parseProvenance("{not json"), null);
});

test("list members link to the right page and show names, never ids", () => {
  assert.equal(memberLink("companies", "co_1"), "/companies/co_1");
  assert.equal(memberLink("contacts", "ct_1"), "/contacts/ct_1");
  assert.equal(memberLink("job_postings", "jp_1"), "/jobs/jp_1");
  assert.equal(memberLink("opportunities", "op_1"), "/opportunities/op_1");
  assert.equal(recordName({ name: "Acme Corp", id: "co_1" }), "Acme Corp");
  assert.equal(recordName({ full_name: "Pat Lee" }), "Pat Lee");
  assert.equal(recordName({ id: "co_1" }), "(unnamed)");
  assert.equal(recordName(null), "(removed)");
});

test("a signal becomes an actionable company row; jobs link by company name", () => {
  const fresh = signalAsCompany({ id: "hs_9", company_name: "Northwind", signal_type: "STACK_MIGRATION", job_posting_ids: ["jp_1"] });
  assert.equal(fresh?.state, "NEW ACCOUNT");
  assert.deepEqual(fresh?.signal_ids, ["hs_9"]);
  const linked = signalAsCompany({ id: "hs_8", company_name: "Contoso", company_id: "co_7" });
  assert.equal(linked?.state, "LINKED");
  assert.equal(linked?.crm?.[0]?.id, "co_7");
  assert.equal(signalAsCompany({ id: "hs_0" }), null);
  assert.equal(jobsForCompany("Acme & Co"), "/jobs?company=Acme%20%26%20Co");
});

test("task statuses that end a background run", () => {
  for (const s of ["completed", "failed", "cancelled", "paused"]) assert.equal(taskStopped(s), true);
  for (const s of ["queued", "running", "retrying", "succeeded", undefined]) assert.equal(taskStopped(s), false);
});

test("the Hiring companies view lives in the URL and tabs carry the Jobs filters", () => {
  assert.deepEqual(readView(new URLSearchParams("hstate=review&hsort=high&hoffset=50")), { state: "REVIEW", sort: "high", offset: 50 });
  assert.deepEqual(readView(new URLSearchParams("hstate=MATCHED&hsort=bogus&hoffset=-3")), { state: "", sort: "open", offset: 0 });
  const params = new URLSearchParams("relevance=HIGH&change=stale&hoffset=50&tab=hiring&offset=100");
  assert.equal(carryFilters(params, ["relevance", "change", "offset"], "reviews"), "/jobs?relevance=HIGH&change=stale&offset=100&tab=reviews");
  assert.equal(carryFilters(new URLSearchParams(""), ["relevance"], "jobs"), "/jobs");
});
