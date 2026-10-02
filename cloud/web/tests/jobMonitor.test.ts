import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CHANGE_OPTIONS, JOB_FIELDS, JOB_TABLE_COLUMNS, STATUS_OPTIONS, changeTone, checkMapping, closureReasonText, display, fieldValues, goneCheckText,
  historyDetails, httpStatusText, isBlank, jobClosureText, joinKeywords, keywordsOf, normalizeMapping, safeJobUrl, statusLabel, statusTone, tableCell, urlLabel,
} from "../src/platform/logic/jobFields.ts";
import {
  CHANGE_FILTERS, CONDITION_FIELDS, ORDER_OPTIONS, buildConditions, clearScope, deepLink, hasScope, jobsLink, newMonitorLink, parseConditionValue, parseConditionsParam,
  STATE_CHANGES, parseFilters, parsePage, scopeBanner, searchBody, toQueryString, validateConditions, withFilter, type Group,
} from "../src/platform/logic/jobFilters.ts";

// --- fields --------------------------------------------------------------------------------

test("the 14 mandatory fields, in import order, and the 22 table columns", () => {
  assert.equal(JOB_FIELDS.length, 14);
  assert.equal(JOB_FIELDS[0], "Job URL");
  assert.equal(JOB_FIELDS[13], "Scraped Date");
  assert.deepEqual([...JOB_TABLE_COLUMNS], [
    "Job Title", "Company", "Location", "Experience", "Salary", "Keywords", "Remote", "Source", "Board", "Search Term",
    "Scraped Date", "First Seen", "Last Seen", "Last Changed", "Stale Date", "Closed Date", "Status", "New/Changed",
    "Relevance", "Score", "Reason", "Job URL",
  ]);
  assert.equal(new Set(JOB_TABLE_COLUMNS).size, JOB_TABLE_COLUMNS.length);
});

test("table cells: dates are date-only, relevance class and integer score are separate", () => {
  const job = {
    first_seen_at: "2026-09-28T14:03:11.512Z", last_changed_at: null, status_label: "CLOSED", change_badge: "Changed",
    relevance_class: "review", relevance_score: 72.6,
  };
  assert.equal(tableCell(job, "First Seen"), "2026-09-28");
  assert.equal(tableCell(job, "Last Changed"), "");
  assert.equal(tableCell(job, "Status"), "CLOSED");
  assert.equal(tableCell(job, "New/Changed"), "Changed");
  assert.equal(tableCell(job, "Relevance"), "REVIEW");
  assert.equal(tableCell(job, "Score"), "73");
  assert.equal(tableCell({ relevance_score: null, relevance_class: "bogus" }, "Score"), "");
  assert.equal(tableCell({ relevance_score: null, relevance_class: "bogus" }, "Relevance"), "");
  assert.equal(tableCell({ relevance_score: 0 }, "Score"), "0");
});

test("the original-job link is only ever an http(s) job_url, unchanged", () => {
  assert.equal(safeJobUrl("https://boards.greenhouse.io/acme/jobs/123?gh_src=x"), "https://boards.greenhouse.io/acme/jobs/123?gh_src=x");
  assert.equal(safeJobUrl("http://jobs.example.com/1"), "http://jobs.example.com/1");
  assert.equal(safeJobUrl("javascript:alert(1)"), null);
  assert.equal(safeJobUrl("JaVaScRiPt:alert(1)"), null);
  assert.equal(safeJobUrl("data:text/html,hi"), null);
  assert.equal(safeJobUrl("/jobs/123"), null);
  assert.equal(safeJobUrl("ftp://example.com/x"), null);
  assert.equal(safeJobUrl(""), null);
  assert.equal(safeJobUrl(null), null);
  assert.equal(safeJobUrl(undefined), null);
  assert.equal(urlLabel("https://www.example.com/careers/42/"), "example.com/careers/42");
  assert.equal(urlLabel("javascript:alert(1)"), "javascript:alert(1)");
});

