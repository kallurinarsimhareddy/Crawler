// Research agent, imports, exports, sources, credits and settings. The AI scraper is in Scraper.tsx.

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { DataTable, Json, KeyValues, PageHeader, Pill, ResourceList, Stat, Tags, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import { AiProviderSettings, ProactiveSettings } from "../controlroom/AiSettings";

// --- Research agent ------------------------------------------------------------

const EXAMPLE =
  "Find US manufacturing companies using RPG or AS400, match them against my internal data, remove companies already in my CRM, find companies with new ERP hiring, identify missing IT/HR/VP contacts using my authorized sources, validate the emails, rank the opportunities, assign the correct staffing campaign, and export the results.";

export function Research({ part = "both" }: { part?: "form" | "history" | "both" }) {
  const client = useWs();
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  const action = useAction();
  return (
    <div className="page">
      <PageHeader title="Research agent" subtitle="Ask in plain language. The agent plans, shows the plan and its credit cost, and only runs after you approve. CRM changes are proposed, never applied silently." />
      {part !== "history" && (
      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            const run = await client.post<Row>("/research/plan", { question });
            navigate(`/research/${run.id}`);
          });
        }}
      >
        <label className="field field--wide">
          <span className="field__label">Question</span>
          <textarea className="input textarea" rows={4} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={EXAMPLE} />
        </label>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions">
          <button className="button button--primary" type="submit" disabled={action.busy || !question.trim()}>{action.busy ? "Planning…" : "Create plan"}</button>
          <button className="button button--ghost" type="button" onClick={() => setQuestion(EXAMPLE)}>Use example</button>
        </div>
      </form>
      )}
      {part !== "form" && (
      <ResourceList
        empty={{ title: "No research yet", description: "Ask a question above and the agent plans it; every plan and its results are kept here.", icon: "bot" }}
        load={(q, s) => client.list("/research/runs", q, s)}
        link={(r) => `/research/${r.id}`}
        columns={[
          { key: "question", label: "Question", render: (r) => String(r.question).slice(0, 110) },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "result_count", label: "Results" },
          { key: "planner", label: "Planner" },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
        ]}
      />
      )}
    </div>
  );
}

interface Step { id?: string; tool?: string; title?: string; description?: string; spends_credits?: boolean; mutates?: boolean; estimated_credits?: number; params?: unknown; status?: string }
interface Proposal { id: string; type?: string; kind?: string; title?: string; description?: string; count?: number; status?: string }

