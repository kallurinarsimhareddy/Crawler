// The AI scraper: URLs (pasted or CSV/XLSX) + a plain-language instruction -> a run
// with live progress -> Companies / Jobs / All-fields results -> CSV, XLSX, JSON.

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { DataTable, PageHeader, Pill, ResourceList, Stat, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

const EXAMPLES = [
  "Get the company name, company website and all job post titles.",
  "Get company name, careers URL, ATS/platform and job titles.",
  "Get company name, website, location, industry and contact page.",
  "Find all job titles and job URLs from this page.",
];

interface Field {
  name: string;
  type: string;
  level?: string;
  required?: boolean;
  source?: string;
}

interface Schema {
  entity: string;
  fields: Field[];
  filters?: { field: string; op: string; value: number }[];
  custom?: string[];
  parser?: string;
  ai_note?: string;
}

interface Progress {
  total?: number;
  processed?: number;
  completed?: number;
  failed?: number;
  pages?: number;
  records?: number;
  current_url?: string | null;
  stage?: string;
  ai_calls?: number;
  ai_note?: string | null;
}

const label = (name: string) => name.replace(/_/g, " ");

function FieldChips({ schema }: { schema: Schema }) {
  return (
    <div className="scraper-schema">
      <span className="muted small">{schema.entity === "job" ? "One row per job posting:" : "One row per company/page:"}</span>
      <span className="chips">
        {schema.fields.map((f) => (
          <span key={f.name} className="chip" title={`${f.type}${f.required ? ", required" : ""}${f.source === "custom" ? ", custom field (filled by AI when allowed)" : ""}`}>
            {label(f.name)}
            <span className="muted"> · {f.type}</span>
            {f.required ? " *" : ""}
          </span>
        ))}
      </span>
      {schema.filters?.map((f) => (
        <span key={f.field} className="chip">
          {label(f.field)} within {f.value} days
        </span>
      ))}
      {schema.ai_note && <span className="muted small">{schema.ai_note}</span>}
    </div>
  );
}

export function Scraper() {
  const client = useWs();
  const navigate = useNavigate();
  const [urls, setUrls] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [column, setColumn] = useState("");
  const [instruction, setInstruction] = useState(EXAMPLES[0]);
  const [useAi, setUseAi] = useState(true);
  const [schema, setSchema] = useState<Schema | null>(null);
  const action = useAction();
  const urlCount = urls.split(/\n/).filter((line) => line.trim()).length;
  const ready = instruction.trim() && (urlCount > 0 || file);

  const run = () =>
    action.run(async () => {
      const started = file
        ? await client.upload<Row>("/scraper/runs", [file], { instruction, urls, column, use_ai: String(useAi) })
        : await client.post<Row>("/scraper/runs", { instruction, urls, use_ai: useAi });
      navigate(`/scraper/${started.id}`);
    });

  return (
    <div className="page">
      <PageHeader
        title="AI Scraper"
        subtitle="Paste URLs or upload a CSV/XLSX, say what to extract, and run. Structured data and page links are read first; AI (Gemini, free tier) is used only for what they can't find. Pages that block robots, need a login or show a CAPTCHA are reported, never bypassed."
      />
      <div className="card form scraper-form">
        <label className="field field--wide">
          <span className="field__label">URLs</span>
          <textarea
            className="input textarea mono"
            rows={5}
            value={urls}
            onChange={(e) => setUrls(e.target.value)}
            placeholder={"https://example1.com\nhttps://example2.com\nhttps://example3.com"}
          />
          <span className="field__hint">{urlCount ? `${urlCount} line${urlCount === 1 ? "" : "s"}` : "One per line"}</span>
        </label>
        <div className="field-row">
          <label className="field">
            <span className="field__label">…or upload CSV / XLSX</span>
            <input type="file" accept=".csv,.xlsx,.txt" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          <label className="field">
            <span className="field__label">URL column (optional)</span>
            <input className="input" value={column} onChange={(e) => setColumn(e.target.value)} placeholder="Detected automatically" />
          </label>
        </div>
        <label className="field field--wide">
          <span className="field__label">What should I extract?</span>
          <textarea
            className="input textarea"
            rows={2}
            value={instruction}
            onChange={(e) => {
              setInstruction(e.target.value);
              setSchema(null);
            }}
            placeholder="Get company name, website and job titles…"
          />
        </label>
        <div className="chips">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="chip chip--button"
              onClick={() => {
                setInstruction(example);
                setSchema(null);
              }}
            >
              {example}
            </button>
          ))}
        </div>
        {schema && <FieldChips schema={schema} />}
        <label className="checkbox">
          <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} />
          <span>Use AI when the rules can't find a field (free tier only, $0)</span>
        </label>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions">
          <button
            type="button"
            className="button button--ghost"
            disabled={action.busy || !instruction.trim()}
            onClick={() => action.run(async () => setSchema(await client.post<Schema>("/scraper/schema", { instruction })))}
          >
            Preview fields
          </button>
          <button type="button" className="button button--primary button--large" disabled={action.busy || !ready} onClick={() => void run()}>
            {action.busy ? "Starting…" : "Run scraper"}
          </button>
        </div>
      </div>
      <h2 className="section-title">Recent runs</h2>
      <ResourceList
        load={(q, s) => client.list("/scraper/runs", q, s)}
        link={(r) => `/scraper/${r.id}`}
        columns={[
          { key: "instruction", label: "Instruction" },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "url_count", label: "URLs", render: (r) => fmt((r.stats as Row | undefined)?.url_count) },
          { key: "records", label: "Records", render: (r) => fmt((r.stats as Row | undefined)?.records ?? ((r.stats as Row | undefined)?.progress as Progress | undefined)?.records) },
          { key: "created_at", label: "Started", render: (r) => fmtDate(r.created_at) },
        ]}
      />
    </div>
  );
}

