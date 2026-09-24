// Workspace AI memory: vocabulary, default filters, provider priorities and preferences
// the Control Room applies to every request. Never secrets.

import { useState, type FormEvent } from "react";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { PageHeader, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

const KIND_LABELS: Record<string, string> = {
  alias: "Vocabulary (aliases)",
  default_filter: "Default filters",
  source_priority: "Source priority",
  allowed_providers: "Allowed providers",
  preferred_fields: "Preferred result fields",
  scoring_preference: "Scoring preferences",
  campaign_definition: "Campaign definitions",
  saved_pattern: "Saved research patterns",
  preference: "Other preferences",
};

const EXAMPLES = [
  "Whenever I say ERP, include SAP, Oracle, JDE, Infor and Dynamics.",
  "Always exclude customers.",
  "Prefer Seamless over ZoomInfo.",
  "Default to the United States.",
];

function describe(row: Row): string {
  const value = (row.value ?? {}) as Record<string, unknown>;
  if (Array.isArray(value.expands_to)) return `“${row.key}” → ${(value.expands_to as string[]).join(", ")}`;
  if (Array.isArray(value.exclude_lifecycles)) return `exclude ${(value.exclude_lifecycles as string[]).join(", ")}`;
  if (Array.isArray(value.order)) return (value.order as string[]).join(" before ");
  if (Array.isArray(value.allowed)) return `only ${(value.allowed as string[]).join(", ")}`;
  if (Array.isArray(value.fields)) return (value.fields as string[]).join(", ");
  if (value.country) return `country: ${String(value.country)}`;
  if (value.prioritize) return `prioritise ${String(value.prioritize)}`;
  return JSON.stringify(value);
}

export function MemoryPage() {
  const client = useWs();
  const [text, setText] = useState("");
  const [reload, setReload] = useState(0);
  const action = useAction();
  const memory = useLoad((s) => client.get<{ items: Row[] }>("/agent/memory", undefined, s), client.base + "memory" + reload);
  const grouped: Record<string, Row[]> = {};
  for (const row of memory.data?.items ?? []) (grouped[String(row.kind)] ??= []).push(row);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      await client.post("/agent/memory", { text });
      setText("");
      setReload((n) => n + 1);
    });
  };

  return (
    <div className="page">
      <PageHeader title="AI memory" subtitle="What CareerCrawler AI remembers for this workspace and applies to every request. Only this workspace can see it." />
      <div className="alert alert--info" role="note">Never store secrets here. API keys and passwords belong in Settings → Provider connections, where they are encrypted; memory refuses anything that looks like a key or token.</div>
      <form className="card form" onSubmit={submit}>
        <label className="field field--wide">
          <span className="field__label">Tell me something to remember</span>
          <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder={EXAMPLES[0]} />
        </label>
        <div className="chips">
          {EXAMPLES.map((e) => <button key={e} type="button" className="chip cr-chipbtn" onClick={() => setText(e)}>{e}</button>)}
        </div>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions"><button className="button button--primary" disabled={action.busy || !text.trim()}>Remember</button></div>
      </form>
      {memory.error && <ErrorBanner error={memory.error} onRetry={memory.refresh} />}
      {!memory.data ? <Loading /> : Object.keys(grouped).length === 0 ? (
        <p className="muted">Nothing remembered yet.</p>
      ) : (
        <div className="grid-2">
          {Object.entries(grouped).map(([kind, rows]) => (
            <div key={kind} className="card pad">
              <h3>{KIND_LABELS[kind] ?? kind}</h3>
              <ul className="cr-list">
                {rows.map((row) => (
                  <li key={row.id}>
                    <div>{describe(row)}</div>
                    <div className="muted small">{row.text ? `“${String(row.text)}” · ` : ""}{fmtDate(row.updated_at)}{row.enabled === false ? " · disabled" : ""}</div>
                    <button type="button" className="button button--ghost button--small" onClick={() => action.run(async () => { await client.del(`/agent/memory/${row.id}`); setReload((n) => n + 1); })}>Forget</button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
