import { test } from "node:test";
import assert from "node:assert/strict";
import { factorFill, factorsTotal, formatCell, linePath, presetRange, reportQuery, stagePosition } from "../src/platform/logic/reports.ts";

test("formatCell never turns missing data into zero", () => {
  assert.equal(formatCell("reply_rate", null), "—");
  assert.equal(formatCell("won_value", undefined), "—");
  assert.equal(formatCell("reply_rate", 0.125), "12.5%");
  assert.equal(formatCell("conversion", 0), "0.0%");
  assert.equal(formatCell("sent", 1200), (1200).toLocaleString());
  assert.equal(formatCell("hits", ["a", "b"]), "a, b");
});

test("presetRange is inclusive and anchored to today", () => {
  const today = new Date(Date.UTC(2026, 8, 29));
  assert.deepEqual(presetRange("7d", today), { start: "2026-09-23", end: "2026-09-29" });
  assert.deepEqual(presetRange("qtd", today), { start: "2026-07-01", end: "2026-09-29" });
  assert.deepEqual(presetRange("ytd", today), { start: "2026-01-01", end: "2026-09-29" });
  assert.equal(presetRange("unknown", today).start, "2026-08-31");
});

test("reportQuery drops empty filters", () => {
  assert.deepEqual(reportQuery("2026-01-01", "", { campaign_id: " cp_1 ", kind: "" }), { start: "2026-01-01", campaign_id: "cp_1" });
});

test("score factors add up and fill their weight", () => {
  const factors = [
    { name: "a", weight: 40, value: 1, points: 20, reason: "" },
    { name: "b", weight: 60, value: 1, points: 60, reason: "" },
  ];
  assert.equal(factorsTotal(factors), 80);
  assert.equal(factorFill(factors[0]), 50);
  assert.equal(factorFill({ name: "z", weight: 0, value: null, points: 0, reason: "" }), 0);
});

test("linePath scales to the box", () => {
  assert.equal(linePath([], 100, 20, 5), "");
  assert.equal(linePath([0, 10], 100, 20, 10), "M0.0,18.0 L100.0,2.0");
});

test("buying stages are ordered", () => {
  assert.equal(stagePosition("unaware"), 1);
  assert.equal(stagePosition("customer"), 6);
  assert.equal(stagePosition(null), 0);
});