export function ResearchRun() {
  const { runId = "" } = useParams();
  const client = useWs();
  const [allowPaid, setAllowPaid] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  const run = useLoad((signal) => client.get<Row>(`/research/runs/${runId}`, undefined, signal), client.base + runId, 4000);
  const results = useLoad((signal) => client.list(`/research/runs/${runId}/results`, { limit: 200 }, signal).catch(() => ({ items: [], total: 0, limit: 0, offset: 0, has_more: false })), client.base + runId + "r" + String(run.data?.status));
  const action = useAction();
  if (run.error) return <div className="page"><ErrorBanner error={run.error} /></div>;
  if (!run.data) return <div className="page"><Loading /></div>;
  const r = run.data;
  const plan = (Array.isArray(r.plan) ? r.plan : []) as Step[];
  const proposals = (Array.isArray(r.proposed_actions) ? r.proposed_actions : []) as Proposal[];
  const progress = (r.progress ?? {}) as Record<string, unknown>;
  const resultRows = results.data?.items ?? [];
  const resultKeys = Array.from(new Set(resultRows.flatMap((row) => Object.keys((row.data ?? {}) as object)))).slice(0, 8);

  return (
    <div className="page">
      <Link to="/research" className="back">← Research</Link>
      <PageHeader title="Research run" subtitle={String(r.question)} actions={<Pill value={r.status} />} />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="grid-2">
        <div className="card pad">
          <h3>Plan</h3>
          <ol className="plan">
            {plan.map((step, i) => (
              <li key={step.id ?? i} className="plan__step">
                <div><strong>{step.title ?? step.tool}</strong> {step.status && <Pill value={step.status} />}</div>
                <div className="muted small">{step.description ?? step.tool}</div>
                <div className="chips">
                  {step.spends_credits && <span className="chip chip--warn">spends credits{step.estimated_credits ? ` (~${step.estimated_credits})` : ""}</span>}
                  {step.mutates && <span className="chip chip--warn">changes CRM — proposal only</span>}
                </div>
              </li>
            ))}
          </ol>
          {r.status === "planned" && (
            <div className="form__actions">
              <label className="check"><input type="checkbox" checked={allowPaid} onChange={(e) => setAllowPaid(e.target.checked)} /> Allow paid provider credits for this run</label>
              <button className="button button--primary" disabled={action.busy} onClick={() => action.run(async () => { await client.post(`/research/runs/${runId}/approve`, { allow_paid: allowPaid }); run.refresh(); })}>Approve and run</button>
            </div>
          )}
        </div>
        <div className="card pad">
          <h3>Intent & sources</h3>
          <Json value={r.intent} />
          <h3>Estimated credits</h3>
          <Json value={r.estimated_credits} />
          <h3>Progress</h3>
          <p className="muted">{String(progress.message ?? "—")}</p>
        </div>
      </div>
      {r.summary ? <div className="card pad"><h3>Summary</h3><p>{String(r.summary)}</p></div> : null}
      <div className="card pad">
        <div className="title-row">
          <h3>Results ({fmt(r.result_count)})</h3>
          <button className="button button--ghost button--small" disabled={action.busy || resultRows.length === 0} onClick={() => action.run(() => client.post("/exports", { entity_type: "research_results", filters: { run_id: runId }, format: "xlsx" }))}>Export XLSX</button>
        </div>
        <DataTable
          rows={resultRows}
          empty="No results yet."
          columns={[
            { key: "rank", label: "#" },
            ...resultKeys.map((k) => ({ key: k, label: k.replace(/_/g, " "), render: (row: Row) => fmt(((row.data ?? {}) as Record<string, unknown>)[k]) })),
            { key: "score", label: "Score", render: (row: Row) => fmt(row.score) },
            { key: "evidence", label: "Evidence", render: (row: Row) => `${Array.isArray(row.evidence) ? row.evidence.length : 0} items` },
          ]}
        />
      </div>
      {proposals.length > 0 && (
        <div className="card pad">
          <h3>Proposed actions</h3>
          <p className="muted small">Nothing below has happened yet. Select what to apply.</p>
          <ul className="proposals">
            {proposals.map((p) => (
              <li key={p.id}>
                <label className="check">
                  <input type="checkbox" disabled={p.status === "applied"} checked={chosen.includes(p.id)} onChange={(e) => setChosen((c) => (e.target.checked ? [...c, p.id] : c.filter((x) => x !== p.id)))} />
                  <strong>{p.title ?? p.type ?? p.kind}</strong> {p.count !== undefined && <span className="muted">({p.count})</span>} {p.status && <Pill value={p.status} />}
                  {p.description && <span className="muted small"> — {p.description}</span>}
                </label>
              </li>
            ))}
          </ul>
          <button className="button button--primary" disabled={action.busy || chosen.length === 0} onClick={() => action.run(async () => { await client.post(`/research/runs/${runId}/actions`, { action_ids: chosen }); setChosen([]); run.refresh(); })}>Apply selected</button>
        </div>
      )}
    </div>
  );
}

// --- Imports ------------------------------------------------------------------------

