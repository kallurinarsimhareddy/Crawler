// SANA GTM AI Scraper: URLs (paste / CSV / TXT / XLSX) + an instruction -> schema preview (editable)
// -> advanced options -> work & cost estimate -> run (a persistent background job) -> live progress
// -> All / Companies / Jobs / Pages / Errors / Evidence / CRM -> CSV, XLSX, JSON, NDJSON.

import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { DataTable, PageHeader, Pill, ResourceList, Stat, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

const EXAMPLES = [
  "Get the company name, company website and all job post titles.",
  "Get company name, careers URL, ATS/platform and job titles.",
  "Get company name, website, location, industry and contact page.",
  "Find all job titles and job URLs from this page.",
];

const TYPES = ["string", "integer", "decimal", "boolean", "date", "datetime", "url", "email", "phone", "enum", "array", "object"];
const ACTIVE = ["queued", "planning", "fetching", "extracting", "paginating", "enriching", "validating", "normalizing", "saving", "running"];

interface Field {
  name: string;
  label?: string;
  type: string;
  level?: string;
  required?: boolean;
  source?: string;
  enum?: string[] | null;
  hint?: string | null;
  description?: string;
}

interface Schema {
  entity: string;
  entities?: string[];
  fields: Field[];
  filters?: { field: string; op: string; value: unknown; mode?: string }[];
  criteria?: Record<string, unknown>;
  custom?: string[];
  parser?: string;
  ai_note?: string;
  instruction?: string;
}

interface Options {
  max_pages: number;
  max_records: number;
  max_runtime_minutes: number;
  follow_details: boolean;
  browser: boolean;
  pagination: boolean;
  use_ai: boolean;
  concurrency: number;
}

interface Plan {
  inputs: { accepted: number; rejected: { row: number; url: string; reason: string }[]; report: Record<string, number> };
  sources: { url: string; row: number; label: string; kind: string; official_api?: boolean }[];
  schema: Schema;
  limits: Record<string, number>;
  browser: { requested: boolean; available: boolean; note?: string | null };
  estimate: { pages_max: number; requests_max: number; requests_typical: number; ai_calls_max: number; estimated_cost_usd: number | null; cost_note: string; ai_free_only: boolean };
  requires_confirmation: boolean;
  confirmation_reasons: string[];
}

interface Template {
  id: string;
  name: string;
  description?: string;
  category?: string;
  instruction: string;
  schema?: Schema | Record<string, never>;
  options?: Partial<Options> & { max_runtime_s?: number };
  builtin: boolean;
}

const DEFAULT_OPTIONS: Options = { max_pages: 25, max_records: 10000, max_runtime_minutes: 30, follow_details: false, browser: false, pagination: true, use_ai: true, concurrency: 4 };

const label = (name: string) => name.replace(/_/g, " ");

function FieldChips({ schema }: { schema: Schema }) {
  return (
    <div className="scraper-schema">
      <span className="muted small">{schema.entity === "job" ? "One row per job posting:" : "One row per company/page:"}</span>
      <span className="chips">
        {schema.fields.map((f) => (
          <span key={f.name} className="chip" title={`${f.type}${f.required ? ", required" : ""}${f.source === "custom" ? ", custom field" : ""}`}>
            {label(f.name)}
            <span className="muted"> · {f.type}</span>
            {f.required ? " *" : ""}
          </span>
        ))}
      </span>
      {schema.filters?.map((f) => (
        <span key={`${f.field}${f.op}`} className="chip chip--warn">
          {label(f.field)} {f.op === "within_days" ? `within ${String(f.value)} days` : `contains ${(f.value as string[]).join(" / ")}`}
        </span>
      ))}
      {schema.ai_note && <span className="muted small">{schema.ai_note}</span>}
    </div>
  );
}

function SchemaEditor({ schema, onChange }: { schema: Schema; onChange: (s: Schema) => void }) {
  const set = (fields: Field[]) => onChange({ ...schema, fields });
  const update = (i: number, patch: Partial<Field>) => set(schema.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  const move = (i: number, d: number) => {
    const fields = [...schema.fields];
    const [f] = fields.splice(i, 1);
    fields.splice(Math.max(0, Math.min(fields.length, i + d)), 0, f);
    set(fields);
  };
  return (
    <div className="table-wrap">
      <table className="table scraper-schema-editor">
        <thead>
          <tr>
            <th>Order</th>
            <th>Field</th>
            <th>Type</th>
            <th>Level</th>
            <th>Required</th>
            <th>Source</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {schema.fields.map((f, i) => (
            <tr key={i}>
              <td className="nowrap">
                <button type="button" className="button button--ghost button--small" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                <button type="button" className="button button--ghost button--small" aria-label="Move down" disabled={i === schema.fields.length - 1} onClick={() => move(i, 1)}>↓</button>
              </td>
              <td>
                <input className="input input--small" value={f.name} aria-label="Field name" onChange={(e) => update(i, { name: e.target.value })} />
                {f.type === "enum" && (
                  <input className="input input--small" placeholder="Allowed values, comma-separated" value={(f.enum ?? []).join(", ")} onChange={(e) => update(i, { enum: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} />
                )}
              </td>
              <td>
                <select className="input input--small" value={f.type} aria-label="Type" onChange={(e) => update(i, { type: e.target.value })}>
                  {TYPES.map((t) => <option key={t}>{t}</option>)}
                </select>
              </td>
              <td>
                <select className="input input--small" value={f.level ?? "company"} aria-label="Level" onChange={(e) => update(i, { level: e.target.value })}>
                  <option value="company">company</option>
                  <option value="job">job</option>
                </select>
              </td>
              <td><input type="checkbox" checked={Boolean(f.required)} aria-label="Required" onChange={(e) => update(i, { required: e.target.checked })} /></td>
              <td className="small muted">{f.source ?? "user"}</td>
              <td><button type="button" className="button button--ghost button--small" onClick={() => set(schema.fields.filter((_, j) => j !== i))}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="button button--ghost button--small" onClick={() => set([...schema.fields, { name: `field_${schema.fields.length + 1}`, type: "string", level: schema.entity === "job" ? "job" : "company", source: "user" }])}>
        + Add field
      </button>
    </div>
  );
}

function OptionsPanel({ options, onChange, bare = false }: { options: Options; onChange: (o: Options) => void; bare?: boolean }) {
  const num = (key: keyof Options, text: string, min: number, max: number) => (
    <label className="field">
      <span className="field__label">{text}</span>
      <input className="input" type="number" min={min} max={max} value={options[key] as number} onChange={(e) => onChange({ ...options, [key]: Number(e.target.value) })} />
    </label>
  );
  const box = (key: keyof Options, text: string) => (
    <label className="checkbox">
      <input type="checkbox" checked={options[key] as boolean} onChange={(e) => onChange({ ...options, [key]: e.target.checked })} />
      <span>{text}</span>
    </label>
  );
  const body = (
    <>
      <div className="field-row">
        {num("max_pages", "Max pages per URL", 1, 500)}
        {num("max_records", "Max records", 1, 100000)}
        {num("max_runtime_minutes", "Max runtime (minutes)", 1, 360)}
        {num("concurrency", "URLs at once", 1, 16)}
      </div>
      <div className="scraper-checks">
        {box("pagination", "Follow pagination / load-more links")}
        {box("follow_details", "Open each job's detail page")}
        {box("browser", "Browser rendering fallback (JavaScript pages)")}
        {box("use_ai", "AI extraction when rules can't find a field (free tier, $0)")}
      </div>
    </>
  );
  if (bare) return body;
  return (
    <details className="scraper-advanced">
      <summary>Advanced options</summary>
      {body}
    </details>
  );
}

function Templates({ onPick, reloadKey }: { onPick: (t: Template) => void; reloadKey: number }) {
  const client = useWs();
  const list = useLoad((signal) => client.get<{ items: Template[] }>("/scraper/templates", undefined, signal), client.base + "tpl" + reloadKey);
  const action = useAction();
  const items = list.data?.items ?? [];
  return (
    <div className="scraper-templates">
      <span className="field__label">Templates</span>
      <div className="chips">
        {items.map((t) => (
          <span key={t.id} className="chip chip--button-group">
            <button type="button" className="chip chip--button" title={t.description} onClick={() => onPick(t)}>{t.name}</button>
            <button type="button" className="chip chip--button" title="Duplicate" onClick={() => void action.run(async () => { await client.post(`/scraper/templates/${t.id}/duplicate`, {}); list.refresh(); })}>⧉</button>
            {!t.builtin && (
              <button type="button" className="chip chip--button" title="Delete" onClick={() => void action.run(async () => { await client.del(`/scraper/templates/${t.id}`); list.refresh(); })}>✕</button>
            )}
          </span>
        ))}
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

function toOptionsPayload(o: Options) {
  return { ...o, max_runtime_minutes: o.max_runtime_minutes };
}

type Step = "input" | "schema" | "filters" | "limits" | "review";

const STEPS: { key: Step; label: string }[] = [
  { key: "input", label: "1 · Input" },
  { key: "schema", label: "2 · Schema" },
  { key: "filters", label: "3 · Filters" },
  { key: "limits", label: "4 · Limits" },
  { key: "review", label: "5 · Review" },
];

export function Scraper() {
  const client = useWs();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [urls, setUrls] = useState(() => params.get("urls") ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [column, setColumn] = useState("");
  const [instruction, setInstruction] = useState(EXAMPLES[0]);
  const [options, setOptions] = useState<Options>(DEFAULT_OPTIONS);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [stale, setStale] = useState(false);
  const [step, setStep] = useState<Step>("input");
  const [schema, setSchema] = useState<Schema | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [tplKey, setTplKey] = useState(0);
  const action = useAction();
  const urlCount = urls.split(/\n/).filter((line) => line.trim()).length;
  const ready = instruction.trim() && (urlCount > 0 || file);

  // Changing the input keeps the plan on screen but marks it out of date: the run
  // always uses a plan that matches what you see.
  const invalidate = () => {
    if (plan) setStale(true);
    setConfirm(false);
  };

  const body = (withSchema: boolean) => ({
    urls,
    instruction,
    options: toOptionsPayload(options),
    ...(withSchema && schema ? { schema } : {}),
    ...(templateId ? { template_id: templateId } : {}),
  });

  const formFields = (withSchema: boolean): Record<string, string> => ({
    instruction,
    urls,
    column,
    options: JSON.stringify(toOptionsPayload(options)),
    ...(withSchema && schema ? { schema: JSON.stringify(schema) } : {}),
    ...(templateId ? { template_id: templateId } : {}),
  });

  const preview = () =>
    action.run(async () => {
      const result = file ? await client.upload<Plan>("/scraper/plan", [file], formFields(Boolean(schema))) : await client.post<Plan>("/scraper/plan", body(Boolean(schema)));
      const first = plan === null;
      setPlan(result);
      setSchema(result.schema);
      setStale(false);
      setConfirm(false);
      if (first) setStep("schema");
    });

  const run = () =>
    action.run(async () => {
      const fields = { ...formFields(true), confirm: String(confirm) };
      const started = file ? await client.upload<Row>("/scraper/runs", [file], fields) : await client.post<Row>("/scraper/runs", { ...body(true), confirm });
      navigate(`/scraper/${started.id}`);
    });

  const pickTemplate = (t: Template) => {
    setInstruction(t.instruction);
    setTemplateId(t.id);
    const o = t.options ?? {};
    setOptions({ ...DEFAULT_OPTIONS, ...o, max_runtime_minutes: o.max_runtime_s ? Math.round(o.max_runtime_s / 60) : DEFAULT_OPTIONS.max_runtime_minutes } as Options);
    setSchema(t.schema && "fields" in t.schema ? (t.schema as Schema) : null);
    invalidate();
  };

  const setInstructionText = (text: string) => {
    setInstruction(text);
    setSchema(null);
    setTemplateId(null);
    invalidate();
  };

  const inputs = (
    <>
      <label className="field field--wide">
        <span className="field__label">What do you want to collect?</span>
        <textarea className="input textarea scraper-ask" rows={2} value={instruction} onChange={(e) => setInstructionText(e.target.value)} placeholder="Get company name, website and job titles…" />
      </label>
      <div className="chips">
        {EXAMPLES.map((example) => (
          <button key={example} type="button" className="chip chip--button" onClick={() => setInstructionText(example)}>
            {example}
          </button>
        ))}
      </div>
      <div className="scraper-sources">
        <label className="field field--wide">
          <span className="field__label">From these URLs</span>
          <textarea className="input textarea mono" rows={4} value={urls} onChange={(e) => { setUrls(e.target.value); invalidate(); }} placeholder={"https://example1.com\nhttps://example2.com\nhttps://example3.com"} />
          <span className="field__hint">{urlCount ? `${urlCount} line${urlCount === 1 ? "" : "s"}` : "One per line"}</span>
        </label>
        <div className="scraper-upload">
          <label className="field">
            <span className="field__label">…or upload CSV / TXT / XLSX</span>
            <input type="file" accept=".csv,.xlsx,.txt,.tsv" onChange={(e) => { setFile(e.target.files?.[0] ?? null); invalidate(); }} />
          </label>
          <label className="field">
            <span className="field__label">URL column (optional)</span>
            <input className="input" value={column} onChange={(e) => { setColumn(e.target.value); invalidate(); }} placeholder="Detected automatically" />
          </label>
        </div>
      </div>
      <details className="scraper-advanced">
        <summary>Start from a template</summary>
        <Templates onPick={pickTemplate} reloadKey={tplKey} />
      </details>
    </>
  );

  const estimate = plan && (
    <div className="scraper-estimate" aria-live="polite">
      <span><strong className="tabular">{fmt(plan.inputs.accepted)}</strong> URLs</span>
      <span>~<strong className="tabular">{fmt(plan.estimate.requests_typical)}</strong> requests <span className="muted">(max {fmt(plan.estimate.requests_max)})</span></span>
      <span><strong className="tabular">{fmt(plan.estimate.ai_calls_max)}</strong> AI calls max</span>
      <span>AI cost <strong>{plan.estimate.estimated_cost_usd === null ? "?" : `$${plan.estimate.estimated_cost_usd.toFixed(2)}`}</strong></span>
    </div>
  );

  return (
    <div className="page">
      <PageHeader
        title="AI Scraper"
        subtitle="Say what to collect and where from. Structured data, official job-board APIs and page links are read first; AI (Gemini free tier, $0) only fills what they can't. Pages behind a login, CAPTCHA or firewall are reported, never bypassed."
      />

      {!plan ? (
        <section className="card form scraper-landing" aria-label="What do you want to collect?">
          {inputs}
          {action.error && <ErrorBanner error={action.error} />}
          <div className="form__actions scraper-landing__actions">
            <span className="muted small">Next: review the fields, filters, limits and cost before anything runs.</span>
            <button type="button" className="button button--primary button--large" disabled={action.busy || !ready} onClick={() => void preview()}>
              {action.busy ? "Planning…" : "Preview Extraction Plan"}
            </button>
          </div>
        </section>
      ) : (
        <section className="card scraper-wizard" aria-label="Extraction plan">
          <Tabs tabs={STEPS} active={step} onChange={(k) => setStep(k as Step)} />
          {stale && (
            <div className="alert alert--info">
              <span>The input changed since this plan was made.</span>
              <button type="button" className="button button--ghost button--small" disabled={action.busy || !ready} onClick={() => void preview()}>Re-check plan</button>
            </div>
          )}
          <div className="scraper-step">
            {step === "input" && inputs}
            {step === "schema" && schema && (
              <>
                <FieldChips schema={schema} />
                <SchemaEditor schema={schema} onChange={(s) => { setSchema(s); setConfirm(false); }} />
              </>
            )}
            {step === "filters" && schema && (
              <>
                {(schema.filters ?? []).length === 0 ? (
                  <EmptyState
                    icon="filter"
                    title="No filters — every row is kept"
                    description='Filters come from your instruction. Add a condition such as "posted in the last 30 days" or "titles containing SAP" on the Input step and re-check the plan.'
                    action={<button type="button" className="button button--ghost button--small" onClick={() => setStep("input")}>Edit the instruction</button>}
                  />
                ) : (
                  <>
                    <p className="small muted">Rows that fail a filter are kept out of the results (and counted), never silently changed.</p>
                    <div className="chips">
                      {(schema.filters ?? []).map((f, i) => (
                        <span key={`${f.field}${f.op}${i}`} className="chip chip--warn">
                          {label(f.field)} {f.op === "within_days" ? `within ${String(f.value)} days` : `contains ${(f.value as string[]).join(" / ")}`}
                          <button type="button" className="chip__x" aria-label={`Remove filter on ${label(f.field)}`} onClick={() => { setSchema({ ...schema, filters: (schema.filters ?? []).filter((_, j) => j !== i) }); setConfirm(false); }}>×</button>
                        </span>
                      ))}
                    </div>
                  </>
                )}
                {schema.criteria && Object.keys(schema.criteria).length > 0 && (
                  <p className="small muted">Research criteria (shown, never used to drop rows): {JSON.stringify(schema.criteria)}</p>
                )}
              </>
            )}
            {step === "limits" && (
              <>
                <p className="small muted">Every run is bounded. Changing a limit re-checks the estimate before the run.</p>
                <OptionsPanel bare options={options} onChange={(o) => { setOptions(o); invalidate(); }} />
                <p className="small muted">
                  Browser rendering: {plan.browser.requested ? (plan.browser.available ? "on" : "requested, unavailable on this server") : "off"}
                  {plan.browser.note ? ` — ${plan.browser.note}` : ""}
                </p>
              </>
            )}
            {step === "review" && (
              <>
                <h3 className="section-title">Sources</h3>
                <DataTable
                  rows={plan.sources.slice(0, 20).map((s, i) => ({ ...s, id: String(i) }) as unknown as Row)}
                  columns={[
                    { key: "row", label: "Row" },
                    { key: "url", label: "URL", className: "mono small" },
                    { key: "label", label: "Detected source" },
                  ]}
                />
                {plan.inputs.rejected.length > 0 && <p className="small">{plan.inputs.rejected.length} inputs will be skipped: {plan.inputs.rejected.slice(0, 5).map((r) => `row ${r.row} (${r.reason})`).join(", ")}</p>}
                <h3 className="section-title">Work & cost estimate</h3>
                <div className="stats">
                  <Stat label="URLs" value={fmt(plan.inputs.accepted)} />
                  <Stat label="Pages (max)" value={fmt(plan.estimate.pages_max)} />
                  <Stat label="Requests" value={`~${fmt(plan.estimate.requests_typical)}`} hint={`max ${fmt(plan.estimate.requests_max)}`} />
                  <Stat label="AI calls (max)" value={fmt(plan.estimate.ai_calls_max)} />
                  <Stat label="Estimated AI cost" value={plan.estimate.estimated_cost_usd === null ? "?" : `$${plan.estimate.estimated_cost_usd.toFixed(2)}`} hint={plan.estimate.cost_note} />
                </div>
                <p className="small muted">
                  Limits: {plan.limits.max_pages} pages per URL · {fmt(plan.limits.max_records)} records · {Math.round(plan.limits.max_runtime_s / 60)} min
                </p>
                {plan.requires_confirmation && (
                  <label className="checkbox scraper-confirm">
                    <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
                    <span>High-volume run ({plan.confirmation_reasons.join("; ")}). I confirm.</span>
                  </label>
                )}
                <div className="form__actions">
                  <input className="input input--small" placeholder="Template name" value={templateName} onChange={(e) => setTemplateName(e.target.value)} />
                  <button type="button" className="button button--ghost" disabled={action.busy || !templateName.trim()} onClick={() => void action.run(async () => { await client.post("/scraper/templates", { name: templateName, instruction, schema, options: toOptionsPayload(options) }); setTemplateName(""); setTplKey((k) => k + 1); })}>
                    Save as template
                  </button>
                </div>
              </>
            )}
          </div>
          {action.error && <ErrorBanner error={action.error} />}
          <div className="scraper-footer">
            {estimate}
            <div className="actions">
              {step !== "review" && (
                <button type="button" className="button button--ghost" onClick={() => setStep(STEPS[STEPS.findIndex((s) => s.key === step) + 1].key)}>
                  Next
                </button>
              )}
              {stale ? (
                <button type="button" className="button button--primary" disabled={action.busy || !ready} onClick={() => void preview()}>
                  {action.busy ? "Planning…" : "Re-check plan"}
                </button>
              ) : (
                <button type="button" className="button button--primary button--large" disabled={action.busy || (plan.requires_confirmation && !confirm)} onClick={() => void run()} title={plan.requires_confirmation && !confirm ? "Confirm the high-volume run on the Review step" : undefined}>
                  {action.busy ? "Starting…" : "Run Scraper"}
                </button>
              )}
            </div>
          </div>
        </section>
      )}

      <h2 className="section-title">Recent runs</h2>
      <ResourceList
        load={(q, s) => client.list("/scraper/runs", q, s)}
        link={(r) => `/scraper/${r.id}`}
        empty={{ title: "No scraper runs yet", description: "Runs appear here with their status, record counts and downloads.", icon: "scraper" }}
        columns={[
          { key: "instruction", label: "Instruction" },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "url_count", label: "URLs", render: (r) => fmt((r.stats as Row | undefined)?.url_count) },
          { key: "records", label: "Records", render: (r) => fmt((r.stats as Row | undefined)?.records ?? ((r.stats as Row | undefined)?.progress as Row | undefined)?.records) },
          { key: "created_at", label: "Started", render: (r) => fmtDate(r.created_at) },
        ]}
      />
    </div>
  );
}

// --- one run --------------------------------------------------------------------------------

type View = "all" | "companies" | "jobs" | "pages" | "errors" | "evidence" | "crm";

function ProgressPanel({ run }: { run: Row }) {
  const stats = (run.stats ?? {}) as Row;
  const p = (stats.progress ?? {}) as Row;
  const obs = (stats.observability ?? {}) as Row;
  const total = Number(p.total ?? stats.url_count ?? 0);
  const done = Number(p.processed ?? 0);
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const status = String(run.status);
  const active = ACTIVE.includes(status);
  const current = (p.current_urls as string[] | undefined) ?? [];
  return (
    <div className="card pad">
      <div className="progress">
        <div className="progress__track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
          <div className={`progress__fill progress__fill--${active ? "running" : status}`} style={{ width: `${percent}%` }} />
        </div>
        <div className="progress__meta">
          <span>
            Stage: <strong>{String(p.stage ?? status)}</strong>
          </span>
          <span className="tabular">
            {done} / {total} · {percent}%
          </span>
        </div>
      </div>
      <div className="stats scraper-stats">
        <Stat label="URLs" value={`${done} / ${total}`} />
        <Stat label="Pages" value={fmt(p.pages ?? obs.pages_visited ?? 0)} />
        <Stat label="Records" value={fmt(stats.records ?? p.records ?? 0)} />
        <Stat label="Jobs" value={fmt(p.jobs ?? obs.jobs_found ?? "—")} />
        <Stat label="Companies" value={fmt(p.companies ?? obs.companies_found ?? "—")} />
        <Stat label="Requests" value={fmt(p.requests ?? obs.requests ?? 0)} />
        <Stat label="Completed" value={fmt(p.completed ?? 0)} />
        <Stat label="Blocked" value={fmt(p.blocked ?? obs.blocked_urls ?? 0)} />
        <Stat label="Errors" value={fmt(p.failed ?? 0)} hint={stats.errors !== undefined ? `${fmt(stats.errors)} notes` : undefined} />
        <Stat label="AI calls" value={fmt(p.ai_calls ?? stats.ai_calls ?? 0)} hint={`${fmt(p.ai_failures ?? stats.ai_failures ?? 0)} failed`} />
        <Stat label="Browser pages" value={fmt(p.browser_pages ?? obs.browser_pages ?? 0)} />
        {obs.duration_seconds !== undefined && <Stat label="Duration" value={`${fmt(obs.duration_seconds)} s`} />}
      </div>
      {active && current.length > 0 && (
        <p className="small">
          Current: {current.map((u) => <span key={u} className="mono">{u} </span>)}
        </p>
      )}
      {Boolean(p.ai_note || stats.ai_note) && <p className="small muted">AI: {String(p.ai_note ?? stats.ai_note)}</p>}
      {run.error ? <p className="small error-text">{String(run.error)}</p> : null}
    </div>
  );
}

function Cell({ value, type }: { value: unknown; type?: string }) {
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  if (Array.isArray(value)) return <span>{value.map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v))).join(" | ")}</span>;
  if (typeof value === "object") return <code className="small">{JSON.stringify(value)}</code>;
  const text = String(value);
  if (/^https?:\/\//.test(text) && (type === "url" || type === undefined || /^https?:\/\//.test(text))) {
    return (
      <a className="link mono small" href={text} target="_blank" rel="noopener noreferrer nofollow">
        {text.length > 70 ? text.slice(0, 70) + "…" : text}
      </a>
    );
  }
  return <span>{fmt(value)}</span>;
}

function RecordsTable({ run, view }: { run: Row; view: "all" | "companies" | "jobs" }) {
  const client = useWs();
  const schema = run.schema as Schema;
  const types = Object.fromEntries(schema.fields.map((f) => [f.name, f.type]));
  const records = useLoad(
    (signal) => client.get<{ columns: string[]; items: Row[]; total: number; final: boolean }>(`/scraper/runs/${run.id}/records`, { view, limit: 2000 }, signal),
    `${client.base}${run.id}${view}${String(run.status)}${String(run.updated_at)}`,
  );
  if (records.error) return <ErrorBanner error={records.error} />;
  if (!records.data) return <Loading />;
  const columns = records.data.columns.filter((c) => c !== "extracted_at");
  return (
    <DataTable
      rows={records.data.items.map((r, i) => ({ ...r, id: `${i}` }))}
      empty={records.data.final ? "No records." : "Results appear here when the run finishes."}
      columns={columns.map((c) => ({
        key: c,
        label: label(c),
        className: c === "confidence" || c === "job_count" ? "tabular" : undefined,
        render: (r: Row) => {
          const status = ((r._field_status ?? {}) as Record<string, string>)[c];
          const cell = <Cell value={r[c]} type={types[c]} />;
          return status && status !== "valid" && status !== "missing" ? (
            <span title={status}>
              {cell} <span className={`chip ${status === "invalid" || status === "conflict" ? "chip--warn" : ""}`}>{status}</span>
            </span>
          ) : c === "source_url" && Array.isArray(r.source_urls) && (r.source_urls as string[]).length > 1 ? (
            <span title={(r.source_urls as string[]).join("\n")}>
              {cell} <span className="muted small">+{(r.source_urls as string[]).length - 1}</span>
            </span>
          ) : (
            cell
          );
        },
      }))}
    />
  );
}

function PagesTable({ run }: { run: Row }) {
  const client = useWs();
  const pages = useLoad((signal) => client.get<{ items: Row[]; total: number }>(`/scraper/runs/${run.id}/pages`, { limit: 1000 }, signal), `${client.base}${run.id}pages${String(run.updated_at)}`);
  return (
    <DataTable
      rows={pages.data?.items ?? []}
      empty="No pages visited yet."
      columns={[
        { key: "input_index", label: "Input", render: (r) => fmt(Number(r.input_index) + 1) },
        { key: "url", label: "URL", render: (r) => <Cell value={r.url} type="url" /> },
        { key: "kind", label: "Kind" },
        { key: "outcome", label: "Outcome", render: (r) => <Pill value={r.outcome} /> },
        { key: "http_status", label: "HTTP" },
        { key: "records", label: "Records" },
        { key: "attempts", label: "Attempts" },
        { key: "browser_used", label: "Browser", render: (r) => (r.browser_used ? <span title={String(r.browser_reason ?? "")}>yes · {fmt(r.browser_duration_ms)} ms</span> : "—") },
        { key: "error", label: "Notes", className: "small" },
      ]}
    />
  );
}

function ErrorsTable({ run }: { run: Row }) {
  const client = useWs();
  const errors = useLoad((signal) => client.get<{ items: Row[]; total: number }>(`/scraper/runs/${run.id}/errors`, undefined, signal), `${client.base}${run.id}errors${String(run.updated_at)}`);
  return (
    <DataTable
      rows={(errors.data?.items ?? []).map((r, i) => ({ ...r, id: String(i) }) as Row)}
      empty={run.stats && (run.stats as Row).files ? "No errors." : "Errors appear when the run finishes."}
      columns={[
        { key: "kind", label: "Kind" },
        { key: "input_row", label: "Row" },
        { key: "url", label: "URL", render: (r) => <Cell value={r.url} type="url" /> },
        { key: "outcome", label: "Outcome", render: (r) => <Pill value={r.outcome} /> },
        { key: "field", label: "Field" },
        { key: "error", label: "Error", className: "small" },
      ]}
    />
  );
}

function EvidenceTable({ run }: { run: Row }) {
  const client = useWs();
  const evidence = useLoad((signal) => client.get<{ items: Row[]; total: number }>(`/scraper/runs/${run.id}/evidence`, { limit: 200 }, signal), `${client.base}${run.id}evidence${String(run.updated_at)}`);
  const rows: Row[] = [];
  (evidence.data?.items ?? []).forEach((item, i) => {
    Object.entries((item.fields ?? {}) as Record<string, Row>).forEach(([name, info]) => {
      rows.push({ ...info, id: `${i}-${name}`, record: item.label, field: name, status: ((item.status ?? {}) as Record<string, string>)[name] });
    });
  });
  return (
    <DataTable
      rows={rows}
      empty="Evidence appears when the run finishes."
      columns={[
        { key: "record", label: "Record" },
        { key: "field", label: "Field", render: (r) => label(String(r.field)) },
        { key: "value", label: "Value", render: (r) => <Cell value={r.value} /> },
        { key: "method", label: "Method" },
        { key: "confidence", label: "Confidence", className: "tabular" },
        { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
        { key: "evidence", label: "Evidence", className: "small" },
        { key: "source_url", label: "Source", render: (r) => <Cell value={r.source_url} type="url" /> },
        { key: "alternatives", label: "Conflicting values", render: (r) => <Cell value={(r.alternatives as Row[] | undefined)?.map((a) => `${String(a.value)} (${String(a.method)})`)} /> },
      ]}
    />
  );
}

function CrmPanel({ run }: { run: Row }) {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const [actions, setActions] = useState<string[]>(["company", "job"]);
  const [selected, setSelected] = useState<string[]>([]);
  const match = useLoad((signal) => client.get<{ items: Row[]; summary: Record<string, number> }>(`/scraper/runs/${run.id}/crm/match`, undefined, signal), `${client.base}${run.id}match${reload}`);
  const proposals = useLoad((signal) => client.get<{ items: Row[] }>(`/scraper/runs/${run.id}/proposals`, undefined, signal), `${client.base}${run.id}prop${reload}`);
  const action = useAction();
  const toggle = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const done = () => { setSelected([]); setReload((n) => n + 1); };
  return (
    <div className="pad">
      <p className="small muted">Matching is read-only. Proposals change nothing until you approve them and press Apply.</p>
      {match.error && <ErrorBanner error={match.error} />}
      <div className="chips">
        {Object.entries(match.data?.summary ?? {}).map(([k, v]) => <span key={k} className="chip">{label(k)}: {v}</span>)}
      </div>
      <DataTable
        rows={(match.data?.items ?? []).map((r, i) => ({ ...r, id: String(i) }) as Row)}
        empty="No companies to match."
        columns={[
          { key: "company_name", label: "Company" },
          { key: "website", label: "Website", render: (r) => <Cell value={r.website} type="url" /> },
          { key: "match", label: "CRM", render: (r) => <Pill value={r.match} /> },
          { key: "crm_name", label: "CRM record" },
          { key: "conflicts", label: "Conflicts", render: (r) => <Cell value={Object.keys((r.conflicts ?? {}) as object)} /> },
        ]}
      />
      <div className="form__actions">
        {["company", "job", "contact", "opportunity", "task"].map((a) => (
          <label key={a} className="checkbox">
            <input type="checkbox" checked={actions.includes(a)} onChange={(e) => setActions((s) => (e.target.checked ? [...s, a] : s.filter((x) => x !== a)))} />
            <span>{a}</span>
          </label>
        ))}
        <button type="button" className="button button--ghost" disabled={action.busy || !actions.length} onClick={() => void action.run(async () => { await client.post(`/scraper/runs/${run.id}/crm/propose`, { actions }); done(); })}>
          Propose CRM changes
        </button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <DataTable
        rows={proposals.data?.items ?? []}
        empty="No proposals yet."
        columns={[
          { key: "select", label: "", render: (r) => <input type="checkbox" aria-label="Select" checked={selected.includes(String(r.id))} disabled={r.status === "applied"} onChange={() => toggle(String(r.id))} /> },
          { key: "action", label: "Action" },
          { key: "record_key", label: "Record", className: "small" },
          { key: "match", label: "Match", render: (r) => <Pill value={r.match} /> },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "error", label: "Notes", className: "small" },
        ]}
      />
      <div className="form__actions">
        <button type="button" className="button button--ghost" disabled={!selected.length || action.busy} onClick={() => void action.run(async () => { await client.post("/scraper/proposals/review", { ids: selected, decision: "approved" }); done(); })}>Approve</button>
        <button type="button" className="button button--ghost" disabled={!selected.length || action.busy} onClick={() => void action.run(async () => { await client.post("/scraper/proposals/review", { ids: selected, decision: "rejected" }); done(); })}>Reject</button>
        <button type="button" className="button button--primary" disabled={!selected.length || action.busy} onClick={() => void action.run(async () => { await client.post("/scraper/proposals/apply", { ids: selected }); done(); })}>Apply approved to CRM</button>
      </div>
    </div>
  );
}

export function ScrapeRun() {
  const { runId = "" } = useParams();
  const client = useWs();
  const navigate = useNavigate();
  const [active, setActive] = useState(true);
  const [view, setView] = useState<View | null>(null);
  const [templateName, setTemplateName] = useState("");
  const run = useLoad(
    async (signal) => {
      const loaded = await client.get<Row>(`/scraper/runs/${runId}`, undefined, signal);
      setActive(ACTIVE.includes(String(loaded.status)) || loaded.status === "paused");
      return loaded;
    },
    client.base + runId,
    active ? 2000 : undefined,
  );
  const action = useAction();
  useEffect(() => {
    if (run.data && view === null) setView((run.data.schema as Schema).entity === "job" ? "jobs" : "companies");
  }, [run.data, view]);
  if (!run.data) return <div className="page">{run.error ? <ErrorBanner error={run.error} /> : <Loading />}</div>;
  const data = run.data;
  const stats = (data.stats ?? {}) as Row;
  const schema = data.schema as Schema;
  const status = String(data.status);
  const files = (stats.files ?? {}) as Record<string, unknown>;
  const current: View = view ?? "all";
  const tabs: { key: View; label: string }[] = [
    { key: "all", label: "All" },
    { key: "companies", label: "Companies" },
    ...(schema.entity === "job" ? [{ key: "jobs" as View, label: "Jobs" }] : []),
    { key: "pages", label: "Pages" },
    { key: "errors", label: "Errors" },
    { key: "evidence", label: "Evidence" },
    { key: "crm", label: "CRM" },
  ];
  const act = (name: string) =>
    action.run(async () => {
      const result = await client.post<Row>(`/scraper/runs/${runId}/${name}`);
      if (name === "restart") navigate(`/scraper/${result.id}`);
      setActive(true);
      run.refresh();
    });
  const download = (fmtName: "csv" | "xlsx" | "json" | "ndjson") =>
    action.run(() => {
      const csvView = fmtName === "csv" && ["companies", "jobs", "pages", "errors"].includes(current) ? current : "all";
      const name = `scrape-${runId}${fmtName === "csv" && csvView !== "all" ? "-" + csvView : ""}.${fmtName}`;
      return client.download(`/scraper/runs/${runId}/files/${fmtName}${csvView !== "all" ? `?view=${csvView}` : ""}`, name);
    });
  const isActive = ACTIVE.includes(status);
  return (
    <div className="page">
      <PageHeader
        title="Scrape run"
        subtitle={String(data.instruction)}
        actions={
          <>
            <Pill value={status} />
            {isActive && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void act("pause")}>Pause</button>}
            {status === "paused" && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void act("resume")}>Resume</button>}
            {(isActive || status === "paused") && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void act("cancel")}>Cancel</button>}
            {["failed", "cancelled", "completed"].includes(status) && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void act("retry")}>Retry</button>}
            {["failed", "cancelled", "completed"].includes(status) && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void act("restart")}>Restart</button>}
          </>
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      <FieldChips schema={schema} />
      <ProgressPanel run={data} />
      <div className="card">
        <div className="card__header scraper-results__header">
          <Tabs tabs={tabs.map((t) => ({ key: t.key, label: t.label }))} active={current} onChange={(k) => setView(k as View)} />
          <div className="scraper-downloads">
            {(["csv", "xlsx", "json", "ndjson"] as const).map((f) => (
              <button key={f} type="button" className="button button--ghost button--small" disabled={action.busy || !files[f]} onClick={() => void download(f)}>
                {f.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        {current === "all" || current === "companies" || current === "jobs" ? <RecordsTable run={data} view={current} /> : null}
        {current === "pages" && <PagesTable run={data} />}
        {current === "errors" && <ErrorsTable run={data} />}
        {current === "evidence" && <EvidenceTable run={data} />}
        {current === "crm" && (files.json ? <CrmPanel run={data} /> : <p className="pad muted">CRM matching is available when the run finishes.</p>)}
      </div>
      <div className="form__actions">
        <input className="input input--small" placeholder="Template name" value={templateName} onChange={(e) => setTemplateName(e.target.value)} />
        <button type="button" className="button button--ghost button--small" disabled={action.busy || !templateName.trim()} onClick={() => void action.run(async () => { await client.post("/scraper/templates", { name: templateName, instruction: data.instruction, schema, options: stats.options }); setTemplateName(""); })}>
          Save as template
        </button>
      </div>
    </div>
  );
}
