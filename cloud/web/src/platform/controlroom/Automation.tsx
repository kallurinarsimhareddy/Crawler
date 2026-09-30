// Automation builder on the workflow engine (cloud/intel/automation). Two ways
// to build: a simple TRIGGER → CONDITIONS → ACTIONS list, or steps with if/else
// branches, delays, approval steps and per-step retries (compiled to the
// engine's graph by logic/workflows.ts). Workflows start disabled; runs are
// idempotent per event, audited, and CRM changes are proposed for review.

import { useState } from "react";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { compileSteps, countSteps, describeHistory, describeStep, runNeeds, stepsFromGraph, type Graph, type HistoryEntry, type Step } from "../logic/workflows";
import { Json, PageHeader, Pill, Tabs, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import "../styles/workflows.css";

const TRIGGERS = ["new_company", "hiring_spike", "technology_detected", "leadership_change", "new_contact",
  "email_validated", "job_posted", "long_open_job", "company_matched", "research_completed",
  "reply_received", "scrape_completed", "import_completed", "validation_job_completed", "manual", "schedule"];
const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"];
const FIELDS = ["company.industry", "company.technologies", "company.lifecycle", "company.country", "company.state",
  "company.opportunity_score", "company.hiring_score", "signal.signal_type", "contact.title", "contact.email_status",
  "contact.function", "job.title", "payload.score", "payload.status"];

interface FieldSpec { key: string; label: string; kind: "text" | "number" | "bool" | "tags" | "select" | "json"; options?: string[]; hint?: string }

const PROPOSE_HINT = "proposed for review; applied directly only if an admin saves it with “safe automation”";

const ACTION_FIELDS: Record<string, FieldSpec[]> = {
  create_task: [
    { key: "title", label: "Title", kind: "text", hint: "{company.name} and {contact.full_name} are filled in" },
    { key: "due_in_days", label: "Due in days", kind: "number" },
    { key: "priority", label: "Priority", kind: "select", options: ["low", "normal", "high", "urgent"] },
    { key: "description", label: "Description", kind: "text" },
  ],
  send_notification: [
    { key: "title", label: "Title", kind: "text" },
    { key: "body", label: "Message", kind: "text" },
    { key: "severity", label: "Severity", kind: "select", options: ["info", "success", "warning", "error"] },
  ],
  add_to_list: [
    { key: "list_id", label: "List id", kind: "text" },
    { key: "entity", label: "Entity", kind: "select", options: ["company", "contact"] },
  ],
  remove_from_list: [
    { key: "list_id", label: "List id", kind: "text" },
    { key: "entity", label: "Entity", kind: "select", options: ["company", "contact"] },
  ],
  update_company: [
    { key: "changes", label: "Changes (JSON)", kind: "json", hint: PROPOSE_HINT },
    { key: "safe_automation", label: "Safe automation (admin only)", kind: "bool" },
  ],
  update_contact: [
    { key: "changes", label: "Changes (JSON)", kind: "json", hint: PROPOSE_HINT },
    { key: "safe_automation", label: "Safe automation (admin only)", kind: "bool" },
  ],
  create_crm_proposal: [
    { key: "entity", label: "Entity", kind: "select", options: ["company", "contact"] },
    { key: "changes", label: "Changes (JSON)", kind: "json", hint: "always reviewed before it is applied" },
    { key: "reason", label: "Reason", kind: "text" },
  ],
  start_research: [{ key: "question", label: "Research question", kind: "text", hint: "planned only — you approve the plan before it runs" }],
  validate_email: [
    { key: "list_id", label: "Contact list id (optional)", kind: "text", hint: "empty = the event's contact" },
    { key: "allow_paid", label: "Allow paid validation (admin-saved only)", kind: "bool" },
  ],
  add_to_campaign: [{ key: "campaign_id", label: "Campaign id", kind: "text" }],
  start_sequence: [{ key: "sequence_id", label: "Sequence id", kind: "text", hint: "enrolments wait for approval; nothing is sent" }],
  create_opportunity: [
    { key: "title", label: "Title (optional)", kind: "text", hint: "empty = use campaign mapping" },
    { key: "campaign_id", label: "Campaign id", kind: "text" },
    { key: "use_campaign_mapping", label: "Use campaign mapping", kind: "bool" },
  ],
  assign_campaign: [{ key: "campaign_id", label: "Campaign id", kind: "text" }],
  find_contacts: [
    { key: "functions", label: "Functions", kind: "tags", hint: "it, hr, executive" },
    { key: "allow_paid", label: "Allow paid providers (admin-saved only)", kind: "bool" },
  ],
  enrich_company: [
    { key: "functions", label: "Functions", kind: "tags" },
    { key: "allow_paid", label: "Allow paid providers (admin-saved only)", kind: "bool" },
  ],
  assign_owner: [
    { key: "owner_id", label: "Owner user id", kind: "text" },
    { key: "entity", label: "Entity", kind: "select", options: ["company", "contact", "opportunity"] },
  ],
  queue_sequence: [{ key: "sequence_id", label: "Sequence id", kind: "text", hint: "enrolments wait for approval; nothing is sent" }],
  export: [
    { key: "entity_type", label: "Entity", kind: "select", options: ["companies", "contacts", "job_postings", "opportunities"] },
    { key: "format", label: "Format", kind: "select", options: ["csv", "xlsx", "json"] },
  ],
  webhook: [{ key: "url", label: "HTTPS URL (public hosts only)", kind: "text" }],
  wait: [{ key: "hours", label: "Wait (hours)", kind: "number" }],
};
const STEP_ACTIONS = Object.keys(ACTION_FIELDS).filter((t) => t !== "wait");

type Leaf = { field: string; op: string; value: unknown };
type Group = { all?: Node[]; any?: Node[] };
type Node = Leaf | Group;
type Action = Record<string, unknown> & { type: string };

interface Draft {
  id?: string;
  name: string;
  trigger: string;
  conditions: Group;
  actions: Action[];
  mode: "simple" | "steps";
  steps: Step[];
  failure_policy: string;
  max_attempts?: number;
  backoff_seconds?: number;
  every_minutes?: number;
  description?: string;
}

function isGroup(node: Node): node is Group {
  return "all" in node || "any" in node;
}

function toGroup(value: unknown): Group {
  if (Array.isArray(value)) return { all: value as Node[] };
  if (value && typeof value === "object" && ("all" in value || "any" in value)) return value as Group;
  if (value && typeof value === "object" && "field" in value) return { all: [value as Leaf] };
  return { all: [] };
}

function parseValue(op: string, raw: string): unknown {
  if (op === "exists") return true;
  if (op === "in") return raw.split(",").map((s) => s.trim()).filter(Boolean);
  if (["gt", "gte", "lt", "lte"].includes(op) && raw.trim() !== "" && !Number.isNaN(Number(raw))) return Number(raw);
  return raw;
}

function clean(action: Action): Action {
  return Object.fromEntries(Object.entries(action).filter(([k, v]) => !k.startsWith("_") && v !== "" && v !== undefined)) as Action;
}

function cleanSteps(steps: Step[]): Step[] {
  return steps.map((s) => (s.kind === "action" ? { ...s, action: clean(s.action) } : s.kind === "branch" ? { ...s, then: cleanSteps(s.then), else: cleanSteps(s.else) } : s));
}

function draftFrom(w: Row): Draft {
  const graph = (w.graph ?? {}) as Graph;
  const steps = stepsFromGraph(graph);
  const retry = (w.retry_policy ?? {}) as { max_attempts?: number; backoff_seconds?: number };
  return {
    id: w.id, name: String(w.name), trigger: String(w.trigger), conditions: toGroup(w.conditions),
    actions: ((w.actions as Action[]) ?? []).map(clean), mode: graph.nodes ? "steps" : "simple", steps: cleanSteps(steps),
    failure_policy: String(w.failure_policy ?? "stop"), max_attempts: retry.max_attempts, backoff_seconds: retry.backoff_seconds,
    every_minutes: graph.schedule?.every_minutes, description: w.description ? String(w.description) : undefined,
  };
}

function ConditionGroup({ group, onChange, depth = 0 }: { group: Group; onChange: (g: Group) => void; depth?: number }) {
  const mode: "all" | "any" = "any" in group ? "any" : "all";
  const children = (group[mode] ?? []) as Node[];
  const set = (next: Node[]) => onChange({ [mode]: next } as Group);
  return (
    <div className={`cr-cond cr-cond--depth${depth}`}>
      <div className="cr-cond__head">
        <label>
          <span className="sr-only">Combine conditions with</span>
          <select className="input input--small" value={mode} onChange={(e) => onChange({ [e.target.value]: children } as Group)}>
            <option value="all">ALL of these</option>
            <option value="any">ANY of these</option>
          </select>
        </label>
      </div>
      {children.length === 0 && <p className="muted small">No conditions: always true.</p>}
      {children.map((child, i) =>
        isGroup(child) ? (
          <div key={i} className="cr-cond__row">
            <ConditionGroup group={child} depth={depth + 1} onChange={(g) => set(children.map((c, k) => (k === i ? g : c)))} />
            <button type="button" className="button button--ghost button--small" onClick={() => set(children.filter((_, k) => k !== i))}>Remove group</button>
          </div>
        ) : (
          <div key={i} className="cr-cond__row cr-cond__leaf">
            <input className="input input--small" list="cr-fields" aria-label="Field" value={child.field} onChange={(e) => set(children.map((c, k) => (k === i ? { ...child, field: e.target.value } : c)))} />
            <select className="input input--small" aria-label="Operator" value={child.op} onChange={(e) => set(children.map((c, k) => (k === i ? { ...child, op: e.target.value, value: parseValue(e.target.value, Array.isArray(child.value) ? child.value.join(", ") : String(child.value ?? "")) } : c)))}>
              {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            {child.op !== "exists" && (
              <input className="input input--small" aria-label="Value" value={Array.isArray(child.value) ? child.value.join(", ") : String(child.value ?? "")} onChange={(e) => set(children.map((c, k) => (k === i ? { ...child, value: parseValue(child.op, e.target.value) } : c)))} />
            )}
            <button type="button" className="button button--ghost button--small" aria-label="Remove condition" onClick={() => set(children.filter((_, k) => k !== i))}>×</button>
          </div>
        ),
      )}
      <div className="actions">
        <button type="button" className="button button--ghost button--small" onClick={() => set([...children, { field: "company.industry", op: "eq", value: "" }])}>+ Condition</button>
        {depth < 2 && <button type="button" className="button button--ghost button--small" onClick={() => set([...children, { any: [] }])}>+ Group</button>}
      </div>
    </div>
  );
}

function JsonField({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(value === undefined ? "" : JSON.stringify(value));
  const [bad, setBad] = useState(false);
  return (
    <>
      <input
        className="input mono"
        aria-invalid={bad}
        value={text}
        placeholder='{"lifecycle": "account"}'
        onChange={(e) => {
          setText(e.target.value);
          if (!e.target.value.trim()) {
            setBad(false);
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(e.target.value));
            setBad(false);
          } catch {
            setBad(true);
          }
        }}
      />
      {bad && <span className="field__hint cr-bad">Not valid JSON yet</span>}
    </>
  );
}

function ActionFields({ action, onChange, types }: { action: Action; onChange: (a: Action) => void; types: string[] }) {
  const fields = ACTION_FIELDS[action.type] ?? [];
  return (
    <>
      <select className="input input--small" aria-label="Action type" value={action.type} onChange={(e) => onChange({ type: e.target.value })}>
        {types.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
      </select>
      <div className="form-grid">
        {fields.map((f) => (
          <label key={f.key} className="field">
            <span className="field__label">{f.label}</span>
            {f.kind === "bool" ? (
              <input type="checkbox" checked={Boolean(action[f.key])} onChange={(e) => onChange({ ...action, [f.key]: e.target.checked })} />
            ) : f.kind === "select" ? (
              <select className="input" value={String(action[f.key] ?? "")} onChange={(e) => onChange({ ...action, [f.key]: e.target.value })}>
                <option value="">—</option>
                {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : f.kind === "json" ? (
              <JsonField value={action[f.key]} onChange={(v) => onChange({ ...action, [f.key]: v })} />
            ) : (
              <input
                className="input"
                type={f.kind === "number" ? "number" : "text"}
                value={Array.isArray(action[f.key]) ? (action[f.key] as string[]).join(", ") : String(action[f.key] ?? "")}
                onChange={(e) => onChange({ ...action, [f.key]: f.kind === "number" ? (e.target.value === "" ? undefined : Number(e.target.value)) : f.kind === "tags" ? e.target.value.split(",").map((s) => s.trim()).filter(Boolean) : e.target.value })}
              />
            )}
            {f.hint && <span className="field__hint">{f.hint}</span>}
          </label>
        ))}
      </div>
    </>
  );
}

function ActionEditor({ action, onChange, onRemove, index }: { action: Action; onChange: (a: Action) => void; onRemove: () => void; index: number }) {
  return (
    <div className="cr-action">
      <div className="cr-action__head">
        <span className="tabular muted">{index + 1}.</span>
        <span className="wf-grow" />
        <button type="button" className="button button--ghost button--small" onClick={onRemove}>Remove</button>
      </div>
      <ActionFields action={action} onChange={onChange} types={Object.keys(ACTION_FIELDS)} />
    </div>
  );
}

// --- steps (graph) editor -----------------------------------------------------------

function StepCard({ step, onChange, onRemove, onMove, index, count, depth }: {
  step: Step; onChange: (s: Step) => void; onRemove: () => void; onMove: (delta: number) => void; index: number; count: number; depth: number;
}) {
  return (
    <div className={`wf-step wf-step--${step.kind}`}>
      <div className="wf-step__head">
        <span className="wf-step__num tabular">{index + 1}</span>
        <strong>{step.kind === "branch" ? "If / else" : step.kind === "delay" ? "Delay" : step.kind === "approval" ? "Approval" : "Action"}</strong>
        <span className="muted small">{describeStep(step)}</span>
        <span className="wf-grow" />
        <button type="button" className="button button--ghost button--small" aria-label="Move up" disabled={index === 0} onClick={() => onMove(-1)}>↑</button>
        <button type="button" className="button button--ghost button--small" aria-label="Move down" disabled={index === count - 1} onClick={() => onMove(1)}>↓</button>
        <button type="button" className="button button--ghost button--small" onClick={onRemove}>Remove</button>
      </div>
      {step.kind === "action" && (
        <>
          <ActionFields action={step.action as Action} types={STEP_ACTIONS} onChange={(a) => onChange({ ...step, action: a })} />
          <div className="wf-inline">
            <label className="field wf-narrow">
              <span className="field__label">Attempts</span>
              <input className="input input--small" type="number" min={1} max={10} value={step.retries ?? 1} onChange={(e) => onChange({ ...step, retries: Number(e.target.value) || 1 })} />
            </label>
            <label className="field wf-narrow">
              <span className="field__label">Backoff (seconds)</span>
              <input className="input input--small" type="number" min={0} value={step.backoffSeconds ?? 60} onChange={(e) => onChange({ ...step, backoffSeconds: Number(e.target.value) || 0 })} />
            </label>
          </div>
        </>
      )}
      {step.kind === "delay" && (
        <div className="wf-inline">
          <label className="field wf-narrow">
            <span className="field__label">Wait</span>
            <input className="input input--small" type="number" min={1} value={step.amount} onChange={(e) => onChange({ ...step, amount: Math.max(1, Number(e.target.value) || 1) })} />
          </label>
          <label className="field wf-narrow">
            <span className="field__label">Unit</span>
            <select className="input input--small" value={step.unit} onChange={(e) => onChange({ ...step, unit: e.target.value as "minutes" | "hours" | "days" })}>
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </select>
          </label>
        </div>
      )}
      {step.kind === "approval" && (
        <label className="field">
          <span className="field__label">Question for the approver</span>
          <input className="input" value={step.message} onChange={(e) => onChange({ ...step, message: e.target.value })} />
          <span className="field__hint">The run waits until a workspace member approves; a rejection ends the run.</span>
        </label>
      )}
      {step.kind === "branch" && (
        <>
          <ConditionGroup group={toGroup(step.conditions)} onChange={(g) => onChange({ ...step, conditions: g as Record<string, unknown> })} />
          <div className="wf-branches">
            <div className="wf-branch">
              <h5>Then</h5>
              <StepList steps={step.then} depth={depth + 1} onChange={(then) => onChange({ ...step, then })} />
            </div>
            <div className="wf-branch">
              <h5>Else</h5>
              <StepList steps={step.else} depth={depth + 1} onChange={(els) => onChange({ ...step, else: els })} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function StepList({ steps, onChange, depth = 0 }: { steps: Step[]; onChange: (s: Step[]) => void; depth?: number }) {
  const add = (step: Step) => onChange([...steps, step]);
  return (
    <div className="wf-steps">
      {steps.length === 0 && <p className="muted small">No steps{depth ? " — this path continues after the branch" : ""}.</p>}
      {steps.map((s, i) => (
        <StepCard
          key={i}
          step={s}
          index={i}
          count={steps.length}
          depth={depth}
          onChange={(n) => onChange(steps.map((x, k) => (k === i ? n : x)))}
          onRemove={() => onChange(steps.filter((_, k) => k !== i))}
          onMove={(delta) => {
            const next = [...steps];
            const [item] = next.splice(i, 1);
            next.splice(i + delta, 0, item);
            onChange(next);
          }}
        />
      ))}
      <div className="actions">
        <button type="button" className="button button--ghost button--small" onClick={() => add({ kind: "action", action: { type: "create_task", title: "Follow up" } })}>+ Action</button>
        <button type="button" className="button button--ghost button--small" onClick={() => add({ kind: "delay", amount: 1, unit: "days" })}>+ Delay</button>
        <button type="button" className="button button--ghost button--small" onClick={() => add({ kind: "approval", message: "Continue?" })}>+ Approval</button>
        {depth < 2 && <button type="button" className="button button--ghost button--small" onClick={() => add({ kind: "branch", conditions: { all: [] }, then: [], else: [] })}>+ If / else</button>}
      </div>
    </div>
  );
}

// --- builder -------------------------------------------------------------------------

function Builder({ initial, onSaved, onClose }: { initial: Draft; onSaved: () => void; onClose: () => void }) {
  const client = useWs();
  const [draft, setDraft] = useState<Draft>(initial);
  const [payload, setPayload] = useState('{\n  "company_id": ""\n}');
  const [test, setTest] = useState<unknown>(null);
  const [confirmRun, setConfirmRun] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const action = useAction();
  const schedule = draft.trigger === "schedule" && draft.every_minutes ? { every_minutes: draft.every_minutes } : undefined;
  const hasWork = draft.mode === "steps" ? draft.steps.length > 0 : draft.actions.length > 0;

  const parsePayload = (): unknown => {
    try {
      return JSON.parse(payload || "{}");
    } catch {
      throw new Error("The sample payload is not valid JSON");
    }
  };
  const save = () =>
    action.run(async () => {
      const retry: Record<string, number> = {};
      if (draft.max_attempts) retry.max_attempts = draft.max_attempts;
      if (draft.backoff_seconds !== undefined) retry.backoff_seconds = draft.backoff_seconds;
      const body = {
        name: draft.name, trigger: draft.trigger, conditions: draft.conditions, description: draft.description,
        actions: draft.mode === "simple" ? draft.actions.map(clean) : [],
        graph: draft.mode === "steps" ? compileSteps(cleanSteps(draft.steps), schedule) : schedule ? { schedule } : {},
        failure_policy: draft.failure_policy, retry_policy: retry,
      };
      if (draft.id) await client.patch(`/workflows/${draft.id}`, body);
      else {
        const row = await client.post<Row>("/workflows", body);
        setDraft((d) => ({ ...d, id: row.id }));
      }
      setNotice("Saved.");
      onSaved();
    });
  const runTest = () => action.run(async () => setTest(await client.post(`/workflows/${draft.id}/test`, { payload: parsePayload() })));
  const runNow = () =>
    action.run(async () => {
      await client.post(`/workflows/${draft.id}/run`, { payload: parsePayload() });
      setConfirmRun(false);
      setNotice("Run started — see its history in the workflow list.");
      onSaved();
    });

  return (
    <div className="card pad cr-builder">
      <datalist id="cr-fields">{FIELDS.map((f) => <option key={f} value={f} />)}</datalist>
      <div className="form-grid">
        <label className="field"><span className="field__label">Name</span><input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
        <label className="field">
          <span className="field__label">Trigger</span>
          <select className="input" value={draft.trigger} onChange={(e) => setDraft({ ...draft, trigger: e.target.value })}>
            {TRIGGERS.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ").toUpperCase()}</option>)}
          </select>
        </label>
        {draft.trigger === "schedule" && (
          <label className="field">
            <span className="field__label">Every (minutes)</span>
            <input className="input" type="number" min={5} value={draft.every_minutes ?? ""} placeholder="1440 = daily" onChange={(e) => setDraft({ ...draft, every_minutes: e.target.value ? Number(e.target.value) : undefined })} />
          </label>
        )}
        <label className="field">
          <span className="field__label">When a step fails</span>
          <select className="input" value={draft.failure_policy} onChange={(e) => setDraft({ ...draft, failure_policy: e.target.value })}>
            <option value="stop">Stop the run</option>
            <option value="continue">Skip it and continue</option>
            <option value="retry">Retry (3 attempts by default)</option>
          </select>
        </label>
        <label className="field">
          <span className="field__label">Default attempts per step</span>
          <input className="input" type="number" min={1} max={10} value={draft.max_attempts ?? ""} placeholder="1" onChange={(e) => setDraft({ ...draft, max_attempts: e.target.value ? Number(e.target.value) : undefined })} />
        </label>
        <label className="field">
          <span className="field__label">Retry backoff (seconds)</span>
          <input className="input" type="number" min={0} value={draft.backoff_seconds ?? ""} placeholder="60, doubling" onChange={(e) => setDraft({ ...draft, backoff_seconds: e.target.value ? Number(e.target.value) : undefined })} />
        </label>
      </div>
      <div className="wf-mode" role="radiogroup" aria-label="Builder mode">
        <label className="check"><input type="radio" checked={draft.mode === "simple"} onChange={() => setDraft({ ...draft, mode: "simple" })} /> Simple list of actions</label>
        <label className="check"><input type="radio" checked={draft.mode === "steps"} onChange={() => setDraft({ ...draft, mode: "steps", steps: draft.steps.length ? draft.steps : draft.actions.map((a) => ({ kind: "action" as const, action: a })) })} /> Steps with branches, delays and approvals</label>
      </div>
      <div className="cr-flow">
        <div className="cr-flow__col"><h4>TRIGGER</h4><Pill value={draft.trigger} /></div>
        <div className="cr-flow__arrow" aria-hidden="true">→</div>
        <div className="cr-flow__col cr-flow__col--wide"><h4>CONDITIONS</h4><ConditionGroup group={draft.conditions} onChange={(g) => setDraft({ ...draft, conditions: g })} /></div>
        <div className="cr-flow__arrow" aria-hidden="true">→</div>
        <div className="cr-flow__col cr-flow__col--wide">
          <h4>{draft.mode === "steps" ? "STEPS" : "ACTIONS"}</h4>
          {draft.mode === "steps" ? (
            <StepList steps={draft.steps} onChange={(steps) => setDraft({ ...draft, steps })} />
          ) : (
            <>
              {draft.actions.map((a, i) => (
                <ActionEditor key={i} index={i} action={a} onChange={(n) => setDraft({ ...draft, actions: draft.actions.map((x, k) => (k === i ? n : x)) })} onRemove={() => setDraft({ ...draft, actions: draft.actions.filter((_, k) => k !== i) })} />
              ))}
              <button type="button" className="button button--ghost button--small" onClick={() => setDraft({ ...draft, actions: [...draft.actions, { type: "create_task", title: "Follow up" }] })}>+ Action</button>
            </>
          )}
        </div>
      </div>
      <p className="muted small">
        New workflows are saved disabled. Company and contact changes are proposed for review (Approvals tab). Paid actions only spend credits when an admin saved the workflow with “allow paid”.
        Sequence steps only create enrolments that wait for approval — workflows never send email.
      </p>
      {action.error && <ErrorBanner error={action.error} />}
      {notice && <p className="small wf-notice" role="status">{notice}</p>}
      <div className="form__actions">
        <button type="button" className="button button--ghost" onClick={onClose}>Close</button>
        <button type="button" className="button button--primary" disabled={action.busy || !draft.name.trim() || !hasWork} onClick={save}>{draft.id ? "Save changes" : "Save workflow"}</button>
      </div>
      {draft.id && (
        <div className="cr-test">
          <h4>Try it</h4>
          <textarea className="input textarea mono small" rows={3} aria-label="Sample event payload" value={payload} onChange={(e) => setPayload(e.target.value)} />
          <div className="actions">
            <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={runTest}>Dry run (changes nothing)</button>
            {!confirmRun ? (
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => setConfirmRun(true)}>Run now…</button>
            ) : (
              <>
                <span className="small">This runs the steps for real with this payload.</span>
                <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={runNow}>Confirm run</button>
                <button type="button" className="button button--ghost button--small" onClick={() => setConfirmRun(false)}>Cancel</button>
              </>
            )}
          </div>
          {test ? <Json value={test} /> : null}
        </div>
      )}
    </div>
  );
}

// --- history ------------------------------------------------------------------------------

function RunSteps({ run }: { run: Row }) {
  const history = (Array.isArray(run.history) && (run.history as HistoryEntry[]).length ? run.history : run.steps) as HistoryEntry[] | undefined;
  if (!Array.isArray(history) || history.length === 0) return <span className="muted">—</span>;
  return (
    <ol className="wf-history">
      {history.map((h, i) => (
        <li key={i} className={`wf-history__item wf-history__item--${h.status ?? "unknown"}`}>
          {h.node ? <span className="mono muted">{h.node} </span> : null}
          {describeHistory(h)}
        </li>
      ))}
    </ol>
  );
}

function History({ workflowId }: { workflowId: string }) {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const runs = useLoad((s) => client.list("/workflow-runs", { workflow_id: workflowId, limit: 25 }, s), client.base + "wfr" + workflowId + reload);
  const tasks = useLoad((s) => client.list("/tasks", { entity_type: "workflow_runs", limit: 200 }, s), client.base + "wft" + reload);
  const action = useAction();
  const taskFor = (runId: string) => tasks.data?.items.find((t) => t.entity_id === runId && ["queued", "retrying", "running", "paused"].includes(String(t.status)));
  const act = (path: string) => action.run(async () => { await client.post(path); setReload((n) => n + 1); });
  if (runs.error) return <ErrorBanner error={runs.error} onRetry={runs.refresh} />;
  if (!runs.data) return <Loading />;
  if (runs.data.items.length === 0) return <p className="muted small">No executions yet.</p>;
  return (
    <>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Steps</th><th>When</th><th /></tr></thead>
          <tbody>
            {runs.data.items.map((r) => {
              const task = taskFor(r.id);
              const status = String(task?.status ?? "");
              const needs = runNeeds(String(r.status));
              return (
                <tr key={r.id}>
                  <td className="mono small">{String(r.event_key).slice(0, 40)}</td>
                  <td>
                    <Pill value={r.status} />
                    {needs === "timer" && r.resume_at ? <div className="small muted">until {fmt(r.resume_at)}</div> : null}
                    {r.error ? <div className="cr-bad small">{String(r.error)}</div> : null}
                  </td>
                  <td className="tabular">{String(r.attempts)}</td>
                  <td className="small"><RunSteps run={r} /></td>
                  <td className="small">{fmt(r.created_at)}</td>
                  <td className="actions">
                    {needs === "approval" && (
                      <>
                        <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/approve`)}>Approve</button>
                        <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/reject`)}>Reject</button>
                      </>
                    )}
                    {needs === "timer" && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/resume`)}>Resume now</button>}
                    {["waiting", "awaiting_approval", "pending"].includes(String(r.status)) && (
                      <button type="button" className="button button--danger button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/cancel`)}>Cancel run</button>
                    )}
                    {task && ["queued", "retrying", "running"].includes(status) && <button type="button" className="button button--ghost button--small" onClick={() => act(`/tasks/${task.id}/pause`)}>Pause task</button>}
                    {task && status === "paused" && <button type="button" className="button button--ghost button--small" onClick={() => act(`/tasks/${task.id}/resume`)}>Resume task</button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// --- approvals & proposals ---------------------------------------------------------------------

function Approvals() {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const action = useAction();
  const waiting = useLoad((s) => client.list("/workflow-runs", { status: "awaiting_approval", limit: 50 }, s), client.base + "wfa" + reload);
  const proposals = useLoad((s) => client.list("/workflow-proposals", { status__in: "proposed,approved,failed", limit: 100 }, s), client.base + "wfp" + reload);
  const act = (path: string, body?: unknown) => action.run(async () => { await client.post(path, body); setReload((n) => n + 1); });
  return (
    <div className="wf-approvals">
      {action.error && <ErrorBanner error={action.error} />}
      <section className="card pad">
        <h3>Runs waiting for approval</h3>
        {waiting.error && <ErrorBanner error={waiting.error} onRetry={waiting.refresh} />}
        {!waiting.data ? <Loading /> : waiting.data.items.length === 0 ? (
          <p className="muted small">Nothing is waiting. Approval steps pause a run here until someone decides.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Run</th><th>Step</th><th>Since</th><th /></tr></thead>
              <tbody>
                {waiting.data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="mono small">{String(r.event_key).slice(0, 40)}</td>
                    <td className="mono small">{String(r.current_node ?? "—")}</td>
                    <td className="small">{fmt(r.updated_at)}</td>
                    <td className="actions">
                      <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/approve`)}>Approve</button>
                      <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act(`/workflow-runs/${r.id}/reject`)}>Reject</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card pad">
        <h3>Proposed CRM changes</h3>
        <p className="muted small">Workflows propose company and contact changes. Approve, then apply — nothing changes until you apply it.</p>
        {proposals.error && <ErrorBanner error={proposals.error} onRetry={proposals.refresh} />}
        {!proposals.data ? <Loading /> : proposals.data.items.length === 0 ? (
          <p className="muted small">No proposals waiting.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Record</th><th>Changes</th><th>Reason</th><th>Status</th><th /></tr></thead>
              <tbody>
                {proposals.data.items.map((p) => (
                  <tr key={p.id}>
                    <td className="small"><Pill value={p.entity_type} /> <span className="mono">{String(p.entity_id)}</span></td>
                    <td className="mono small">{JSON.stringify(p.changes)}</td>
                    <td className="small">{fmt(p.reason)}{p.error ? <div className="cr-bad">{String(p.error)}</div> : null}</td>
                    <td><Pill value={p.status} /></td>
                    <td className="actions">
                      {p.status === "proposed" && (
                        <>
                          <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act("/workflow-proposals/review", { ids: [p.id], decision: "approve" })}>Approve</button>
                          <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act("/workflow-proposals/review", { ids: [p.id], decision: "reject" })}>Reject</button>
                        </>
                      )}
                      {p.status === "approved" && <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => act("/workflow-proposals/apply", { ids: [p.id] })}>Apply</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

// --- templates ---------------------------------------------------------------------------------

function Templates({ onCreated }: { onCreated: (row: Row) => void }) {
  const client = useWs();
  const action = useAction();
  const templates = useLoad((s) => client.get<{ items: Row[] }>("/workflow-templates", undefined, s), client.base + "wftpl");
  if (templates.error) return <ErrorBanner error={templates.error} onRetry={templates.refresh} />;
  if (!templates.data) return <Loading />;
  return (
    <>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="wf-templates">
        {templates.data.items.map((t) => {
          const steps = stepsFromGraph(t.graph as Graph);
          return (
            <div key={String(t.key)} className="card pad wf-template">
              <h3>{String(t.name)}</h3>
              <p className="muted small">{String(t.description)}</p>
              <div className="small"><Pill value={t.trigger} /> <span className="muted">{countSteps(steps)} step(s): {steps.map(describeStep).join(" → ")}</span></div>
              <div className="form__actions">
                <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => action.run(async () => onCreated(await client.post<Row>(`/workflow-templates/${String(t.key)}`, {})))}>Use template</button>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

// --- page --------------------------------------------------------------------------------------

export function AutomationBuilder() {
  const client = useWs();
  const [tab, setTab] = useState("workflows");
  const [editing, setEditing] = useState<Draft | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const action = useAction();
  const workflows = useLoad((s) => client.list("/workflows", { limit: 100 }, s), client.base + "wf" + reload);
  const blank: Draft = { name: "New workflow", trigger: "hiring_spike", conditions: { all: [] }, actions: [], mode: "steps", steps: [], failure_policy: "stop" };
  return (
    <div className="page">
      <PageHeader
        title="Workflows"
        subtitle="Triggers, conditions, if/else branches, delays, approvals and actions. Runs are idempotent per event, retried by policy, audited, and CRM changes wait for review."
        actions={<button type="button" className="button button--primary" onClick={() => { setTab("workflows"); setEditing(blank); }}>New workflow</button>}
      />
      <Tabs
        tabs={[{ key: "workflows", label: "Workflows", count: workflows.data?.total }, { key: "templates", label: "Templates" }, { key: "approvals", label: "Approvals & proposals" }]}
        active={tab}
        onChange={setTab}
      />
      {tab === "templates" && <Templates onCreated={(row) => { setReload((n) => n + 1); setTab("workflows"); setEditing(draftFrom(row)); }} />}
      {tab === "approvals" && <Approvals />}
      {tab === "workflows" && (
        <>
          {editing && <Builder key={editing.id ?? "new"} initial={editing} onClose={() => setEditing(null)} onSaved={() => setReload((n) => n + 1)} />}
          {action.error && <ErrorBanner error={action.error} />}
          {workflows.error && <ErrorBanner error={workflows.error} onRetry={workflows.refresh} />}
          {!workflows.data ? <Loading /> : workflows.data.items.length === 0 && !editing ? (
            <EmptyState
              icon="workflow"
              title="No workflows yet"
              description="Automate follow-ups: react to hiring spikes, replies, validated lists or a schedule. Start from a template or build your own."
              action={<button type="button" className="button button--primary" onClick={() => setTab("templates")}>Browse templates</button>}
            />
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Workflow</th><th>Trigger</th><th>Steps</th><th>On failure</th><th>Last run</th><th>Enabled</th><th /></tr></thead>
                  <tbody>
                    {workflows.data.items.map((w) => {
                      const graph = (w.graph ?? {}) as Graph;
                      const steps = graph.nodes ? stepsFromGraph(graph) : [];
                      return (
                        <tr key={w.id} className="table__row">
                          <td><strong>{String(w.name)}</strong>{w.template_key ? <div className="muted small">from template</div> : null}</td>
                          <td><Pill value={w.trigger} /></td>
                          <td className="small">{graph.nodes ? `${countSteps(steps)} step(s): ${steps.map(describeStep).join(" → ")}` : Array.isArray(w.actions) ? (w.actions as Row[]).map((a) => String(a.type).replace(/_/g, " ")).join(" → ") : "—"}</td>
                          <td className="small">{String(w.failure_policy ?? "stop")}</td>
                          <td className="small">{fmt(w.last_run_at)}</td>
                          <td>
                            <label className="check">
                              <input type="checkbox" checked={Boolean(w.enabled)} aria-label={w.enabled ? "Pause workflow" : "Enable workflow"} onChange={(e) => action.run(async () => { await client.patch(`/workflows/${w.id}`, { enabled: e.target.checked }); setReload((n) => n + 1); })} />
                              {w.enabled ? "On" : "Paused"}
                            </label>
                          </td>
                          <td className="actions">
                            <button type="button" className="button button--ghost button--small" onClick={() => setEditing(draftFrom(w))}>Edit</button>
                            <button type="button" className="button button--ghost button--small" aria-expanded={historyFor === w.id} onClick={() => setHistoryFor(historyFor === w.id ? null : w.id)}>History</button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {historyFor && <div className="pad"><h4>Run history</h4><History workflowId={historyFor} /></div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}
