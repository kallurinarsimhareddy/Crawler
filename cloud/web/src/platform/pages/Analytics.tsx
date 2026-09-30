// Analytics: the workspace overview plus date-ranged, filterable reports (funnel,
// attribution, sequences, hiring trends, team, data quality, usage), each with a
// chart, a table, CSV/XLSX export and saved views. Report state lives in the URL
// (?tab=&report=&start=&end=&f_<filter>=) so every view can be linked.

import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { formatCell, linePath, presetRange, PRESETS, reportQuery } from "../logic/reports";
import { SectionPage } from "../Section";
import { DataTable, KeyValues, fmt, fmtDate, useAction, useLoad, type Column } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import { Analytics as WorkspaceMetrics, Dashboard } from "./Work";
import "../styles/analytics.css";

interface ReportInfo {
  key: string;
  title: string;
  description: string;
  family: string;
  filters: string[];
  chart: string;
}

interface Series {
  name: string;
  points: { x: string; y: number }[];
}

interface ReportResult {
  report: string;
  title: string;
  description: string;
  chart: string;
  range: { start: string; end: string };
  filters: Record<string, string>;
  columns: { key: string; label: string }[];
  rows: Row[];
  series: Series[];
  totals: Record<string, unknown>;
  notes: string[];
  empty: boolean;
  truncated: boolean;
  generated_at: string;
}

const FAMILIES: { key: string; label: string; empty: string }[] = [
  { key: "pipeline", label: "Pipeline", empty: "Add companies and deals to see the funnel, conversion and attribution." },
  { key: "campaigns", label: "Campaigns", empty: "Create campaigns, enroll contacts and send (where allowed) to see performance here." },
  { key: "intelligence", label: "Hiring", empty: "Crawl careers pages or run signal detection to see hiring trends." },
  { key: "team", label: "Team", empty: "Assign owners and log activities to see team performance." },
  { key: "data", label: "Data quality", empty: "Import data or validate emails to see source attribution and validation quality." },
  { key: "usage", label: "Usage", empty: "Run the AI Scraper or AI features to see usage here." },
];

const FILTER_LABELS: Record<string, string> = {
  campaign_id: "Campaign id",
  sequence_id: "Sequence id",
  list_id: "List id",
  signal_type: "Signal type",
  kind: "Activity kind",
  provider: "Provider",
  purpose: "Purpose",
  dimension: "Group by",
};

const FILTER_OPTIONS: Record<string, string[]> = {
  dimension: ["source", "campaign_id", "signal_types", "owner_id"],
  signal_type: ["NEW_ROLE", "MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE", "HIRING_VELOCITY", "LONG_OPEN_ROLE", "HARD_TO_FILL", "SPECIALIZED_TECHNOLOGY", "PROJECT_IMPLEMENTATION", "EXPANSION_HIRING", "BACKFILL_REPLACEMENT", "LEADERSHIP_HIRING"],
};

function useCatalog() {
  const client = useWs();
  return useLoad((s) => client.get<{ items: ReportInfo[] }>("/analytics/reports", undefined, s), client.base + "report-catalog");
}

function viewHref(info: ReportInfo, start?: string, end?: string, filters: Record<string, unknown> = {}): string {
  const params = new URLSearchParams({ tab: info.family, report: info.key });
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  for (const [k, v] of Object.entries(filters)) if (v) params.set(`f_${k}`, String(v));
  return `/analytics?${params.toString()}`;
}

// --- charts -------------------------------------------------------------------------------

