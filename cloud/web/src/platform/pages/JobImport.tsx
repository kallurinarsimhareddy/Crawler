// Historical job import: upload a CSV/XLSX → confirm the column mapping for the 14
// fields → validate (nothing is stored yet) → import, with progress. Re-importing
// the same jobs never duplicates them: the Job URL is the identity.

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { IMPORT_COUNTERS, importPercent } from "../logic/jobCsv";
import { IMPORT_FIELDS, checkMapping, normalizeMapping, type ImportField } from "../logic/jobFields";
import { jobsLink } from "../logic/jobFilters";
import { DataTable, PageHeader, Pill, Stat, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import { FilledCounts, SampleTable } from "./JobMonitors";
import { uploadOne } from "./jobsShared";

type ImportRow = Row & {
  filename?: string;
  format?: string;
  headers?: string[];
  row_count?: number;
  mapping?: Record<string, string | null> | null;
  default_source?: string | null;
  status?: string;
  validation?: Record<string, unknown> | null;
  stats?: Record<string, unknown> | null;
  checkpoint?: { row?: number } | null;
  error?: string | null;
  created_at?: string;
};

const NOT_IN_FILE = "";

function n(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

export function JobImportPage() {
  const client = useWs();
  const [params, setParams] = useSearchParams();
  const importId = params.get("id");
  const [file, setFile] = useState<File | null>(null);
  const [sheet, setSheet] = useState("");
  const [reload, setReload] = useState(0);
  const upload = useAction();
  const history = useLoad((signal) => client.list<ImportRow>("/job-imports", { limit: 20 }, signal), client.base + "imports" + reload);

  const open = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next);
  };

  const start = () =>
    upload.run(async () => {
      if (!file) return;
      const created = await uploadOne<ImportRow>(client, "/job-imports", file, sheet.trim() ? { sheet: sheet.trim() } : {});
      setFile(null);
      setReload((r) => r + 1);
      open(created.id);
    });

  return (
    <div className="page">
      <Link to="/jobs" className="back">← Jobs</Link>
      <PageHeader
        title="Upload CSV"
        subtitle="Upload a UTF-8 CSV (or XLSX) of jobs, any size. Columns are detected and mapped automatically — check the mapping, validate, then import in the background. The same Job URL is never stored twice."
        crumbTitle="Upload CSV"
        actions={importId ? <button type="button" className="button button--ghost" onClick={() => open(null)}>New import</button> : undefined}
      />
      {importId ? (
        <ImportWizard importId={importId} onChanged={() => setReload((r) => r + 1)} />
      ) : (
        <div className="card pad form">
          <h3>1. Upload</h3>
          <div className="form-grid">
            <label className="field field--wide">
              <span className="field__label">CSV file (UTF-8) or XLSX</span>
              <input className="input" type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </label>
            <label className="field">
              <span className="field__label">Sheet (XLSX, optional)</span>
              <input className="input" value={sheet} placeholder="first sheet" onChange={(e) => setSheet(e.target.value)} />
            </label>
          </div>
          {upload.error && <ErrorBanner error={upload.error} />}
          <div className="form__actions">
            <button type="button" className="button button--primary" disabled={!file || upload.busy} onClick={() => void start()}>{upload.busy ? "Uploading…" : "Upload and detect columns"}</button>
          </div>
        </div>
      )}
      <div className="card">
        <div className="card__header"><h2>Previous imports</h2></div>
        {history.error && <ErrorBanner error={history.error} onRetry={history.refresh} />}
        {history.loading && !history.data ? <Loading /> : (
          <DataTable<ImportRow>
            rows={history.data?.items ?? []}
            empty={{ title: "No imports yet", description: "Historical job files you import appear here.", icon: "upload" }}
            columns={[
              { key: "filename", label: "File", render: (r) => <button type="button" className="link-button" onClick={() => open(r.id)}>{String(r.filename ?? r.id)}</button> },
              { key: "format", label: "Format", render: (r) => fmt(r.format) },
              { key: "row_count", label: "Rows", className: "tabular", render: (r) => n(r.row_count) },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "new", label: "New", className: "tabular", render: (r) => n(r.stats?.new) },
              { key: "updated", label: "Updated", className: "tabular", render: (r) => n(r.stats?.updated) },
              { key: "unchanged", label: "Unchanged", className: "tabular", render: (r) => n(r.stats?.unchanged) },
              { key: "duplicates", label: "Duplicates", className: "tabular", render: (r) => n(r.stats?.duplicates) },
              { key: "rejected", label: "Rejected", className: "tabular", render: (r) => n(r.stats?.rejected) },
              { key: "errors", label: "Errors", className: "tabular", render: (r) => n(r.stats?.errors) },
              { key: "created_at", label: "Uploaded", render: (r) => fmt(r.created_at) },
              { key: "finished", label: "Completed", render: (r) => fmt(r.stats?.finished_at) },
              { key: "view", label: "", render: (r) => (r.status === "completed" ? <Link className="link small" to={jobsLink({ import: r.id })}>View jobs</Link> : null) },
            ]}
          />
        )}
      </div>
    </div>
  );
}