// --- one run --------------------------------------------------------------------------

type View = "companies" | "jobs" | "all";

function ProgressPanel({ run }: { run: Row }) {
  const stats = (run.stats ?? {}) as Row;
  const progress = (stats.progress ?? {}) as Progress;
  const total = progress.total ?? Number(stats.url_count ?? 0);
  const done = progress.processed ?? 0;
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const status = String(run.status);
  const active = status === "queued" || status === "running";
  return (
    <div className="card pad">
      <div className="progress">
        <div className="progress__track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
          <div className={`progress__fill progress__fill--${status}`} style={{ width: `${percent}%` }} />
        </div>
        <div className="progress__meta">
          <span>
            Stage: <strong>{progress.stage ?? (active ? "Queued" : status)}</strong>
          </span>
          <span className="tabular">
            {done} / {total} · {percent}%
          </span>
        </div>
      </div>
      <div className="stats">
        <Stat label="URLs" value={`${done} / ${total}`} />
        <Stat label="Pages" value={fmt(progress.pages ?? 0)} />
        <Stat label="Records" value={fmt(stats.records ?? progress.records ?? 0)} />
        <Stat label="Completed" value={fmt(progress.completed ?? 0)} />
        <Stat label="Failed / blocked" value={fmt(progress.failed ?? 0)} />
      </div>
      {active && progress.current_url && (
        <p className="small">
          Current: <span className="mono">{progress.current_url}</span>
        </p>
      )}
      {Boolean(progress.ai_note || stats.ai_note) && <p className="small muted">AI: {String(progress.ai_note ?? stats.ai_note)}{progress.ai_calls ? ` · ${progress.ai_calls} call(s)` : ""}</p>}
      {run.error ? <p className="small error-text">{String(run.error)}</p> : null}
    </div>
  );
}

