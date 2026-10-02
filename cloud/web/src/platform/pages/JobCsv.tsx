// Download CSV for the Jobs page: Current results (the page shown) or All matching jobs
// (every job the active filters select). Small exports are ready immediately; large ones run
// in the background with progress, then download. The same request twice reuses the running
// export instead of starting another.

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ErrorBanner } from "../../components/Feedback";
import { currentRowCount, exportFilename, exportFinished, exportParams, exportPercent, type ExportRecord, type ExportScope } from "../logic/jobCsv";
import { buildConditions, parseConditionsParam, parseFilters, parsePage } from "../logic/jobFilters";
import { DataTable, Pill, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

function n(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

export function DownloadCsvPanel({ onClose }: { onClose: () => void }) {
  const client = useWs();
  const [params] = useSearchParams();
  const filters = useMemo(() => parseFilters(params), [params]);
  const page = useMemo(() => parsePage(params), [params]);
  const conditions = useMemo(() => {
    const tree = parseConditionsParam(params.get("conditions"));
    return tree ? buildConditions(tree) : null;
  }, [params]);
  const query = useMemo(() => exportParams(filters, conditions), [filters, conditions]);
  const [scope, setScope] = useState<ExportScope>("all");
  const [record, setRecord] = useState<ExportRecord | null>(null);
  const [reload, setReload] = useState(0);
  const action = useAction();
  const estimate = useLoad(
    (signal) => client.get<{ count: number; sync_limit: number }>("/job-exports/estimate", query as Record<string, string>, signal),
    `${client.base}|export-estimate|${JSON.stringify(query)}`,
  );
  const history = useLoad((signal) => client.get<{ items: ExportRecord[] }>("/job-exports", { limit: 5 }, signal),
    `${client.base}|exports|${reload}`);
  const total = estimate.data?.count ?? null;
  const rows = scope === "all" ? total : currentRowCount(total, page);
  const background = scope === "all" && typeof total === "number" && total > (estimate.data?.sync_limit ?? 2000);

  const download = (r: ExportRecord) => action.run(() => client.download(`/job-exports/${encodeURIComponent(r.id)}/download`, exportFilename(r)));

  // Poll a background export until it finishes, then download it once.
  useEffect(() => {
    if (!record || exportFinished(record)) return undefined;
    const timer = window.setInterval(() => {
      void client.get<ExportRecord>(`/job-exports/${encodeURIComponent(record.id)}`).then((next) => {
        setRecord(next);
        if (next.status === "completed") {
          setReload((x) => x + 1);
          void download(next);
        }
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record?.id, record?.status]);

  const start = () =>
    action.run(async () => {
      const body = { scope, params: query, page: scope === "current" ? { order: filters.order, limit: page.limit, offset: page.offset } : undefined };
      const created = await client.post<ExportRecord>("/job-exports", body);
      setRecord(created);
      setReload((x) => x + 1);
      if (created.status === "completed") await client.download(`/job-exports/${encodeURIComponent(created.id)}/download`, exportFilename(created));
    });

  const percent = exportPercent(record);
  return (
    <section className="card pad form jm-csv" aria-label="Download CSV">
      <div className="title-row">
        <h3>Download CSV</h3>
        <button type="button" className="button button--ghost button--small" onClick={onClose}>Close</button>
      </div>
      <fieldset className="jm-csv__scopes">
        <legend className="field__label">What to export</legend>
        <label className="jm-csv__scope">
          <input type="radio" name="scope" checked={scope === "current"} onChange={() => setScope("current")} />
          <span><strong>Current results</strong> — the {n(currentRowCount(total, page))} jobs on this page</span>
        </label>
        <label className="jm-csv__scope">
          <input type="radio" name="scope" checked={scope === "all"} onChange={() => setScope("all")} />
          <span><strong>All matching jobs</strong> — every job the active filters select ({n(total)}{Object.keys(query).length ? "" : ", no filters: the whole workspace"})</span>
        </label>
      </fieldset>
      <p className="muted small">
        Estimated rows: <strong>{estimate.loading && total === null ? "counting…" : n(rows)}</strong>
        {background ? " · large export: it is prepared in the background, then downloads." : ""} UTF-8 CSV; cells that start with = + - @ are made safe for spreadsheets.
      </p>
      {estimate.error && <ErrorBanner error={estimate.error} onRetry={estimate.refresh} />}
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button type="button" className="button button--primary" disabled={action.busy || rows === 0 || (record !== null && !exportFinished(record))} onClick={() => void start()}>
          {action.busy ? "Exporting…" : "Export"}
        </button>
      </div>
      {record && (
        <div className="jm-csv__status" role="status">
          {record.status === "completed" ? (
            <p className="small">Ready: {n(record.row_count)} jobs. <button type="button" className="link-button" onClick={() => void download(record)}>Download {exportFilename(record)}</button></p>
          ) : record.status === "failed" ? (
            <p className="alert alert--error small">Export failed{record.error ? `: ${record.error}` : ""}. Export again to retry.</p>
          ) : (
            <>
              <div className="bars__track jm-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
                <span className="bars__fill" style={{ width: `${percent}%` }} />
              </div>
              <p className="muted small">Exporting {n(record.progress_rows ?? 0)} of {n(record.total_rows)} jobs ({percent}%){record.deduplicated ? " — this export was already running" : ""}…</p>
            </>
          )}
        </div>
      )}
      {(history.data?.items?.length ?? 0) > 0 && (
        <>
          <h4 className="section-title">Your recent exports</h4>
          <DataTable<ExportRecord & { [k: string]: unknown }>
            rows={(history.data?.items ?? []) as (ExportRecord & { [k: string]: unknown })[]}
            columns={[
              { key: "created_at", label: "Created", render: (r) => fmt(r.created_at) },
              { key: "scope", label: "Scope", render: (r) => (r.scope === "current" ? "Current results" : "All matching") },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "row_count", label: "Rows", className: "tabular", render: (r) => n(r.row_count) },
              { key: "dl", label: "", render: (r) => (r.status === "completed" ? <button type="button" className="link-button small" onClick={() => void download(r)}>Download</button> : null) },
            ]}
          />
        </>
      )}
    </section>
  );
}
