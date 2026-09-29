import { test } from "node:test";
import assert from "node:assert/strict";
import { compileSteps, countSteps, describeHistory, describeStep, runNeeds, stepsFromGraph, type Step } from "../src/platform/logic/workflows.ts";

const tree: Step[] = [
  { kind: "action", action: { type: "create_task", title: "first" }, retries: 3, backoffSeconds: 30 },
  { kind: "delay", amount: 2, unit: "days" },
  {
    kind: "branch",
    conditions: { field: "company.industry", op: "eq", value: "Manufacturing" },
    then: [{ kind: "approval", message: "go?" }, { kind: "action", action: { type: "send_notification" } }],
    else: [],
  },
  { kind: "action", action: { type: "create_task", title: "last" } },
];

test("compileSteps links steps in order and joins branches", () => {
  const graph = compileSteps(tree);
  const nodes = graph.nodes!;
  const start = nodes[graph.start!];
  assert.equal(start.type, "action");
  assert.deepEqual(start.retry, { max_attempts: 3, backoff_seconds: 30 });
  const delay = nodes[start.next!];
  assert.equal(delay.type, "delay");
  assert.equal(delay.days, 2);
  const cond = nodes[delay.next!];
  assert.equal(cond.type, "condition");
  const last = Object.entries(nodes).find(([, n]) => n.action?.title === "last")![0];
  // the empty else arm goes straight to the step after the branch
  assert.equal(cond.else, last);
  const approval = nodes[cond.then!];
  assert.equal(approval.type, "approval");
  assert.equal(approval.on_reject, null);
  const notify = nodes[approval.next!];
  assert.equal(notify.next, last);
  assert.equal(nodes[last].next, null);
});

test("compileSteps keeps the tree and schedule, and empty trees compile to nothing", () => {
  const graph = compileSteps(tree, { every_minutes: 60 });
  assert.deepEqual(graph.ui, tree);
  assert.deepEqual(graph.schedule, { every_minutes: 60 });
  assert.deepEqual(compileSteps([]), {});
  assert.deepEqual(compileSteps([], { every_minutes: 30 }), { schedule: { every_minutes: 30 } });
});

test("stepsFromGraph round-trips through ui and walks plain graphs", () => {
  assert.deepEqual(stepsFromGraph(compileSteps(tree)), tree);
  const plain = { start: "a", nodes: { a: { type: "action" as const, action: { type: "export" }, next: "b" }, b: { type: "delay" as const, hours: 4 } } };
  assert.deepEqual(stepsFromGraph(plain), [
    { kind: "action", action: { type: "export" }, retries: undefined, backoffSeconds: undefined },
    { kind: "delay", unit: "hours", amount: 4 },
  ]);
  assert.deepEqual(stepsFromGraph(null), []);
});

test("descriptions", () => {
  assert.equal(countSteps(tree), 6);
  assert.equal(describeStep({ kind: "delay", amount: 1, unit: "days" }), "wait 1 day");
  assert.equal(describeStep(tree[0]), "create task");
  assert.equal(describeHistory({ type: "condition", status: "succeeded", result: { branch: "else" } }), "condition: succeeded → else");
  assert.equal(describeHistory({ type: "action", action: "webhook", status: "failed", attempt: 2, error: "HTTP 500" }), "webhook: failed (attempt 2) — HTTP 500");
  assert.equal(runNeeds("awaiting_approval"), "approval");
  assert.equal(runNeeds("waiting"), "timer");
  assert.equal(runNeeds("succeeded"), null);
});