test("blank fields render blank — nothing is invented", () => {
  assert.equal(display(null), "");
  assert.equal(display(undefined), "");
  assert.equal(display("  "), "");
  assert.ok(isBlank(null));
  const job = { id: "j1", job_url: "https://x.test/1", title: "SAP Analyst", company_name: null, location: null, salary_budget: null, keyword_1: null, remote: null, status: "open" };
  assert.equal(tableCell(job, "Company"), "");
  assert.equal(tableCell(job, "Salary"), "");
  assert.equal(tableCell(job, "Keywords"), "");
  assert.equal(tableCell(job, "Remote"), "");
  assert.equal(tableCell(job, "New/Changed"), "");
  const values = fieldValues(job);
  assert.equal(values["Company Name"], "");
  assert.equal(values["Keyword 5"], "");
  assert.equal(values["Job Title"], "SAP Analyst");
});

test("keywords join in source order; status labels map open/closed/unknown", () => {
  assert.equal(joinKeywords({ keywords: ["SAP", "ABAP", "S/4HANA"] }), "SAP, ABAP, S/4HANA");
  assert.deepEqual(keywordsOf({ keyword_1: "Java", keyword_2: null, keyword_3: "Spring" }), ["Java", "Spring"]);
  assert.equal(statusLabel({ status: "open" }), "ACTIVE");
  assert.equal(statusLabel({ status: "closed" }), "CLOSED");
  assert.equal(statusLabel({ status: "unknown" }), "UNKNOWN");
  assert.equal(statusLabel({ status: "open", status_label: "ACTIVE" }), "ACTIVE");
  assert.equal(statusLabel({}), "");
});

test("fields object from the API wins over raw columns", () => {
  const values = fieldValues({ title: "raw", fields: { "Job Title": "Labelled", "Keyword 1": "SQL" } });
  assert.equal(values["Job Title"], "Labelled");
  assert.equal(values["Keyword 1"], "SQL");
});

test("an import mapping needs Job URL and Job Title", () => {
  const headers = ["Link", "Position", "Employer"];
  assert.deepEqual(checkMapping({ "Job Title": "Position" }, headers).missing, ["Job URL"]);
  assert.equal(checkMapping({ "Job URL": "Link" }, headers).ok, false);
  assert.equal(checkMapping({ "Job URL": "Link", "Job Title": "Position" }, headers).ok, true);
  // A header the file does not have does not count.
  assert.equal(checkMapping({ "Job URL": "URL", "Job Title": "Position" }, headers).ok, false);
  assert.deepEqual(checkMapping({ "Job URL": "Link", "Job Title": "Link" }, headers).reused, ["Link"]);
  const full = normalizeMapping({ "Job URL": "Link", "Company Name": "Employer", "Location": "Nope" }, headers);
  assert.equal(Object.keys(full).length, 14);
  assert.equal(full["Job URL"], "Link");
  assert.equal(full["Location"], null);
  assert.equal(full["Job Title"], null);
});

// --- filters -------------------------------------------------------------------------------

test("filters round-trip through the query string", () => {
  const filters = { title: "SAP", remote: "Hybrid", status: "ACTIVE", scraped_from: "2026-09-01", keyword: "ABAP", order: "-last_changed_at" };
  const qs = toQueryString(filters);
  assert.deepEqual(parseFilters(qs), filters);
  assert.deepEqual(parseFilters("?" + qs), filters);
  assert.equal(toQueryString({}), "");
  assert.equal(toQueryString({ title: "  " }), "");
});

test("unknown keys and blank values are dropped; paging is bounded", () => {
  assert.deepEqual(parseFilters("title=&evil=1&company=Acme"), { company: "Acme" });
  assert.deepEqual(parseFilters("change=bogus"), {});
  assert.deepEqual(parseFilters("change=UNCHANGED"), { change: "unchanged" });
  assert.deepEqual(parsePage("offset=100&limit=9999"), { limit: 500, offset: 100 });
  assert.deepEqual(parsePage("offset=-5&limit=abc"), { limit: 50, offset: 0 });
  assert.equal(toQueryString({ title: "x" }, { offset: 50, limit: 50 }), "title=x&offset=50");
  assert.deepEqual(withFilter({ title: "a" }, "title", ""), {});
});

