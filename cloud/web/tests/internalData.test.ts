import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildMapping,
  conflictPayload,
  initialDecisions,
  previewLine,
  resultLink,
  schemaMatrix,
  sharedTargets,
  undecided,
  type ReviewColumn,
} from "../src/platform/logic/internalData.ts";

const columns: ReviewColumn[] = [
  { column: "Company Name", suggestion: "company.name", confidence: 0.95, status: "confident", reasons: [], alternatives: [] },
  { column: "Name", suggestion: "company.name", confidence: 0.5, status: "ambiguous", reasons: ["ambiguous"], alternatives: ["contact.full_name"] },
  { column: "Owner", suggestion: null, confidence: 0, status: "unmapped", reasons: [], alternatives: [] },
];

test("confident columns are pre-filled, ambiguous ones stay undecided", () => {
  const d = initialDecisions(columns);
  assert.equal(d["Company Name"], "company.name");
  assert.equal(d["Name"], undefined);
  assert.equal(d["Owner"], null);
  assert.deepEqual(undecided(columns, d), ["Name"]);
});

test("an explicit 'not mapped' decides an ambiguous column", () => {
  const d = { ...initialDecisions(columns), Name: null };
  assert.deepEqual(undecided(columns, d), []);
  assert.deepEqual(buildMapping(d), { "Company Name": "company.name", Name: null, Owner: null });
});

test("the stored mapping wins over suggestions", () => {
  const d = initialDecisions(columns, { Name: "contact.full_name" });
  assert.equal(d["Name"], "contact.full_name");
});

test("undecided columns are never sent", () => {
  assert.deepEqual(buildMapping({ A: undefined, B: "company.domain" }), { B: "company.domain" });
});

test("shared targets flag single-valued fields only", () => {
  assert.deepEqual(sharedTargets({ A: "company.name", B: "company.name", C: "company.tags", D: "company.tags" }), { "company.name": ["A", "B"] });
});

test("schema matrix matches columns across files", () => {
  const rows = schemaMatrix(
    [{ key: "companyname", column: "Company Name", files_present: 1, files_total: 2, type_hint: "text", variants: [] }],
    [
      { file_id: "a", filename: "a.csv", present: ["Company name"] },
      { file_id: "b", filename: "b.csv", present: ["Account"] },
    ],
  );
  assert.deepEqual(rows[0].cells, [true, false]);
  assert.equal(rows[0].coverage, "1/2");
});

test("conflict payload skips undecided and rejects blank manual values", () => {
  const { decisions, invalid } = conflictPayload({ "company.industry": "take_new", "company.city": undefined, "company.state": { value: " " } });
  assert.deepEqual(decisions, { "company.industry": "take_new" });
  assert.deepEqual(invalid, ["company.state"]);
});

test("preview line and result links", () => {
  assert.match(previewLine({ companies: 2, companies_existing: 1, companies_new: 1, contacts: 3 }), /2 companies \(1 in CRM, 1 new\)/);
  assert.deepEqual(resultLink("create-campaign", { campaign: { id: "cp_1" } }), { to: "/campaigns/cp_1", label: "Open draft campaign" });
  assert.deepEqual(resultLink("validate-emails", { job: null, task: { id: "tsk_1" } }), { to: "/background", label: "Open background job" });
  assert.equal(resultLink("add-to-list", {}), null);
});