export function Imports() {
  const client = useWs();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [target, setTarget] = useState("companies");
  const [files, setFiles] = useState<File[]>([]);
  const action = useAction();
  return (
    <div className="page">
      <PageHeader title="Imports" subtitle="Upload 12–30 CSV, XLSX or JSON files at once. Schemas are validated and compared; incompatible files are rejected with reasons; column meaning is never changed without your explicit mapping." />
      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            const batch = await client.post<Row>("/imports", { name: name || `Import ${new Date().toLocaleString()}`, target });
            await client.upload(`/imports/${batch.id}/files`, files);
            navigate(`/imports/${batch.id}`);
          });
        }}
      >
        <div className="field-row">
          <label className="field"><span className="field__label">Batch name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="field">
            <span className="field__label">Importing</span>
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="companies">Companies</option>
              <option value="contacts">Contacts</option>
              <option value="companies_and_contacts">Companies and contacts</option>
            </select>
          </label>
        </div>
        <label className="field field--wide">
          <span className="field__label">Files ({files.length} selected, up to 30)</span>
          <input type="file" multiple accept=".csv,.xlsx,.json" onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, 30))} />
        </label>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions"><button className="button button--primary" disabled={action.busy || files.length === 0}>{action.busy ? "Uploading…" : "Upload and validate"}</button></div>
      </form>
      <ResourceList
        load={(q, s) => client.list("/imports", q, s)}
        link={(r) => `/imports/${r.id}`}
        columns={[
          { key: "name", label: "Batch" },
          { key: "target", label: "Target" },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "file_count", label: "Files" },
          { key: "row_count", label: "Rows" },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
        ]}
      />
    </div>
  );
}