test("deep links from notifications and chat (?monitor&run&change) are understood", () => {
  const filters = parseFilters("monitor=jm_1&run=jr_9&change=new");
  assert.deepEqual(deepLink(filters), { monitor: "jm_1", run: "jr_9", change: "new", importId: null, sinceLastRun: false, since: null });
  assert.ok(hasScope(filters));
  assert.equal(scopeBanner(filters, 50, "Acme careers"), "Showing 50 new jobs from run jr_9 of Acme careers");
  assert.equal(scopeBanner(parseFilters("monitor=jm_1&since_last_run=1"), 3, "Acme"), "Showing 3 new jobs from the last run of Acme");
  assert.equal(scopeBanner(parseFilters("monitor=jm_1"), 1, null), "Showing 1 job from monitor jm_1");
  assert.equal(scopeBanner(parseFilters("change=closed"), 7), "Showing 7 closed jobs");
  assert.deepEqual(clearScope({ ...filters, title: "SAP" }), { title: "SAP" });
  assert.equal(jobsLink({ monitor: "jm_1", run: "jr_9", change: "changed" }), "/jobs?change=changed&monitor=jm_1&run=jr_9");
  assert.deepEqual(parseFilters(jobsLink({ monitor: "m", run: "r", change: "new" }).split("?")[1]), { monitor: "m", run: "r", change: "new" });
  assert.deepEqual(deepLink(filters).since, null);
  assert.equal(newMonitorLink("https://acme.com/careers?a=1&b=2"), "/monitors?new=https%3A%2F%2Facme.com%2Fcareers%3Fa%3D1%26b%3D2");
});

test("change=unchanged round-trips like the other change scopes", () => {
  assert.deepEqual([...CHANGE_FILTERS], ["new", "changed", "unchanged", "stale", "expired", "closed", "reopened"]);
  assert.deepEqual([...CHANGE_OPTIONS], [...CHANGE_FILTERS]);
  for (const change of CHANGE_FILTERS) {
    const link = jobsLink({ monitor: "jm_1", change });
    assert.deepEqual(parseFilters(link.split("?")[1]), { monitor: "jm_1", change });
  }
  const filters = parseFilters("change=unchanged&monitor=jm_1&run=jr_9");
  assert.equal(toQueryString(filters), "change=unchanged&monitor=jm_1&run=jr_9");
  assert.equal(deepLink(filters).change, "unchanged");
  assert.ok(hasScope(filters));
  assert.equal(scopeBanner(filters, 12, "Acme"), "Showing 12 unchanged jobs from run jr_9 of Acme");
  assert.equal(scopeBanner(parseFilters("change=unchanged&monitor=jm_1"), 4, "Acme"), "Showing 4 unchanged jobs from the last run of Acme");
  assert.equal(searchBody(filters, null, { limit: 50, offset: 0 }).change, "unchanged");
  assert.deepEqual(clearScope(filters), {});
});

// --- conditions ----------------------------------------------------------------------------

test("condition trees: AND/OR groups, validation, and the cleaned body", () => {
  const tree: Group = {
    all: [
      { field: "title", op: "contains", value: "SAP" },
      { any: [{ field: "remote", op: "eq", value: "Remote" }, { field: "location", op: "contains", value: "Texas" }] },
      { field: "salary", op: "not_empty" },
      { field: "keyword", op: "in", value: parseConditionValue("in", "ABAP, Fiori,") },
    ],
  };
  assert.deepEqual(validateConditions(tree), []);
  assert.deepEqual(buildConditions(tree), {
    all: [
      { field: "title", op: "contains", value: "SAP" },
      { any: [{ field: "remote", op: "eq", value: "Remote" }, { field: "location", op: "contains", value: "Texas" }] },
      { field: "salary", op: "not_empty" },
      { field: "keyword", op: "in", value: ["ABAP", "Fiori"] },
    ],
  });
  const bad: Group = { any: [{ field: "title", op: "contains", value: "" }, { field: "nope", op: "eq", value: "x" }, { all: [] }] };
  assert.equal(validateConditions(bad).length, 2);
  assert.equal(buildConditions(bad), null);
  const body = searchBody({ title: "SAP", monitor: "jm_1" }, tree, { limit: 50, offset: 100 });
  assert.equal(body.title, "SAP");
  assert.equal(body.monitor, "jm_1");
  assert.equal(body.limit, 50);
  assert.equal(body.offset, 100);
  assert.equal(body.order, "-first_seen_at");
  assert.ok(body.conditions);
  assert.equal(searchBody({}, { all: [] }, { limit: 50, offset: 0 }).conditions, undefined);
  assert.deepEqual(parseConditionsParam(JSON.stringify(tree)), tree);
  assert.equal(parseConditionsParam("{bad"), null);
  assert.equal(parseConditionsParam('{"x":1}'), null);
});

// --- relevance / JobSpy filters ------------------------------------------------------------

