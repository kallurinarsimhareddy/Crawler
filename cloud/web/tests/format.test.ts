import { test } from "node:test";
import assert from "node:assert/strict";
import { fileSize, percent } from "../src/platform/logic/format.ts";

test("percent handles empty totals", () => {
  assert.equal(percent(1, 0), "—");
  assert.equal(percent(1, 8), "12.5%");
});

test("fileSize scales units", () => {
  assert.equal(fileSize(999), "999 B");
  assert.equal(fileSize(1536), "1.5 KB");
  assert.equal(fileSize(3 * 1024 * 1024), "3.0 MB");
});
