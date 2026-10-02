import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAX_SEARCH_TERMS, boardLabel, buildJobSpyBody, countSearchTerms, disabledSelected, freshnessLabel, isJobSpyMonitor, jobSpySummary,
  parseRelevanceParam, parseSearchTerms, relevanceClass, relevanceScoreText, relevanceTone, sourceWithBoard, toggleRelevance,
  validateJobSpyForm, type JobSpyForm,
} from "../src/platform/logic/jobspy.ts";
import { tableCell } from "../src/platform/logic/jobFields.ts";

const form = (over: Partial<JobSpyForm> = {}): JobSpyForm => ({
  name: "", schedule: "daily", runNow: false, boards: ["indeed"], terms: ["SAP"], location: "United States", hoursOld: "24", resultsWanted: "25", ...over,
});

test("search terms: comma/newline split, trimmed, deduped (case-insensitive), max 25", () => {
  assert.deepEqual(parseSearchTerms(" SAP , ERP\nsap\n\n  Oracle   EBS ,,"), ["SAP", "ERP", "Oracle EBS"]);
  assert.deepEqual(parseSearchTerms(["SAP", "ERP, MES", "erp"]), ["SAP", "ERP", "MES"]);
  assert.deepEqual(parseSearchTerms(""), []);
  const many = Array.from({ length: 40 }, (_, i) => `term${i}`).join(",");
  assert.equal(parseSearchTerms(many).length, MAX_SEARCH_TERMS);
  assert.equal(parseSearchTerms(many)[24], "term24");
  assert.equal(countSearchTerms(many), 40);
});

test("JobSpy form validation: board, keyword, hours 1–720, results 1–200", () => {
  assert.deepEqual(validateJobSpyForm(form()), []);
  assert.equal(validateJobSpyForm(form({ boards: [] })).length, 1);
  assert.equal(validateJobSpyForm(form({ terms: [] })).length, 1);
  for (const hours of ["0", "721", "", "abc", "2.5", "-1"]) assert.equal(validateJobSpyForm(form({ hoursOld: hours })).length, 1, hours);
  for (const hours of ["1", "36", "720"]) assert.deepEqual(validateJobSpyForm(form({ hoursOld: hours })), [], hours);
  for (const n of ["0", "201", ""]) assert.equal(validateJobSpyForm(form({ resultsWanted: n })).length, 1, n);
  for (const n of ["1", "200"]) assert.deepEqual(validateJobSpyForm(form({ resultsWanted: n })), [], n);
  assert.equal(validateJobSpyForm(form({ terms: Array.from({ length: 26 }, (_, i) => `t${i}`) })).length, 1);
  assert.equal(validateJobSpyForm(form({ boards: [], terms: [], hoursOld: "0", resultsWanted: "0" })).length, 4);
});

test("the create body: strategy jobspy, filters with country USA, optional name / run_now", () => {
  const body = buildJobSpyBody(form({ boards: ["indeed", "linkedin", "indeed"], terms: ["SAP", "ERP"], hoursOld: "72", resultsWanted: "50", runNow: true, name: "  ERP watch ", location: "  " }));
  assert.deepEqual(body, {
    strategy: "jobspy",
    schedule: "daily",
    name: "ERP watch",
    run_now: true,
    filters: { boards: ["indeed", "linkedin"], search_terms: ["SAP", "ERP"], location: "United States", hours_old: 72, results_wanted: 50, country: "USA" },
  });
  const plain = buildJobSpyBody(form({ schedule: "weekly" }));
  assert.equal("name" in plain, false);
  assert.equal("run_now" in plain, false);
  assert.equal(plain.schedule, "weekly");
  assert.throws(() => buildJobSpyBody(form({ terms: [] })));
});

test("disabled boards are selectable but flagged", () => {
  const boards = [
    { id: "indeed", label: "Indeed", enabled: true, note: null },
    { id: "linkedin", label: "LinkedIn", enabled: false, note: "disabled until access is authorized" },
  ];
  assert.deepEqual(disabledSelected(boards, ["indeed"]), []);
  assert.deepEqual(disabledSelected(boards, ["indeed", "linkedin"]).map((b) => b.id), ["linkedin"]);
  assert.equal(boardLabel("zip_recruiter"), "ZipRecruiter");
  assert.equal(boardLabel("linkedin", boards), "LinkedIn");
  assert.equal(boardLabel("other"), "other");
});

test("a JobSpy monitor reads back from monitor.filters", () => {
  assert.ok(isJobSpyMonitor({ strategy: "jobspy" }));
  assert.ok(!isJobSpyMonitor({ strategy: "site_profile", source_name: "Greenhouse" }));
  assert.ok(!isJobSpyMonitor(null));
  const s = jobSpySummary({ boards: ["indeed", "zip_recruiter"], search_terms: ["SAP", "ERP"], location: "Texas", hours_old: 168, results_wanted: 25 });
  assert.deepEqual(s, { boards: ["Indeed", "ZipRecruiter"], terms: ["SAP", "ERP"], location: "Texas", freshness: "Last 7 days", results: "25 per search" });
  assert.deepEqual(jobSpySummary(null), { boards: [], terms: [], location: "", freshness: "", results: "" });
  assert.equal(freshnessLabel(24), "Last 24 hours");
  assert.equal(freshnessLabel(36), "Last 36 hours");
  assert.equal(freshnessLabel(null), "");
});

test("relevance badge: HIGH green, REVIEW amber, REJECT gray; score 0–100", () => {
  assert.equal(relevanceTone("HIGH"), "completed");
  assert.equal(relevanceTone("review"), "cancelled");
  assert.equal(relevanceTone("REJECT"), "queued");
  assert.equal(relevanceTone(null), null);
  assert.equal(relevanceTone("maybe"), null);
  assert.equal(relevanceClass(" high "), "HIGH");
  assert.equal(relevanceScoreText(87.4), "87");
  assert.equal(relevanceScoreText(0), "0");
  assert.equal(relevanceScoreText(140), "100");
  assert.equal(relevanceScoreText(null), "");
  assert.equal(relevanceScoreText("x"), "");
});

test("relevance quick chips toggle a stable comma list", () => {
  assert.equal(toggleRelevance(undefined, "REVIEW"), "REVIEW");
  assert.equal(toggleRelevance("REVIEW", "HIGH"), "HIGH,REVIEW");
  assert.equal(toggleRelevance("HIGH,REVIEW", "HIGH"), "REVIEW");
  assert.equal(toggleRelevance("REVIEW", "REVIEW"), "");
  assert.deepEqual(parseRelevanceParam("reject,bogus,high"), ["HIGH", "REJECT"]);
});

test("source shows the board: JobSpy · Indeed", () => {
  assert.equal(sourceWithBoard("JobSpy", "Indeed"), "JobSpy · Indeed");
  assert.equal(sourceWithBoard("Greenhouse", null), "Greenhouse");
  assert.equal(sourceWithBoard("", "Indeed"), "Indeed");
  assert.equal(sourceWithBoard("Indeed", "indeed"), "Indeed");
  assert.equal(tableCell({ source: "JobSpy", source_board: "Indeed" }, "Source"), "JobSpy · Indeed");
  assert.equal(tableCell({ source: "Workday" }, "Source"), "Workday");
});