test("relevance, category, board and search-term filters round-trip through the URL", () => {
  const filters = { relevance: "HIGH,REVIEW", relevance_min: "60", category: "ERP", source_board: "Indeed", search_term: "SAP", order: "-relevance_score" };
  const qs = toQueryString(filters);
  assert.deepEqual(parseFilters(qs), filters);
  assert.equal(new URLSearchParams(qs).get("relevance"), "HIGH,REVIEW");
  // normalised: upper-cased, unknown classes dropped, stable order
  assert.deepEqual(parseFilters("relevance=review,high,bogus"), { relevance: "HIGH,REVIEW" });
  assert.deepEqual(parseFilters("relevance=bogus"), {});
  assert.deepEqual(parseFilters("relevance_min=abc"), {});
  assert.deepEqual(parseFilters("relevance_min=150"), {});
  assert.deepEqual(parseFilters("relevance_min=0"), { relevance_min: "0" });
  assert.ok((ORDER_OPTIONS as readonly string[]).includes("-relevance_score"));
  const body = searchBody(filters, null, { limit: 50, offset: 0 });
  assert.equal(body.relevance, "HIGH,REVIEW");
  assert.equal(body.relevance_min, "60");
  assert.equal(body.source_board, "Indeed");
  assert.equal(body.order, "-relevance_score");
});

test("advanced conditions accept relevance_score, relevance, source_board, search_term", () => {
  for (const field of ["relevance_score", "relevance", "source_board", "search_term"]) assert.ok((CONDITION_FIELDS as readonly string[]).includes(field), field);
  const tree: Group = { all: [{ field: "relevance_score", op: "gte", value: "70" }, { field: "relevance", op: "in", value: ["HIGH", "REVIEW"] }, { field: "source_board", op: "eq", value: "Indeed" }, { field: "search_term", op: "contains", value: "SAP" }] };
  assert.deepEqual(validateConditions(tree), []);
  assert.deepEqual(buildConditions(tree), tree);
});

// --- lifecycle (STALE / EXPIRED / CLOSED reasons, migration 0013) ---------------------------------

test("lifecycle statuses: labels, filter options and badge tones", () => {
  assert.deepEqual([...STATUS_OPTIONS], ["ACTIVE", "STALE", "EXPIRED", "CLOSED", "UNKNOWN"]);
  assert.deepEqual([...CHANGE_OPTIONS], [...CHANGE_FILTERS]);
  assert.equal(statusLabel({ status: "stale" }), "STALE");
  assert.equal(statusLabel({ status: "expired" }), "EXPIRED");
  assert.equal(statusLabel({ status: "open" }), "ACTIVE");
  assert.equal(statusLabel({ status_label: "expired", status: "closed" }), "EXPIRED");
  assert.deepEqual(["ACTIVE", "STALE", "EXPIRED", "CLOSED", "UNKNOWN"].map(statusTone), ["running", "cancelled", "expired", "failed", "queued"]);
  assert.equal(new Set(["ACTIVE", "STALE", "EXPIRED", "CLOSED"].map(statusTone)).size, 4);
  assert.equal(changeTone("Stale"), "cancelled");
  assert.equal(changeTone("Expired"), "expired");
  assert.equal(changeTone("New"), "completed");
});

test("lifecycle table cells: last seen, stale date, closed date, board, search term, reason", () => {
  const job = {
    last_seen_at: "2026-10-01T19:00:00Z", stale_at: "2026-10-02T06:00:00Z", closed_at: null, source_board: "Indeed",
    search_term: "SAP", relevance_reason: "title keyword SAP",
  };
  assert.equal(tableCell(job, "Last Seen"), "2026-10-01");
  assert.equal(tableCell(job, "Stale Date"), "2026-10-02");
  assert.equal(tableCell(job, "Closed Date"), "");
  assert.equal(tableCell(job, "Board"), "Indeed");
  assert.equal(tableCell(job, "Search Term"), "SAP");
  assert.equal(tableCell(job, "Reason"), "title keyword SAP");
});

