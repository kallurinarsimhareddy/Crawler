import { test } from "node:test";
import assert from "node:assert/strict";
import { auditQuery, canAdmin, canManage, canWrite, describeChanges, parseEvents, parseIds, roleLabel, toSearch } from "../src/platform/logic/admin.ts";

test("role labels and rights", () => {
  assert.equal(roleLabel("member"), "User");
  assert.equal(roleLabel("viewer"), "Read-only");
  assert.equal(roleLabel(undefined), "—");
  assert.equal(canWrite("viewer"), false);
  assert.equal(canWrite("manager"), true);
  assert.equal(canManage("member"), false);
  assert.equal(canManage("manager"), true);
  assert.equal(canAdmin("manager"), false);
  assert.equal(canAdmin("owner"), true);
});

test("parseIds splits and dedupes", () => {
  assert.deepEqual(parseIds("co_1, co_2\nco_1  co_3;"), ["co_1", "co_2", "co_3"]);
  assert.deepEqual(parseIds("   "), []);
});

test("auditQuery keeps filled filters and validates dates", () => {
  assert.deepEqual(auditQuery({ action: " session ", actor: "" }, " x ").query, { action: "session", q: "x" });
  assert.equal(auditQuery({ from: "yesterday" }).error, "From must be a date like 2026-09-30");
  assert.equal(auditQuery({ from: "2026-09-30", to: "2026-09-01" }).error, "From is after To");
  assert.equal(auditQuery({ from: "2026-09-01", to: "2026-09-30" }).error, null);
  assert.equal(toSearch({ action: "a b", from: "2026-01-01" }), "?action=a+b&from=2026-01-01");
  assert.equal(toSearch({}), "");
});

test("describeChanges and parseEvents", () => {
  assert.deepEqual(describeChanges({ role: "admin", nested: { a: 1 } }), ["role: admin", 'nested: {"a":1}']);
  assert.deepEqual(describeChanges(null), []);
  assert.equal(describeChanges({ long: "x".repeat(300) })[0].length, 207);
  assert.deepEqual(parseEvents("test, notification,,"), ["test", "notification"]);
});