function ImportWizard({ importId, onChanged }: { importId: string; onChanged: () => void }) {
  const client = useWs();
  const [polling, setPolling] = useState(false);
  const loaded = useLoad(
    async (signal) => {
      const row = await client.get<ImportRow>(`/job-imports/${encodeURIComponent(importId)}`, undefined, signal);
      setPolling(row.status === "importing");
      return row;
    },
    client.base + importId,
    polling ? 2000 : undefined,
  );
  if (loaded.error && !loaded.data) return <ErrorBanner error={loaded.error} onRetry={loaded.refresh} />;
  if (!loaded.data) return <Loading />;
  // Keyed by id so a different import starts with its own mapping.
  return <Wizard key={importId} row={loaded.data} onRow={() => { loaded.refresh(); onChanged(); }} />;
}

function Wizard({ row, onRow }: { row: ImportRow; onRow: () => void }) {
  const client = useWs();
  const headers = row.headers ?? [];
  const [mapping, setMapping] = useState<Record<ImportField, string | null>>(() => normalizeMapping(row.mapping, headers));
  const [defaultSource, setDefaultSource] = useState(row.default_source ?? "");
  const [dirty, setDirty] = useState(false);
  const action = useAction();
  const check = checkMapping(mapping, headers);
  const status = String(row.status ?? "uploaded");
  const validation = (row.validation ?? null) as Record<string, unknown> | null;
  const validated = Boolean(validation && typeof validation.rows_checked === "number") && !dirty;
  const busy = status === "importing";
  const done = status === "completed";
  const locked = busy || done;

  const validate = () =>
    action.run(async () => {
      await client.post(`/job-imports/${row.id}/validate`, { mapping, default_source: defaultSource.trim() || undefined });
      setDirty(false);
      onRow();
    });
  const begin = () =>
    action.run(async () => {
      await client.post(`/job-imports/${row.id}/start`);
      onRow();
    });

  const problems = (validation?.problems ?? {}) as Record<string, number>;
  const examples = (Array.isArray(validation?.examples) ? validation!.examples : []) as { row?: number; problem?: string }[];
  const preview = (Array.isArray(validation?.preview) ? validation!.preview : []) as Record<string, unknown>[];
  const stats = (row.stats ?? {}) as Record<string, unknown>;
  const total = Number(row.row_count ?? 0);
  const at = Number(row.checkpoint?.row ?? stats.rows ?? 0);
  const percent = importPercent(stats, total, at, status);
  const report = () => action.run(() => client.download(`/job-imports/${encodeURIComponent(row.id)}/report`, `import-report-${row.id}.csv`));

  return (
    <>
      <div className="card pad">
        <div className="title-row">
          <h3>{String(row.filename)}</h3>
          <Pill value={status} />
        </div>
        <p className="muted small">{String(row.format ?? "").toUpperCase()} · {n(row.row_count)} rows · {headers.length} columns detected</p>
        {row.error && <p className="alert alert--error">{String(row.error)}</p>}
      </div>

      <div className="card pad form">
        <h3>2. Map columns</h3>
        <p className="muted small">Columns were matched automatically by name — check and correct them. Job URL and Job Title are required; a field that is not in the file stays blank — nothing is guessed.</p>
        <div className="jm-mapping">
          {IMPORT_FIELDS.map((field) => (
            <label key={field} className="field">
              <span className="field__label">{field}{field === "Job URL" || field === "Job Title" ? <span aria-hidden="true"> *</span> : null}</span>
              <select
                className="input input--small"
                disabled={locked}
                value={mapping[field] ?? NOT_IN_FILE}
                onChange={(e) => { setMapping((m) => ({ ...m, [field]: e.target.value || null })); setDirty(true); }}
              >
                <option value={NOT_IN_FILE}>— not in file —</option>
                {headers.map((h) => <option key={h} value={h}>{h}</option>)}
              </select>
            </label>
          ))}
          <label className="field">
            <span className="field__label">Default Source (optional)</span>
            <input className="input input--small" disabled={locked} value={defaultSource} placeholder="e.g. LinkedIn export 2025" onChange={(e) => { setDefaultSource(e.target.value); setDirty(true); }} />
          </label>
        </div>
        {!check.ok && <p className="alert alert--warning">Map {check.missing.join(" and ")} to continue.</p>}
        {check.reused.length > 0 && <p className="alert alert--info">Used for more than one field: {check.reused.join(", ")}.</p>}
        {action.error && <ErrorBanner error={action.error} />}
        {!locked && (
          <div className="form__actions">
            <button type="button" className="button button--primary" disabled={!check.ok || action.busy} onClick={() => void validate()}>{action.busy ? "Validating…" : "3. Validate"}</button>
          </div>
        )}
      </div>

      {validation && typeof validation.rows_checked === "number" && (
        <div className="card pad">
          <h3>Validation{dirty ? <span className="muted small"> — out of date, validate again</span> : null}</h3>
          <div className="stats stats--wrap">
            <Stat label="Rows checked" value={n(validation.rows_checked)} hint={validation.truncated ? "first rows only" : undefined} />
            <Stat label="Valid" value={n(validation.valid)} />
            <Stat label="Rejected" value={n(validation.rejected)} />
            <Stat label="Duplicates in file" value={n(validation.duplicates_in_file)} />
            <Stat label="Already stored" value={n(validation.already_stored)} hint={typeof validation.already_stored_checked === "number" ? `of ${validation.already_stored_checked.toLocaleString()} checked` : undefined} />
          </div>
          {Object.keys(problems).length > 0 && (
            <>
              <h4 className="section-title">Problems</h4>
              <div className="chips">{Object.entries(problems).map(([k, v]) => <span key={k} className="chip chip--warn">{k.replace(/_/g, " ")}: {Number(v).toLocaleString()}</span>)}</div>
              {examples.length > 0 && (
                <ul className="small jm-examples">{examples.slice(0, 10).map((e, i) => <li key={i}>Row {e.row ?? "?"}: {String(e.problem ?? "")}</li>)}</ul>
              )}
            </>
          )}
          <h4 className="section-title">Filled per field</h4>
          <FilledCounts filled={validation.filled as Record<string, number>} of={typeof validation.valid === "number" ? validation.valid : undefined} />
          <h4 className="section-title">Preview (first {preview.length} rows)</h4>
          <SampleTable rows={preview} />
          {!locked && (
            <div className="form__actions">
              <button type="button" className="button button--primary" disabled={!validated || !check.ok || action.busy || Number(validation.valid ?? 0) === 0} onClick={() => void begin()}>4. Import</button>
            </div>
          )}
        </div>
      )}

      {(busy || done || status === "failed" || status === "cancelled") && (
        <div className="card pad">
          <h3>{busy ? `Importing ${n(total)} rows` : done ? "Import completed" : "Import stopped"}</h3>
          {total > 0 && (
            <>
              <div className="bars__track jm-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
                <span className="bars__fill" style={{ width: `${percent}%` }} />
              </div>
              <p className="muted small">Progress: {percent}% · row {n(Math.min(at, total))} of {n(total)}</p>
            </>
          )}
          <div className="stats stats--wrap">
            {IMPORT_COUNTERS.map(([key, label]) => <Stat key={key} label={label} value={n(stats[key])} />)}
            <Stat label="Linked to companies" value={n(stats.linked)} />
            <Stat label="Company review" value={n(stats.review)} />
          </div>
          {busy && <p className="muted small">Refreshing every 2 seconds. You can leave this page; the import continues in the background.</p>}
          {done && (
            <div className="actions">
              <Link className="button button--primary" to={jobsLink({ import: row.id })}>View imported jobs</Link>
              <button type="button" className="button button--ghost" onClick={() => void report()}>Download import report</button>
              {Number(stats.review ?? 0) > 0 && <Link className="button button--ghost" to="/jobs?tab=reviews">Review company names</Link>}
            </div>
          )}
        </div>
      )}
    </>
  );
}