test("closure reasons in words, only for closed jobs", () => {
  assert.equal(closureReasonText("missed_full_sweeps", { missed: 2 }), "Missing from 2 completed full sweeps");
  assert.equal(closureReasonText("missed_full_sweeps", { missed: 1 }), "Missing from 1 completed full sweep");
  assert.equal(closureReasonText("missed_full_sweeps"), "Missing from consecutive completed full sweeps");
  assert.equal(closureReasonText("source_gone", { httpStatus: 410 }), "Removed at the source (HTTP 410)");
  assert.equal(closureReasonText("source_gone"), "Removed at the source");
  assert.equal(closureReasonText("manual"), "Closed manually");
  assert.equal(closureReasonText(null), "");
  assert.equal(jobClosureText({ status: "closed", closure_reason: "source_gone", gone_status: 410 }), "Removed at the source (HTTP 410)");
  assert.equal(jobClosureText({ status: "open", closure_reason: "source_gone" }), "");
  assert.equal(httpStatusText(410), "HTTP 410 (gone)");
  assert.equal(httpStatusText(200), "HTTP 200 (still live)");
  assert.equal(httpStatusText(null), "");
  assert.equal(goneCheckText({ gone_checked_at: "2026-10-02T15:00:00Z", gone_status: 410 }), "2026-10-02 · HTTP 410 (gone)");
  assert.equal(goneCheckText({}), "");
});

test("history details for stale, expired, closed and reopened entries", () => {
  assert.deepEqual(historyDetails({ change: "stale", after: { status: "STALE", active_since: "2026-08-01T00:00:00+00:00", stale_after_days: 30 } }),
    ["Still active since 2026-08-01 — more than 30 days"]);
  assert.deepEqual(historyDetails({ change: "expired", after: { status: "EXPIRED", listing_date: "2026-06-29", reason: "older than the source's 90-day listing window" } }),
    ["Expired: older than the source's 90-day listing window", "Last listing date 2026-06-29"]);
  assert.deepEqual(historyDetails({ change: "closed", after: { status: "CLOSED", reason: "source_gone", http_status: 410 } }), ["Removed at the source (HTTP 410)"]);
  assert.deepEqual(historyDetails({ change: "closed", after: { status: "CLOSED", missed_full_sweeps: 2 } }), ["Missing from 2 completed full sweeps"]);
  assert.deepEqual(historyDetails({ change: "reopened", before: { status: "CLOSED", closure_reason: "missed_full_sweeps" } }),
    ["Seen again after it was CLOSED (Missing from consecutive completed full sweeps)"]);
  assert.deepEqual(historyDetails({ change: "changed", changed_fields: ["salary_budget"] }), []);
});

test("stale / expired deep links from notifications round-trip and explain their scope", () => {
  for (const change of ["stale", "expired"]) {
    const link = jobsLink({ monitor: "jm_1", change, since: "2026-10-02" });
    assert.equal(link, `/jobs?change=${change}&since=2026-10-02&monitor=jm_1`);
    assert.deepEqual(parseFilters(link.split("?")[1]), { monitor: "jm_1", change, since: "2026-10-02" });
  }
  assert.ok(STATE_CHANGES.includes("stale") && STATE_CHANGES.includes("expired"));
  assert.equal(scopeBanner(parseFilters("monitor=jm_1&change=stale&since=2026-10-02"), 12, "WAD"), "Showing 12 stale jobs of WAD since 2026-10-02");
  assert.equal(scopeBanner(parseFilters("monitor=jm_1&change=stale"), 5, "WAD"), "Showing 5 stale jobs of WAD");
  assert.equal(scopeBanner(parseFilters("monitor=jm_1&run=jr_9&change=expired"), 3, "WAD"), "Showing 3 expired jobs from run jr_9 of WAD");
  assert.equal(scopeBanner(parseFilters("change=expired"), 1), "Showing 1 expired job");
  assert.equal(scopeBanner(parseFilters("change=closed&since=2026-09-30"), 4), "Showing 4 closed jobs since 2026-09-30");
  assert.equal(scopeBanner(parseFilters("monitor=jm_1&change=closed"), 2, "WAD"), "Showing 2 closed jobs from the last run of WAD");
  assert.deepEqual(parseFilters("change=stale&since=garbage"), { change: "stale" });
  assert.deepEqual(parseFilters("since=2026-10-02T06:00:00Z"), { since: "2026-10-02" });
  assert.deepEqual(clearScope(parseFilters("change=stale&since=2026-10-02&monitor=m&title=SAP")), { title: "SAP" });
  assert.equal(searchBody(parseFilters("change=expired&since=2026-10-01"), null, { limit: 50, offset: 0 }).since, "2026-10-01");
});
