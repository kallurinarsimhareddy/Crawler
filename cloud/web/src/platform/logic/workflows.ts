// Workflow builder logic: the editable step tree <-> the engine's graph.
// The builder edits a nested list of steps (if/else branches hold their own
// lists); compileSteps() turns it into the engine graph (cloud/intel/automation/
// graph.py) and keeps the tree in graph.ui so the same tree can be edited again.

export type Condition = Record<string, unknown>;
export type ActionConfig = Record<string, unknown> & { type: string };

export type Step =
  | { kind: "action"; action: ActionConfig; retries?: number; backoffSeconds?: number }
  | { kind: "delay"; amount: number; unit: "minutes" | "hours" | "days" }
  | { kind: "approval"; message: string }
  | { kind: "branch"; conditions: Condition; then: Step[]; else: Step[] };

export interface GraphNode {
  type: "condition" | "action" | "delay" | "approval" | "end";
  next?: string | null;
  then?: string | null;
  else?: string | null;
  on_reject?: string | null;
  conditions?: Condition;
  action?: ActionConfig;
  retry?: { max_attempts: number; backoff_seconds: number };
  message?: string;
  minutes?: number;
  hours?: number;
  days?: number;
}

export interface Graph {
  start?: string;
  nodes?: Record<string, GraphNode>;
  schedule?: { every_minutes: number };
  ui?: Step[];
}

/** Compile a step tree into an engine graph. Empty steps compile to an empty graph. */
export function compileSteps(steps: Step[], schedule?: { every_minutes: number }): Graph {
  const nodes: Record<string, GraphNode> = {};
  let counter = 0;
  const id = (prefix: string) => `${prefix}${++counter}`;

  // Compile a list back to front, so each step knows the step after it.
  const compileList = (list: Step[], after: string | null): string | null => {
    let next = after;
    for (let i = list.length - 1; i >= 0; i--) {
      const step = list[i];
      if (step.kind === "action") {
        const nodeId = id("a");
        const node: GraphNode = { type: "action", action: step.action, next };
        if (step.retries && step.retries > 1) node.retry = { max_attempts: step.retries, backoff_seconds: step.backoffSeconds ?? 60 };
        nodes[nodeId] = node;
        next = nodeId;
      } else if (step.kind === "delay") {
        const nodeId = id("d");
        nodes[nodeId] = { type: "delay", [step.unit]: step.amount, next };
        next = nodeId;
      } else if (step.kind === "approval") {
        const nodeId = id("p");
        nodes[nodeId] = { type: "approval", message: step.message, next, on_reject: null };
        next = nodeId;
      } else {
        const nodeId = id("c");
        const thenStart = compileList(step.then, next);
        const elseStart = compileList(step.else, next);
        nodes[nodeId] = { type: "condition", conditions: step.conditions, then: thenStart, else: elseStart };
        next = nodeId;
      }
    }
    return next;
  };

  const start = compileList(steps, null);
  if (!start) return schedule ? { schedule } : {};
  const graph: Graph = { start, nodes, ui: steps };
  if (schedule) graph.schedule = schedule;
  return graph;
}

/** The editable tree for a saved graph: its ui copy, else a best-effort linear walk. */
export function stepsFromGraph(graph: Graph | null | undefined): Step[] {
  if (!graph) return [];
  if (Array.isArray(graph.ui)) return graph.ui;
  const nodes = graph.nodes ?? {};
  const out: Step[] = [];
  const seen = new Set<string>();
  let cursor = graph.start ?? null;
  while (cursor && nodes[cursor] && !seen.has(cursor)) {
    seen.add(cursor);
    const node = nodes[cursor];
    if (node.type === "action" && node.action) {
      out.push({ kind: "action", action: node.action, retries: node.retry?.max_attempts, backoffSeconds: node.retry?.backoff_seconds });
    } else if (node.type === "delay") {
      const unit = node.days ? "days" : node.hours ? "hours" : "minutes";
      out.push({ kind: "delay", unit, amount: Number(node[unit] ?? 1) });
    } else if (node.type === "approval") {
      out.push({ kind: "approval", message: node.message ?? "" });
    } else if (node.type === "condition") {
      // A branch without the builder's tree: keep the two arms as single chains.
      out.push({ kind: "branch", conditions: node.conditions ?? {}, then: stepsFromGraph({ start: node.then ?? undefined, nodes }), else: stepsFromGraph({ start: node.else ?? undefined, nodes }) });
      break;
    }
    cursor = node.next ?? null;
  }
  return out;
}

/** Count the steps of a tree (branches included), for list views. */
export function countSteps(steps: Step[]): number {
  return steps.reduce((sum, s) => sum + 1 + (s.kind === "branch" ? countSteps(s.then) + countSteps(s.else) : 0), 0);
}

/** Short text for one step. */
export function describeStep(step: Step): string {
  switch (step.kind) {
    case "action":
      return step.action.type.replace(/_/g, " ");
    case "delay":
      return `wait ${step.amount} ${step.amount === 1 ? step.unit.replace(/s$/, "") : step.unit}`;
    case "approval":
      return "approval";
    case "branch":
      return "if / else";
  }
}

export interface HistoryEntry {
  node?: string;
  type?: string;
  action?: string;
  status?: string;
  attempt?: number;
  error?: string;
  result?: unknown;
  at?: string;
}

/** One readable line per run-history entry. */
export function describeHistory(entry: HistoryEntry): string {
  const label = entry.action ? entry.action.replace(/_/g, " ") : entry.type ?? "step";
  const attempt = entry.attempt && entry.attempt > 1 ? ` (attempt ${entry.attempt})` : "";
  let detail = "";
  const result = entry.result as Record<string, unknown> | undefined;
  if (entry.error) detail = ` — ${entry.error}`;
  else if (result && typeof result === "object") {
    if ("branch" in result) detail = ` → ${String(result.branch)}`;
    else if ("resume_at" in result) detail = ` until ${String(result.resume_at)}`;
    else if ("proposal_id" in result) detail = " → proposed for review";
  }
  return `${label}: ${entry.status ?? "?"}${attempt}${detail}`;
}

/** Whether a run is waiting on a person or a timer. */
export function runNeeds(status: string): "approval" | "timer" | null {
  if (status === "awaiting_approval") return "approval";
  if (status === "waiting") return "timer";
  return null;
}