export function ImportDetail() {
  const { batchId = "" } = useParams();
  const client = useWs();
  const batch = useLoad((signal) => client.get<Row & { files?: Row[] }>(`/imports/${batchId}`, undefined, signal), client.base + batchId, 5000);
  const suggestions = useLoad((signal) => client.get<Record<string, unknown>>(`/imports/${batchId}/mapping-suggestions`, undefined, signal).catch(() => null), client.base + batchId + "s");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const action = useAction();
  if (!batch.data) return <div className="page">{batch.error ? <ErrorBanner error={batch.error} /> : <Loading />}</div>;
  const b = batch.data;
  const files = (b.files ?? []) as Row[];
  const columns = Array.from(new Set(files.flatMap((f) => (Array.isArray(f.columns) ? (f.columns as string[]) : []))));
  const sugg = ((suggestions.data?.suggestions ?? suggestions.data ?? {}) as Record<string, { target?: string; confidence?: number } | string>);
  const stats = (b.stats ?? {}) as Record<string, number>;

  return (
    <div className="page">
      <Link to="/imports" className="back">← Imports</Link>
      <PageHeader title={String(b.name)} subtitle={`${fmt(b.file_count)} files · ${fmt(b.row_count)} rows · target: ${String(b.target)}`} actions={<Pill value={b.status} />} />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="stats">
        {["created", "merged", "duplicates", "needs_review", "rejected"].map((k) => <Stat key={k} label={k.replace(/_/g, " ")} value={fmt(stats[k] ?? 0)} />)}
      </div>
      <div className="card pad">
        <div className="title-row">
          <h3>Files</h3>
          <button className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => { await client.post(`/imports/${batchId}/validate`); batch.refresh(); suggestions.refresh(); })}>Validate schemas</button>
        </div>
        <DataTable
          rows={files}
          columns={[
            { key: "filename", label: "File" },
            { key: "format", label: "Format" },
            { key: "row_count", label: "Rows" },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "problems", label: "Problems", render: (r) => (Array.isArray(r.problems) && r.problems.length ? <ul className="small">{(r.problems as unknown[]).map((p, i) => <li key={i}>{typeof p === "string" ? p : JSON.stringify(p)}</li>)}</ul> : <span className="muted">none</span>) },
          ]}
        />
      </div>
      <div className="card pad">
        <h3>Column mapping</h3>
        <p className="muted small">Suggestions are shown for reference only. Choose a target for each column you want merged; unmapped columns are kept in the original record but not merged.</p>
        <table className="table">
          <thead><tr><th>Source column</th><th>Suggestion</th><th>Map to</th></tr></thead>
          <tbody>
            {columns.map((col) => {
              const s = sugg[col];
              const suggested = typeof s === "string" ? s : s?.target;
              return (
                <tr key={col}>
                  <td className="mono small">{col}</td>
                  <td className="muted small">{suggested ? `${suggested}${typeof s === "object" && s?.confidence ? ` (${Math.round((s.confidence ?? 0) * 100)}%)` : ""}` : "—"}</td>
                  <td>
                    <input className="input input--small" placeholder={suggested ? `e.g. ${suggested}` : "leave empty to skip"} value={mapping[col] ?? ""} onChange={(e) => setMapping((m) => ({ ...m, [col]: e.target.value }))} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="form__actions">
          <button className="button button--ghost" disabled={action.busy} onClick={() => setMapping(Object.fromEntries(columns.map((c) => { const s = sugg[c]; return [c, (typeof s === "string" ? s : s?.target) ?? ""]; })))}>Copy suggestions into the form</button>
          <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(async () => { await client.put(`/imports/${batchId}/mapping`, { mapping: Object.fromEntries(Object.entries(mapping).map(([k, v]) => [k, v || null])) }); batch.refresh(); })}>Save mapping</button>
          <button className="button button--primary" disabled={action.busy || !["mapped", "validated"].includes(String(b.status))} onClick={() => action.run(async () => { await client.post(`/imports/${batchId}/merge`); batch.refresh(); })}>Merge into CRM</button>
        </div>
      </div>
    </div>
  );
}

// --- Exports, sources, credits, settings ---------------------------------------------------

export function Exports() {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const [entity, setEntity] = useState("companies");
  const [format, setFormat] = useState("xlsx");
  const action = useAction();
  return (
    <div className="page">
      <PageHeader title="Exports" subtitle="CSV, XLSX or JSON with source, import batch, timestamps and workspace provenance on every row." />
      <div className="card form form--inline">
        <div className="field-row">
          <label className="field"><span className="field__label">Data</span>
            <select className="input" value={entity} onChange={(e) => setEntity(e.target.value)}>
              {["companies", "contacts", "job_postings", "opportunities", "hiring_signals"].map((e) => <option key={e} value={e}>{e.replace(/_/g, " ")}</option>)}
            </select>
          </label>
          <label className="field"><span className="field__label">Format</span>
            <select className="input" value={format} onChange={(e) => setFormat(e.target.value)}>
              {["csv", "xlsx", "json"].map((f) => <option key={f} value={f}>{f.toUpperCase()}</option>)}
            </select>
          </label>
        </div>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions"><button className="button button--primary" disabled={action.busy} onClick={() => action.run(async () => { await client.post("/exports", { entity_type: entity, format, filters: {} }); setReload((n) => n + 1); })}>Create export</button></div>
      </div>
      <ResourceList
        reloadKey={String(reload)}
        load={(q, s) => client.list("/exports", q, s)}
        columns={[
          { key: "filename", label: "File" },
          { key: "entity_type", label: "Data" },
          { key: "format", label: "Format" },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "row_count", label: "Rows" },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
          { key: "download", label: "", render: (r) => (r.status === "completed" ? <button className="button button--small button--ghost" onClick={() => action.run(() => client.download(`/exports/${r.id}/download`, String(r.filename ?? `${r.id}.${r.format}`)))}>Download</button> : null) },
        ]}
      />
    </div>
  );
}

export function Sources() {
  const client = useWs();
  const { data, error, loading, refresh } = useLoad((signal) => client.get<{ items: Row[] } | Row[]>("/sources", undefined, signal), client.base + "sources");
  const items = (Array.isArray(data) ? data : data?.items ?? []) as Row[];
  return (
    <div className="page">
      <PageHeader title="Sources" subtitle="Where data can come from, and exactly what each one needs. A source is only marked working after a real check succeeds." />
      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !data ? <Loading /> : (
        <DataTable
          rows={items.map((s, i) => ({ ...s, id: String(s.id ?? s.name ?? i) }) as Row)}
          columns={[
            { key: "name", label: "Source" },
            { key: "kind", label: "Kind" },
            { key: "access_method", label: "Access" },
            { key: "status", label: "Status", render: (r) => <Pill value={(r.health as Row | undefined)?.status ?? r.status} /> },
            { key: "detail", label: "What it needs", render: (r) => String((r.health as Row | undefined)?.detail ?? r.detail ?? "") },
            { key: "requires", label: "Credentials", render: (r) => <Tags values={r.requires} /> },
          ]}
        />
      )}
    </div>
  );
}