function Cell({ value, type }: { value: unknown; type?: string }) {
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  if (Array.isArray(value)) return <span>{value.map(String).join(" | ")}</span>;
  const text = String(value);
  if ((type === "url" || /^https?:\/\//.test(text)) && /^https?:\/\//.test(text)) {
    return (
      <a className="link mono small" href={text} target="_blank" rel="noopener noreferrer nofollow">
        {text.length > 70 ? text.slice(0, 70) + "…" : text}
      </a>
    );
  }
  return <span>{fmt(value)}</span>;
}

function Results({ run }: { run: Row }) {
  const client = useWs();
  const schema = run.schema as Schema;
  const views: { key: View; label: string }[] =
    schema.entity === "job"
      ? [{ key: "companies", label: "Companies" }, { key: "jobs", label: "Jobs" }, { key: "all", label: "All fields" }]
      : [{ key: "companies", label: "Companies" }, { key: "all", label: "All fields" }];
  const [view, setView] = useState<View>(schema.entity === "job" ? "jobs" : "companies");
  const records = useLoad(
    (signal) => client.get<{ columns: string[]; items: Row[]; total: number; final: boolean }>(`/scraper/runs/${run.id}/records`, { view, limit: 2000 }, signal),
    `${client.base}${run.id}${view}${String(run.status)}${String(run.updated_at)}`,
  );
  const action = useAction();
  const types = Object.fromEntries(schema.fields.map((f) => [f.name, f.type]));
  const files = ((run.stats as Row)?.files ?? {}) as Record<string, unknown>;
  const download = (fmtName: "csv" | "xlsx" | "json") =>
    action.run(() => {
      const csvView = fmtName === "csv" && view !== "all" ? view : "all";
      const name = fmtName === "csv" ? `scrape-${run.id}-${csvView === "all" ? "results" : csvView}.csv` : `scrape-${run.id}.${fmtName}`;
      return client.download(`/scraper/runs/${run.id}/files/${fmtName}${csvView !== "all" ? `?view=${csvView}` : ""}`, name);
    });
  const data = records.data;
  const columns = (data?.columns ?? []).filter((c) => c !== "extracted_at");
  return (
    <div className="card">
      <div className="card__header scraper-results__header">
        <Tabs tabs={views.map((v) => ({ key: v.key, label: v.label, count: v.key === view ? data?.total : undefined }))} active={view} onChange={(k) => setView(k as View)} />
        <div className="scraper-downloads">
          {(["csv", "xlsx", "json"] as const).map((f) => (
            <button key={f} type="button" className="button button--ghost button--small" disabled={action.busy || !files[f]} onClick={() => void download(f)}>
              Download {f.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {records.error && <ErrorBanner error={records.error} />}
      {!data ? (
        <Loading />
      ) : (
        <DataTable
          rows={data.items.map((r, i) => ({ ...r, id: `${i}` }))}
          empty={data.final ? "No records." : "Results appear here when the run finishes."}
          columns={columns.map((c) => ({
            key: c,
            label: label(c),
            className: c === "confidence" || c === "job_count" ? "tabular" : undefined,
            render: (r: Row) =>
              c === "source_url" && Array.isArray(r.source_urls) && r.source_urls.length > 1 ? (
                <span title={(r.source_urls as string[]).join("\n")}>
                  <Cell value={r[c]} type="url" /> <span className="muted small">+{r.source_urls.length - 1}</span>
                </span>
              ) : (
                <Cell value={r[c]} type={types[c]} />
              ),
          }))}
        />
      )}
    </div>
  );
}

export function ScrapeRun() {
  const { runId = "" } = useParams();
  const client = useWs();
  const [active, setActive] = useState(true);
  const run = useLoad(
    async (signal) => {
      const loaded = await client.get<Row>(`/scraper/runs/${runId}`, undefined, signal);
      setActive(loaded.status === "queued" || loaded.status === "running");
      return loaded;
    },
    client.base + runId,
    active ? 2000 : undefined,
  );
  const pages = useLoad(
    (signal) => client.list(`/scraper/runs/${runId}/results`, { limit: 500 }, signal),
    client.base + runId + String(run.data?.status) + String(((run.data?.stats as Row | undefined)?.progress as Progress | undefined)?.processed),
  );
  const action = useAction();
  if (!run.data) return <div className="page">{run.error ? <ErrorBanner error={run.error} /> : <Loading />}</div>;
  const data = run.data;
  const stats = (data.stats ?? {}) as Row;
  const schema = data.schema as Schema;
  const status = String(data.status);
  const rejected = (stats.rejected ?? []) as { row: number; url: string; reason: string; source?: string }[];
  const failed = Number(((stats.progress ?? {}) as Progress).failed ?? 0);
  return (
    <div className="page">
      <Link to="/scraper" className="back">
        ← AI Scraper
      </Link>
      <PageHeader
        title="Scrape run"
        subtitle={String(data.instruction)}
        actions={
          <>
            <Pill value={status} />
            {(status === "queued" || status === "running") && (
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void action.run(async () => { await client.post(`/scraper/runs/${runId}/cancel`); run.refresh(); })}>
                Cancel
              </button>
            )}
            {(status === "failed" || status === "cancelled" || (status === "completed" && failed > 0)) && (
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void action.run(async () => { await client.post(`/scraper/runs/${runId}/retry`); setActive(true); run.refresh(); })}>
                Retry
              </button>
            )}
          </>
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      <FieldChips schema={schema} />
      <ProgressPanel run={data} />
      {rejected.length > 0 && (
        <details className="card pad">
          <summary>
            {rejected.length} input{rejected.length === 1 ? "" : "s"} skipped
          </summary>
          <DataTable
            rows={rejected.map((r, i) => ({ ...r, id: String(i) }))}
            columns={[
              { key: "row", label: "Row" },
              { key: "url", label: "Input", className: "mono small" },
              { key: "reason", label: "Reason" },
            ]}
          />
        </details>
      )}
      <Results run={data} />
      <h2 className="section-title">Pages</h2>
      <DataTable
        rows={(pages.data?.items ?? []) as Row[]}
        empty="No pages processed yet."
        columns={[
          { key: "row", label: "Row", render: (r) => fmt(((r.data as Row)?.input as Row | undefined)?.row) },
          { key: "url", label: "URL", render: (r) => <Cell value={r.url} type="url" /> },
          { key: "outcome", label: "Outcome", render: (r) => <Pill value={String((r.data as Row)?.outcome ?? r.status)} /> },
          { key: "records", label: "Records", render: (r) => fmt((((r.data as Row)?.records as unknown[]) ?? []).length) },
          { key: "pages", label: "Pages fetched", render: (r) => fmt((((r.data as Row)?.pages as unknown[]) ?? []).length) },
          { key: "method", label: "Method", className: "small" },
          { key: "problems", label: "Notes", render: (r) => <span className="small">{((r.problems as string[]) ?? []).slice(0, 2).join("; ") || "—"}</span> },
        ]}
      />
    </div>
  );
}