function LineChart({ series }: { series: Series[] }) {
  const w = 640;
  const h = 160;
  const max = Math.max(1, ...series.flatMap((s) => s.points.map((p) => p.y)));
  const first = series[0]?.points[0]?.x;
  const last = series[0]?.points[series[0].points.length - 1]?.x;
  return (
    <figure className="rchart">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="rchart__svg" role="img" aria-label={series.map((s) => `${s.name}: ${s.points.reduce((a, p) => a + p.y, 0)} total`).join("; ")}>
        {series.map((s, i) => (
          <path key={s.name} d={linePath(s.points.map((p) => p.y), w, h, max)} className={`rchart__line rchart__line--${i % 4}`} fill="none" vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
      <figcaption className="rchart__axis small muted">
        <span>{first}</span>
        <span className="rchart__legend">
          {series.map((s, i) => (
            <span key={s.name}><i className={`rchart__key rchart__key--${i % 4}`} aria-hidden="true" />{s.name} ({s.points.reduce((a, p) => a + p.y, 0).toLocaleString()})</span>
          ))}
          <span>peak {max.toLocaleString()}/day</span>
        </span>
        <span>{last}</span>
      </figcaption>
    </figure>
  );
}

/** Horizontal bars in the report's own order (a funnel must not be re-sorted). */
function OrderedBars({ series }: { series: Series }) {
  const points = series.points.slice(0, 20);
  const max = Math.max(1, ...points.map((p) => p.y));
  if (!points.some((p) => p.y > 0)) return null;
  return (
    <ul className="bars rchart__bars" aria-label={series.name}>
      {points.map((p) => (
        <li key={p.x} className="bars__row">
          <span className="bars__label">{p.x}</span>
          <span className="bars__track"><span className="bars__fill" style={{ width: `${(p.y / max) * 100}%` }} /></span>
          <span className="bars__value tabular">{p.y.toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}

function ReportChart({ result }: { result: ReportResult }) {
  if (!result.series.length) return null;
  if (result.chart === "line") return <LineChart series={result.series} />;
  return (
    <div className={result.series.length > 1 ? "grid-2" : undefined}>
      {result.series.map((s) => (
        <div key={s.name}>
          {result.series.length > 1 && <h4 className="rchart__title">{s.name}</h4>}
          <OrderedBars series={s} />
        </div>
      ))}
    </div>
  );
}

function Totals({ totals }: { totals: Record<string, unknown> }) {
  const simple = Object.entries(totals).filter(([, v]) => v === null || typeof v !== "object");
  const nested = Object.entries(totals).filter(([, v]) => v !== null && typeof v === "object" && !Array.isArray(v));
  if (!simple.length && !nested.length) return null;
  const items: [string, string][] = [
    ...simple.map(([k, v]) => [k.replace(/_/g, " "), formatCell(k, v)] as [string, string]),
    ...nested.map(([k, v]) => [k.replace(/_/g, " "), Object.entries(v as Record<string, unknown>).map(([a, b]) => `${a.replace(/_/g, " ")}: ${fmt(b)}`).join(" · ") || "—"] as [string, string]),
  ];
  return <div className="rtotals"><KeyValues items={items} /></div>;
}

// --- one report ------------------------------------------------------------------------

function ReportView({ info, familyEmpty }: { info: ReportInfo; familyEmpty: string }) {
  const client = useWs();
  const { current } = useWorkspace();
  const canWrite = current ? current.role !== "viewer" : false;
  const [params, setParams] = useSearchParams();
  const defaults = presetRange("30d");
  const start = params.get("start") ?? defaults.start;
  const end = params.get("end") ?? defaults.end;
  const applied: Record<string, string> = {};
  for (const f of info.filters) applied[f] = params.get(`f_${f}`) ?? "";
  const [draft, setDraft] = useState<Record<string, string>>(applied);
  const [saving, setSaving] = useState(false);
  const [viewName, setViewName] = useState("");
  const [saved, setSaved] = useState(false);
  const action = useAction();
  const query = reportQuery(start, end, applied);
  const key = JSON.stringify([info.key, query]);
  const { data, error, loading, refresh } = useLoad((s) => client.get<ReportResult>(`/analytics/reports/${info.key}`, query, s), client.base + key);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    setParams(next, { replace: true });
  };
  const applyFilters = (e: FormEvent) => {
    e.preventDefault();
    update(Object.fromEntries(info.filters.map((f) => [`f_${f}`, draft[f]?.trim() || null])));
  };
  const exportAs = (format: "csv" | "xlsx") =>
    action.run(() => client.download(`/analytics/reports/${info.key}/export?${new URLSearchParams({ ...query, format }).toString()}`, `${info.key}_${start}_${end}.${format}`));
  const saveView = (e: FormEvent) => {
    e.preventDefault();
    void action.run(async () => {
      await client.post("/analytics/saved-reports", { name: viewName.trim(), report: info.key, filters: reportQuery("", "", applied), date_range: { start, end } });
      setSaving(false);
      setViewName("");
      setSaved(true);
    });
  };

  const columns: Column[] = (data?.columns ?? []).map((c) => ({
    key: c.key,
    label: c.label,
    render: (r: Row) => formatCell(c.key, r[c.key]),
    className: typeof data?.rows[0]?.[c.key] === "number" ? "tabular" : undefined,
  }));

  return (
    <div className="card report">
      <div className="report__head">
        <div className="min-w-0">
          <h2 className="report__title">{info.title}</h2>
          <p className="muted small">{info.description}</p>
        </div>
        <div className="actions">
          <button type="button" className="button button--ghost button--small" disabled={action.busy || !data} onClick={() => exportAs("csv")}>Export CSV</button>
          <button type="button" className="button button--ghost button--small" disabled={action.busy || !data} onClick={() => exportAs("xlsx")}>Export XLSX</button>
          {canWrite && <button type="button" className="button button--ghost button--small" onClick={() => { setSaving((v) => !v); setSaved(false); }}>Save view</button>}
        </div>
      </div>
      <form className="report__controls" onSubmit={applyFilters}>
        <label className="filterbar__field">
          <span className="field__label">Range</span>
          <select className="input input--small" value="" onChange={(e) => { if (!e.target.value) return; const r = presetRange(e.target.value); update({ start: r.start, end: r.end }); }}>
            <option value="">Preset…</option>
            {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
          </select>
        </label>
        <label className="filterbar__field">
          <span className="field__label">From</span>
          <input className="input input--small" type="date" value={start} max={end} onChange={(e) => update({ start: e.target.value })} />
        </label>
        <label className="filterbar__field">
          <span className="field__label">To</span>
          <input className="input input--small" type="date" value={end} min={start} onChange={(e) => update({ end: e.target.value })} />
        </label>
        {info.filters.map((f) => (
          <label key={f} className="filterbar__field">
            <span className="field__label">{FILTER_LABELS[f] ?? f}</span>
            {FILTER_OPTIONS[f] ? (
              <select className="input input--small" value={draft[f] ?? ""} onChange={(e) => setDraft({ ...draft, [f]: e.target.value })}>
                <option value="">{f === "dimension" ? "source" : "Any"}</option>
                {FILTER_OPTIONS[f].filter((o) => !(f === "dimension" && o === "source")).map((o) => <option key={o} value={o}>{o.replace(/_/g, " ").toLowerCase()}</option>)}
              </select>
            ) : (
              <input className="input input--small" value={draft[f] ?? ""} placeholder={FILTER_LABELS[f] ?? f} onChange={(e) => setDraft({ ...draft, [f]: e.target.value })} />
            )}
          </label>
        ))}
        {info.filters.length > 0 && <button className="button button--ghost button--small report__apply" type="submit">Apply</button>}
      </form>
      {saving && (
        <form className="report__save" onSubmit={saveView}>
          <input className="input input--small" value={viewName} onChange={(e) => setViewName(e.target.value)} placeholder="Name this view" required aria-label="View name" />
          <button className="button button--primary button--small" disabled={action.busy || !viewName.trim()}>Save</button>
        </form>
      )}
      {saved && <div className="alert alert--info">View saved. Find it under “Saved views”.</div>}
      {action.error && <ErrorBanner error={action.error} />}
      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !data ? (
        <Loading label="Running report…" />
      ) : data ? (
        data.empty ? (
          <EmptyState icon="chart" title={`No data for ${fmtDate(data.range.start)} – ${fmtDate(data.range.end)}`} description={familyEmpty} />
        ) : (
          <div className="report__body">
            {data.truncated && <div className="alert alert--warning">This range has more rows than one report scans; narrow the dates for exact numbers.</div>}
            <ReportChart result={data} />
            <Totals totals={data.totals} />
            <DataTable rows={data.rows} columns={columns} empty={{ title: "No rows", description: "Nothing matched in this range." }} />
            {data.notes.length > 0 && (
              <ul className="report__notes small muted">
                {data.notes.map((n) => <li key={n}>{n}</li>)}
              </ul>
            )}
          </div>
        )
      ) : null}
    </div>
  );
}

function ReportFamily({ family }: { family: (typeof FAMILIES)[number] }) {
  const catalog = useCatalog();
  const [params, setParams] = useSearchParams();
  if (catalog.error) return <ErrorBanner error={catalog.error} onRetry={catalog.refresh} />;
  if (!catalog.data) return <Loading />;
  const reports = catalog.data.items.filter((r) => r.family === family.key);
  if (!reports.length) return <EmptyState icon="chart" title="No reports here yet" />;
  const current = reports.find((r) => r.key === params.get("report")) ?? reports[0];
  const pick = (key: string) => {
    const next = new URLSearchParams(params);
    next.set("report", key);
    for (const k of Array.from(next.keys())) if (k.startsWith("f_")) next.delete(k);
    setParams(next, { replace: true });
  };
  return (
    <div className="reports">
      {reports.length > 1 && (
        <div className="report__picker" role="tablist" aria-label="Reports">
          {reports.map((r) => (
            <button key={r.key} type="button" role="tab" aria-selected={r.key === current.key} className={`report__pick${r.key === current.key ? " report__pick--active" : ""}`} onClick={() => pick(r.key)}>
              {r.title}
            </button>
          ))}
        </div>
      )}
      <ReportView key={current.key} info={current} familyEmpty={family.empty} />
    </div>
  );
}

function SavedViews() {
  const client = useWs();
  const catalog = useCatalog();
  const { current } = useWorkspace();
  const canWrite = current ? current.role !== "viewer" : false;
  const [reload, setReload] = useState(0);
  const action = useAction();
  const views = useLoad((s) => client.list("/analytics/saved-reports", { limit: 100, order: "name" }, s), client.base + "saved-reports" + reload);
  if (views.error) return <ErrorBanner error={views.error} onRetry={views.refresh} />;
  if (catalog.error) return <ErrorBanner error={catalog.error} onRetry={catalog.refresh} />;
  if (!views.data || !catalog.data) return <Loading />;
  const byKey = Object.fromEntries(catalog.data.items.map((r) => [r.key, r]));
  const range = (r: Row) => (r.date_range ?? {}) as Record<string, string>;
  return (
    <div className="card">
      {action.error && <ErrorBanner error={action.error} />}
      <DataTable
        rows={views.data.items}
        empty={{ title: "No saved views", description: "Open any report, set the dates and filters, then choose “Save view” to keep it here for the team.", icon: "chart" }}
        columns={[
          { key: "name", label: "View", render: (r) => (byKey[String(r.report)] ? <Link className="link" to={viewHref(byKey[String(r.report)], range(r).start, range(r).end, r.filters as Record<string, unknown>)}>{String(r.name)}</Link> : String(r.name)) },
          { key: "report", label: "Report", render: (r) => byKey[String(r.report)]?.title ?? String(r.report) },
          { key: "date_range", label: "Range", render: (r) => (range(r).start ? `${range(r).start} → ${range(r).end ?? "today"}` : "Last 30 days") },
          { key: "filters", label: "Filters", render: (r) => Object.entries((r.filters ?? {}) as Record<string, string>).filter(([k]) => k !== "start" && k !== "end").map(([k, v]) => `${k}=${v}`).join(", ") || "—" },
          { key: "created_at", label: "Saved", render: (r) => fmtDate(r.created_at) },
          {
            key: "actions",
            label: "",
            render: (r) => canWrite ? (
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => { await client.del(`/analytics/saved-reports/${r.id}`); setReload((n) => n + 1); })}>
                Delete
              </button>
            ) : null,
          },
        ]}
      />
    </div>
  );
}

export function Analytics() {
  return (
    <SectionPage
      title="Analytics"
      subtitle="Funnel, attribution, sequences, hiring trends, team, data quality and usage — with date ranges, filters, export and saved views."
      tabs={[
        { key: "overview", label: "Overview", render: () => <Dashboard /> },
        ...FAMILIES.map((family) => ({ key: family.key, label: family.label, render: () => <ReportFamily family={family} /> })),
        { key: "saved", label: "Saved views", render: () => <SavedViews /> },
        { key: "metrics", label: "Workspace metrics", render: () => <WorkspaceMetrics /> },
      ]}
    />
  );
}