export function Credits() {
  const client = useWs();
  const balances = useLoad((signal) => client.get<{ items: Row[] } | Row[]>("/credits", undefined, signal), client.base + "credits");
  const items = (Array.isArray(balances.data) ? balances.data : balances.data?.items ?? []) as Row[];
  return (
    <div className="page">
      <PageHeader title="Credits" subtitle="Paid provider credits per workspace. Credits are reserved before any paid call and only for an explicit action; data you already have is never bought again." />
      {balances.error && <ErrorBanner error={balances.error} />}
      <div className="stats">
        {items.map((b) => (
          <Stat key={String(b.provider)} label={String(b.provider)} value={fmt(b.remaining ?? (Number(b.total_credits ?? 0) - Number(b.consumed_credits ?? 0) - Number(b.reserved_credits ?? 0)))} hint={`${fmt(b.consumed ?? b.consumed_credits)} used · ${fmt(b.reserved ?? b.reserved_credits)} reserved · synced ${fmtDate(b.last_sync_at)}`} />
        ))}
        {items.length === 0 && !balances.loading && <p className="muted">No provider credit accounts yet. Connect a provider in Settings.</p>}
      </div>
      <h3>Ledger</h3>
      <ResourceList
        load={(q, s) => client.list("/credits/ledger", q, s)}
        columns={[
          { key: "provider", label: "Provider" },
          { key: "entry_type", label: "Entry", render: (r) => <Pill value={r.entry_type} /> },
          { key: "amount", label: "Amount", className: "tabular" },
          { key: "balance_after", label: "Balance after", className: "tabular" },
          { key: "reason", label: "Reason" },
          { key: "created_at", label: "When", render: (r) => fmt(r.created_at) },
        ]}
      />
    </div>
  );
}

/** The outcome of a provider's last live check, as stored by the server (never a secret). */
function connectionResult(r: Row): string {
  const last = (r.last_result ?? null) as Record<string, unknown> | null;
  if (!last) return r.last_error ? String(r.last_error) : "—";
  const parts = [String(last.detail ?? last.status ?? "")];
  if (last.credits_used !== undefined) parts.push(`${fmt(last.credits_used)} credits used`);
  if (last.credits !== undefined) parts.push(`balance ${fmt(last.credits)}`);
  else if (last.credits_remaining !== undefined) parts.push(`balance ${fmt(last.credits_remaining)}`);
  if (r.status === "error" && r.last_error) parts.push(String(r.last_error));
  return parts.filter(Boolean).join(" · ");
}

