// Visual automation builder: TRIGGER → CONDITIONS → ACTIONS, on the existing
// automation engine (cloud/intel/automation/engine.py). Workflows start disabled;
// runs are idempotent per event, retried, audited, and can be paused/cancelled.

import { useState } from "react";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { Json, PageHeader, Pill, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

const TRIGGERS = ["new_company", "hiring_spike", "technology_detected", "leadership_change", "new_contact",
  "email_validated", "job_posted", "long_open_job", "company_matched", "research_completed"];
const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"];
const FIELDS = ["company.industry", "company.technologies", "company.lifecycle", "company.country", "company.state",
  "company.opportunity_score", "company.hiring_score", "signal.signal_type", "contact.title", "contact.email_status",
  "contact.function", "job.title", "payload.score"];

interface FieldSpec { key: string; label: string; kind: "text" | "number" | "bool" | "tags" | "select"; options?: string[]; hint?: string }

const ACTION_FIELDS: Record<string, FieldSpec[]> = {
  create_opportunity: [
    { key: "title", label: "Title (optional)", kind: "text", hint: "empty = use campaign mapping" },
    { key: "campaign_id", label: "Campaign id", kind: "text" },
    { key: "use_campaign_mapping", label: "Use campaign mapping", kind: "bool" },
  ],
  add_to_list: [
    { key: "list_id", label: "List id", kind: "text", hint: "e.g. the Cox-Little list" },
    { key: "entity", label: "Entity", kind: "select", options: ["company", "contact"] },
  ],
  create_task: [
    { key: "title", label: "Title", kind: "text" },
    { key: "due_in_days", label: "Due in days", kind: "number" },
    { key: "priority", label: "Priority", kind: "select", options: ["low", "normal", "high", "urgent"] },
    { key: "description", label: "Description", kind: "text" },
  ],
  assign_campaign: [{ key: "campaign_id", label: "Campaign id", kind: "text" }],
  validate_email: [{ key: "allow_paid", label: "Allow paid validation (admin-saved only)", kind: "bool" }],
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
};

type Leaf = { field: string; op: string; value: unknown };
type Group = { all?: Node[]; any?: Node[] };
type Node = Leaf | Group;
type Action = Record<string, unknown> & { type: string };

interface Draft { id?: string; name: string; trigger: string; conditions: Group; actions: Action[]; enabled?: boolean; description?: string }

const TEMPLATES: { label: string; draft: Draft }[] = [
  {
    label: "Hiring spike → opportunity + Cox-Little list + research task",
    draft: {
      name: "Manufacturing SAP hiring spike", trigger: "hiring_spike",
      conditions: { all: [
        { field: "company.industry", op: "contains", value: "Manufacturing" },
        { field: "company.technologies", op: "contains", value: "SAP" },
        { field: "company.lifecycle", op: "ne", value: "account" },
      ] },
      actions: [
        { type: "create_opportunity", use_campaign_mapping: true },
        { type: "add_to_list", list_id: "", entity: "company" },
        { type: "create_task", title: "Research the hiring spike", due_in_days: 2, priority: "high" },
      ],
    },
  },
  {
    label: "New IT leader → validate email + follow-up task",
    draft: {
      name: "New IT leader follow-up", trigger: "new_contact",
      conditions: { all: [
        { any: [
          { field: "contact.title", op: "contains", value: "CIO" },
          { field: "contact.title", op: "contains", value: "CTO" },
          { field: "contact.title", op: "contains", value: "VP IT" },
        ] },
        { field: "contact.email_status", op: "in", value: ["UNVERIFIED", "UNKNOWN"] },
      ] },
      actions: [
        { type: "validate_email", allow_paid: false },
        { type: "create_task", title: "Follow up with the new IT leader", due_in_days: 3, priority: "normal" },
      ],
    },
  },
];

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
      {children.length === 0 && <p className="muted small">No conditions: every event of this trigger matches.</p>}
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

function ActionEditor({ action, onChange, onRemove, index }: { action: Action; onChange: (a: Action) => void; onRemove: () => void; index: number }) {
  const fields = ACTION_FIELDS[action.type] ?? [];
  return (
    <div className="cr-action">
      <div className="cr-action__head">
        <span className="tabular muted">{index + 1}.</span>
        <select className="input input--small" aria-label="Action type" value={action.type} onChange={(e) => onChange({ type: e.target.value })}>
          {Object.keys(ACTION_FIELDS).map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
        </select>
        <button type="button" className="button button--ghost button--small" onClick={onRemove}>Remove</button>
      </div>
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
    </div>
  );
}

function clean(action: Action): Action {
  return Object.fromEntries(Object.entries(action).filter(([k, v]) => !k.startsWith("_") && v !== "" && v !== undefined)) as Action;
}

function Builder({ initial, onSaved, onClose }: { initial: Draft; onSaved: () => void; onClose: () => void }) {
  const client = useWs();
  const [draft, setDraft] = useState<Draft>(initial);
  const [payload, setPayload] = useState('{\n  "company_id": ""\n}');
  const [test, setTest] = useState<unknown>(null);
  const action = useAction();
  const save = () =>
    action.run(async () => {
      const body = { name: draft.name, trigger: draft.trigger, conditions: draft.conditions, actions: draft.actions.map(clean), description: draft.description };
      if (draft.id) await client.patch(`/workflows/${draft.id}`, body);
      else {
        const row = await client.post<Row>("/workflows", body);
        setDraft((d) => ({ ...d, id: row.id }));
      }
      onSaved();
    });
  const runTest = () =>
    action.run(async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(payload || "{}");
      } catch {
        throw new Error("The sample payload is not valid JSON");
      }
      setTest(await client.post(`/workflows/${draft.id}/test`, { payload: parsed }));
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
      </div>
      <div className="cr-flow">
        <div className="cr-flow__col"><h4>TRIGGER</h4><Pill value={draft.trigger} /></div>
        <div className="cr-flow__arrow" aria-hidden="true">→</div>
        <div className="cr-flow__col cr-flow__col--wide"><h4>CONDITIONS</h4><ConditionGroup group={draft.conditions} onChange={(g) => setDraft({ ...draft, conditions: g })} /></div>
        <div className="cr-flow__arrow" aria-hidden="true">→</div>
        <div className="cr-flow__col cr-flow__col--wide">
          <h4>ACTIONS</h4>
          {draft.actions.map((a, i) => (
            <ActionEditor key={i} index={i} action={a} onChange={(n) => setDraft({ ...draft, actions: draft.actions.map((x, k) => (k === i ? n : x)) })} onRemove={() => setDraft({ ...draft, actions: draft.actions.filter((_, k) => k !== i) })} />
          ))}
          <button type="button" className="button button--ghost button--small" onClick={() => setDraft({ ...draft, actions: [...draft.actions, { type: "create_task", title: "Follow up" }] })}>+ Action</button>
        </div>
      </div>
      <p className="muted small">New workflows are saved disabled. Paid actions only spend credits when an admin saved the workflow with “allow paid”. Sequence actions only create enrolments that wait for approval — nothing is emailed automatically.</p>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button type="button" className="button button--ghost" onClick={onClose}>Close</button>
        <button type="button" className="button button--primary" disabled={action.busy || !draft.name.trim() || draft.actions.length === 0} onClick={save}>{draft.id ? "Save changes" : "Save workflow"}</button>
      </div>
      {draft.id && (
        <div className="cr-test">
          <h4>Test run (dry run — changes nothing)</h4>
          <textarea className="input textarea mono small" rows={3} aria-label="Sample event payload" value={payload} onChange={(e) => setPayload(e.target.value)} />
          <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={runTest}>Test with this payload</button>
          {test ? <Json value={test} /> : null}
        </div>
      )}
    </div>
  );
}

function History({ workflowId }: { workflowId: string }) {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const runs = useLoad((s) => client.list("/workflow-runs", { workflow_id: workflowId, limit: 25 }, s), client.base + "wfr" + workflowId + reload);
  const tasks = useLoad((s) => client.list("/tasks", { entity_type: "workflow_runs", limit: 200 }, s), client.base + "wft" + reload);
  const action = useAction();
  const taskFor = (runId: string) => tasks.data?.items.find((t) => t.entity_id === runId);
  if (!runs.data) return <Loading />;
  if (runs.data.items.length === 0) return <p className="muted small">No executions yet.</p>;
  return (
    <>
      {action.error && <ErrorBanner error={action.error} />}
      <table className="table">
        <thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Steps</th><th>When</th><th /></tr></thead>
        <tbody>
          {runs.data.items.map((r) => {
            const task = taskFor(r.id);
            const status = String(task?.status ?? "");
            return (
              <tr key={r.id}>
                <td className="mono small">{String(r.event_key).slice(0, 40)}</td>
                <td><Pill value={r.status} />{r.error ? <div className="cr-bad small">{String(r.error)}</div> : null}</td>
                <td className="tabular">{String(r.attempts)}</td>
                <td className="small">{Array.isArray(r.steps) ? (r.steps as Row[]).map((s) => `${String(s.type)}:${String(s.status)}`).join(", ") : "—"}</td>
                <td className="small">{fmt(r.created_at)}</td>
                <td>
                  {task && ["queued", "retrying", "running"].includes(status) && (
                    <>
                      <button type="button" className="button button--ghost button--small" onClick={() => action.run(async () => { await client.post(`/tasks/${task.id}/pause`); setReload((n) => n + 1); })}>Pause</button>
                      <button type="button" className="button button--danger button--small" onClick={() => action.run(async () => { await client.post(`/tasks/${task.id}/cancel`); setReload((n) => n + 1); })}>Cancel</button>
                    </>
                  )}
                  {task && status === "paused" && <button type="button" className="button button--ghost button--small" onClick={() => action.run(async () => { await client.post(`/tasks/${task.id}/resume`); setReload((n) => n + 1); })}>Resume</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

export function AutomationBuilder() {
  const client = useWs();
  const [editing, setEditing] = useState<Draft | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const action = useAction();
  const workflows = useLoad((s) => client.list("/workflows", { limit: 100 }, s), client.base + "wf" + reload);
  return (
    <div className="page">
      <PageHeader
        title="Automation builder"
        subtitle="TRIGGER → CONDITIONS → ACTIONS. Runs are idempotent per event, retried on failure, audited, and can be paused or cancelled."
        actions={<button type="button" className="button button--primary" onClick={() => setEditing({ name: "New workflow", trigger: "hiring_spike", conditions: { all: [] }, actions: [] })}>New workflow</button>}
      />
      {!editing && (
        <div className="chips cr-examples">
          {TEMPLATES.map((t) => <button key={t.label} type="button" className="chip cr-chipbtn" onClick={() => setEditing(structuredClone(t.draft))}>Template: {t.label}</button>)}
        </div>
      )}
      {editing && <Builder initial={editing} onClose={() => setEditing(null)} onSaved={() => setReload((n) => n + 1)} />}
      {action.error && <ErrorBanner error={action.error} />}
      {workflows.error && <ErrorBanner error={workflows.error} onRetry={workflows.refresh} />}
      {!workflows.data ? <Loading /> : (
        <div className="card">
          <table className="table">
            <thead><tr><th>Workflow</th><th>Trigger</th><th>Conditions</th><th>Actions</th><th>Enabled</th><th /></tr></thead>
            <tbody>
              {workflows.data.items.length === 0 && <tr><td colSpan={6} className="muted">No workflows yet. Start from a template.</td></tr>}
              {workflows.data.items.map((w) => (
                <tr key={w.id} className="table__row">
                  <td><strong>{String(w.name)}</strong></td>
                  <td><Pill value={w.trigger} /></td>
                  <td className="small mono">{JSON.stringify(w.conditions).slice(0, 80)}</td>
                  <td className="small">{Array.isArray(w.actions) ? (w.actions as Row[]).map((a) => String(a.type)).join(" → ") : "—"}</td>
                  <td>
                    <label className="check">
                      <input type="checkbox" checked={Boolean(w.enabled)} aria-label={w.enabled ? "Pause workflow" : "Enable workflow"} onChange={(e) => action.run(async () => { await client.patch(`/workflows/${w.id}`, { enabled: e.target.checked }); setReload((n) => n + 1); })} />
                      {w.enabled ? "On" : "Paused"}
                    </label>
                  </td>
                  <td className="actions">
                    <button type="button" className="button button--ghost button--small" onClick={() => setEditing({ id: w.id, name: String(w.name), trigger: String(w.trigger), conditions: toGroup(w.conditions), actions: ((w.actions as Action[]) ?? []).map(clean) })}>Edit</button>
                    <button type="button" className="button button--ghost button--small" aria-expanded={historyFor === w.id} onClick={() => setHistoryFor(historyFor === w.id ? null : w.id)}>History</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {historyFor && <div className="pad"><h4>Execution history</h4><History workflowId={historyFor} /></div>}
        </div>
      )}
    </div>
  );
}
