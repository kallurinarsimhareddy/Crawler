import assert from "node:assert/strict";
import { test } from "node:test";
import {
  IMPORT_COUNTERS, currentRowCount, exportFilename, exportFinished, exportParams, exportPercent, importPercent,
} from "../src/platform/logic/jobCsv.ts";
import { IMPORT_FIELDS, checkMapping, normalizeMapping } from "../src/platform/logic/jobFields.ts";

test("export params keep the active filters and drop paging", () => {
  const params = exportParams({ source: "WeAreDevelopers", relevance: "HIGH", order: "-first_seen_at", q: "" } as never, null);
  assert.deepEqual(params, { source: "WeAreDevelopers", relevance: "HIGH" });
  const tree = { op: "all", rules: [] };
  assert.deepEqual(exportParams({} as never, tree), { conditions: tree });
  assert.deepEqual(exportParams({} as never), {});
});

test("current results are the visible page", () => {
  assert.equal(currentRowCount(120, { limit: 50, offset: 0 }), 50);
  assert.equal(currentRowCount(120, { limit: 50, offset: 100 }), 20);
  assert.equal(currentRowCount(0, { limit: 50, offset: 0 }), 0);
  assert.equal(currentRowCount(null, { limit: 50, offset: 0 }), 0);
});

test("export progress and state", () => {
  assert.equal(exportPercent({ id: "x", status: "running", total_rows: 200, progress_rows: 50 }), 25);
  assert.equal(exportPercent({ id: "x", status: "running", total_rows: 200, progress_rows: 200 }), 99);
  assert.equal(exportPercent({ id: "x", status: "completed" }), 100);
  assert.equal(exportPercent(null), 0);
  assert.equal(exportFinished({ id: "x", status: "failed" }), true);
  assert.equal(exportFinished({ id: "x", status: "queued" }), false);
  assert.equal(exportFilename({ id: "x", scope: "all" }), "jobs-all.csv");
  assert.equal(exportFilename({ id: "x", filename: "jobs-current-1.csv" }), "jobs-current-1.csv");
});

test("import summary counters and progress", () => {
  assert.deepEqual(IMPORT_COUNTERS.map(([k]) => k), ["rows", "new", "updated", "unchanged", "duplicates", "rejected", "errors"]);
  assert.equal(importPercent({ percent: 62 }, 1000, 620, "importing"), 62);
  assert.equal(importPercent({}, 1000, 250, "importing"), 25);
  assert.equal(importPercent({ percent: 40 }, 1000, 400, "completed"), 100);
});

test("upload mapping covers source board and search term", () => {
  assert.ok(IMPORT_FIELDS.includes("Source Board") && IMPORT_FIELDS.includes("Search Term"));
  const headers = ["job_url", "job_title", "source_board", "search_term"];
  const mapping = normalizeMapping({ "Job URL": "job_url", "Job Title": "job_title", "Source Board": "source_board",
    "Search Term": "search_term", "Location": "missing" }, headers);
  assert.equal(mapping["Search Term"], "search_term");
  assert.equal(mapping.Location, null);
  assert.equal(checkMapping(mapping, headers).ok, true);
  assert.equal(checkMapping(mapping, headers).mapped, 4);
});