export function Settings() {
  const client = useWs();
  const { current, reload } = useWorkspace();
  const members = useLoad((signal) => client.get<{ items: Row[] }>("/members", undefined, signal), client.base + "members");
  const providers = useLoad((signal) => client.get<{ items: Row[] } | Row[]>("/providers", undefined, signal).catch(() => ({ items: [] })), client.base + "providers");
  const ai = useLoad((signal) => client.get<Record<string, unknown>>("/ai/providers", undefined, signal).catch(() => null), client.base + "ai");
  const action = useAction();
  const [provider, setProvider] = useState("zoominfo");
  const [fields, setFields] = useState<Record<string, string>>({});
  const providerRows = (Array.isArray(providers.data) ? providers.data : providers.data?.items ?? []) as Row[];
  // One input per credential the provider needs (ZoomInfo: client_id + client_secret), from the server catalogue.
  const requires = ((providerRows.find((p) => p.provider === provider)?.requires as string[] | undefined) ?? []).length
    ? (providerRows.find((p) => p.provider === provider)?.requires as string[])
    : ["api_key"];
  const complete = requires.every((name) => (fields[name] ?? "").trim());
  return (
    <div className="page">
      <PageHeader title="Settings" subtitle={`Workspace ${current?.name ?? ""} · your role: ${current?.role ?? "—"}`} />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="card pad">
        <h3>AI data policy</h3>
        <p className="muted small">When off, no private workspace data is sent to external AI providers; extraction and planning use deterministic rules only.</p>
        <label className="check">
          <input type="checkbox" checked={Boolean(current?.ai_external_allowed)} disabled={action.busy || !["owner", "admin"].includes(current?.role ?? "")} onChange={(e) => action.run(async () => { await client.patch("", { ai_external_allowed: e.target.checked }); reload(); })} />
          Allow external AI providers for this workspace
        </label>
        {ai.data && <Json value={ai.data} />}
      </div>
      <AiProviderSettings />
      <ProactiveSettings />
      <div className="card pad">
        <h3>Provider connections</h3>
        <p className="muted small">Secrets are write-only and encrypted at rest; they are never shown again or sent to the browser.</p>
        <DataTable
          rows={providerRows.map((p, i) => ({ ...p, id: String(p.id ?? i) }) as Row)}
          empty="No providers connected."
          columns={[
            { key: "label", label: "Provider", render: (r) => String(r.label ?? r.provider) },
            { key: "kind", label: "Category", render: (r) => String(r.kind ?? "").replace(/_/g, " ") },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "flags", label: "State", render: (r) => <Tags values={[r.configured ? "Configured" : "", r.verified ? "Verified" : "", r.enabled ? "Enabled" : ""].filter(Boolean)} /> },
            { key: "last_checked_at", label: "Last checked", render: (r) => fmtDate(r.last_checked_at) },
            { key: "masked_credential", label: "Credential", className: "mono small", render: (r) => String(r.masked_credential ?? "—") },
            { key: "last_result", label: "Connection result", render: (r) => connectionResult(r) },
            { key: "test", label: "", render: (r) => (r.configured
              ? <button className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => { await client.post(`/providers/${String(r.provider)}/verify`); providers.refresh(); })}>Test connection</button>
              : null) },
          ]}
        />
        <div className="field-row">
          <label className="field"><span className="field__label">Provider</span>
            <select className="input" value={provider} onChange={(e) => { setProvider(e.target.value); setFields({}); }}>
              {["zoominfo", "seamless", "emaillistverify", "claude", "gemini", "openai_compatible", "usajobs", "adzuna"].map((p) => <option key={p}>{p}</option>)}
            </select>
          </label>
          {requires.map((name) => (
            <label className="field" key={name}>
              <span className="field__label">{name.replace(/_/g, " ")}</span>
              <input className="input" type={/secret|key|token|password/.test(name) ? "password" : "text"} autoComplete="off" value={fields[name] ?? ""} onChange={(e) => setFields({ ...fields, [name]: e.target.value })} />
            </label>
          ))}
        </div>
        <div className="form__actions">
          <button className="button button--primary" disabled={action.busy || !complete} onClick={() => action.run(async () => {
            const secrets = Object.fromEntries(requires.map((name) => [name, (fields[name] ?? "").trim()]));
            await client.post(`/providers/${provider}/credentials`, { secrets });
            setFields({});
            providers.refresh();
          })}>Save credentials</button>
          <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(async () => { await client.post(`/providers/${provider}/verify`); providers.refresh(); })}>Verify</button>
        </div>
      </div>
      <div className="card pad">
        <h3>Members</h3>
        <DataTable rows={(members.data?.items ?? []).map((m) => ({ ...m, id: String(m.user_id) }) as Row)} columns={[{ key: "user_id", label: "User", className: "mono small" }, { key: "role", label: "Role", render: (r) => <Pill value={r.role} /> }]} />
        <KeyValues items={[["Workspace id", <span className="mono small">{current?.id}</span>], ["Slug", current?.slug]]} />
      </div>
    </div>
  );
}
