import { test } from "node:test";
import assert from "node:assert/strict";
import { cadenceDays, delaysFromDays, moveStep, parseBulkValues, providerState, ratio, scheduleSummary, validateSteps } from "../src/platform/logic/sending.ts";

test("cadence days round-trip Day 1/3/6/10", () => {
  assert.deepEqual(cadenceDays([0, 2, 3, 4]), [1, 3, 6, 10]);
  assert.deepEqual(delaysFromDays([1, 3, 6, 10]), [0, 2, 3, 4]);
  assert.deepEqual(cadenceDays([]), []);
});

test("bulk suppression parsing de-duplicates and flags invalid values", () => {
  const { values, invalid } = parseBulkValues("A@x.com\na@x.com; @blocked.example, not valid,bob@y.org");
  assert.deepEqual(values, ["a@x.com", "@blocked.example", "bob@y.org"]);
  assert.deepEqual(invalid, ["not valid"]);
});

test("provider cards never claim a connection the server cannot offer", () => {
  const off = providerState({ provider: "google", configured: false, missing: ["CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID"], auth: "oauth" }, 3);
  assert.equal(off.label, "Not configured");
  assert.match(off.detail, /CLIENT_ID/);
  assert.equal(providerState({ provider: "api", configured: true, missing: [], auth: "api_key" }, 2).label, "2 connected");
  assert.equal(providerState({ provider: "google", configured: true, missing: [], auth: "oauth" }, 0).label, "Available");
});

test("ratio shows a dash when a provider does not report", () => {
  assert.equal(ratio(null), "—");
  assert.equal(ratio(0.125), "12.5%");
});

test("schedule summary", () => {
  assert.equal(scheduleSummary({}), "Any time");
  assert.equal(scheduleSummary({ days: [1, 2, 3, 4, 5], start_hour: 9, end_hour: 17, timezone: "UTC", daily_cap: 100 }), "Mon–Fri 9:00–17:00 UTC · 100/day");
  assert.equal(scheduleSummary({ days: [1, 3] }), "Mon, Wed");
});

test("step validation and reordering", () => {
  assert.deepEqual(validateSteps([{ channel: "email", delay_days: 0, step_type: "initial", template_id: null }]), ["Step 1: choose a template."]);
  assert.deepEqual(validateSteps([{ channel: "wait", delay_days: 2, step_type: "wait" }]), []);
  assert.deepEqual(moveStep(["a", "b", "c"], 2, 1), ["a", "b", "c"]);
  assert.deepEqual(moveStep(["a", "b", "c"], 1, -1), ["b", "a", "c"]);
});
