// Shared building blocks for the platform pages: data loading, tables, tabs,
// key/value panels, status pills and a generic filterable resource list.

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../components/Feedback";
import type { PageOf, Query, Row } from "./api";

// --- data ------------------------------------------------------------------

export interface Loaded<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

/** Load once per `key`, again on refresh(); optional polling while `pollMs` is set. */
export function useLoad<T>(load: (signal: AbortSignal) => Promise<T>, key: string, pollMs?: number): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    const run = async () => {
      try {
        const result = await loadRef.current(controller.signal);
        if (controller.signal.aborted) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (!controller.signal.aborted) setError(err as Error);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
      if (pollMs && !controller.signal.aborted) timer = window.setTimeout(run, pollMs);
    };
    setLoading(true);
    void run();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [key, tick, pollMs]);

  return { data, error, loading, refresh: useCallback(() => setTick((n) => n + 1), []) };
}

/** Run an action, surfacing its error and busy state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const run = useCallback(async <T,>(action: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await action();
    } catch (err) {
      setError(err as Error);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, clear: () => setError(null) };
}

// --- formatting ------------------------------------------------------------

export function fmt(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.map(fmt).join(", ") : "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(1);
  if (typeof value === "object") return JSON.stringify(value);
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
    const date = new Date(text);
    if (!Number.isNaN(date.getTime())) return date.toLocaleString();
  }
  return text;
}

export function fmtDate(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString();
}

const TONES: Record<string, string> = {
  // good
  VALID: "completed", completed: "completed", won: "completed", active: "running", APPROVED: "completed",
  verified: "completed", ok: "completed", succeeded: "completed", merged: "completed", FOUND: "completed",
  configured: "running", open: "running", running: "running", NEW_COMPANY_DISCOVERY: "running",
  // neutral
  queued: "queued", draft: "queued", pending_approval: "queued", planned: "queued", UNKNOWN: "queued",
  not_configured: "queued", paused: "cancelled", retrying: "cancelled", NEEDS_REVIEW: "cancelled",
  RISKY: "cancelled", NEEDS_VERIFICATION: "cancelled", UNVERIFIED: "queued", blocked: "cancelled",
  // bad
  INVALID: "failed", failed: "failed", lost: "failed", error: "failed", REJECTED: "failed", MISSING: "failed",
  DISPOSABLE: "failed", cancelled: "cancelled", DUPLICATE: "cancelled", incompatible: "failed",
};

export function Pill({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  const text = String(value);
  const tone = TONES[text] ?? "queued";
  return (
    <span className={`badge badge--${tone}`}>
      <span className="badge__dot" aria-hidden="true" />
      {text.replace(/_/g, " ")}
    </span>
  );
}

export function Score({ value }: { value: unknown }) {
  if (typeof value !== "number") return <span className="muted">—</span>;
  const tone = value >= 70 ? "hi" : value >= 40 ? "mid" : "lo";
  return (
    <span className={`score score--${tone}`} title={`${value.toFixed(1)} / 100`}>
      <span className="score__bar" style={{ width: `${Math.max(4, Math.min(100, value))}%` }} />
      <span className="score__value tabular">{Math.round(value)}</span>
    </span>
  );
}

// --- layout ----------------------------------------------------------------

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page__header">
      <div className="title-row">
        <h1>{title}</h1>
        {actions && <div className="actions">{actions}</div>}
      </div>
      {subtitle && <p className="muted">{subtitle}</p>}
    </div>
  );
}

export function Tabs({ tabs, active, onChange }: { tabs: { key: string; label: string; count?: number }[]; active: string; onChange: (key: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          aria-selected={active === tab.key}
          className={`tab${active === tab.key ? " tab--active" : ""}`}
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
          {tab.count !== undefined && <span className="tab__count">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function KeyValues({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="details">
      {items.map(([label, value]) => (
        <div key={label} style={{ display: "contents" }}>
          <dt>{label}</dt>
          <dd>{value === null || value === undefined || value === "" ? <span className="muted">—</span> : value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className="stat__value tabular">{value}</div>
      {hint && <div className="stat__hint">{hint}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Tags({ values }: { values: unknown }) {
  if (!Array.isArray(values) || values.length === 0) return <span className="muted">—</span>;
  return (
    <span className="chips">
      {values.slice(0, 8).map((v) => (
        <span key={String(v)} className="chip">
          {String(v)}
        </span>
      ))}
      {values.length > 8 && <span className="chip chip--more">+{values.length - 8}</span>}
    </span>
  );
}

// --- tables ----------------------------------------------------------------

export interface Column<T = Row> {
  key: string;
  label: string;
  render?: (row: T) => ReactNode;
  className?: string;
}

export function DataTable<T extends Row>({ rows, columns, link, empty = "Nothing here yet." }: {
  rows: T[];
  columns: Column<T>[];
  link?: (row: T) => string;
  empty?: string;
}) {
  if (rows.length === 0) return <EmptyState title={empty} />;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="table__row">
              {columns.map((c, i) => {
                const content = c.render ? c.render(row) : fmt(row[c.key]);
                return (
                  <td key={c.key} className={c.className}>
                    {i === 0 && link ? (
                      <Link className="link" to={link(row)}>
                        {content}
                      </Link>
                    ) : (
                      content
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export interface FilterDef {
  key: string;
  label: string;
  options?: string[];
  placeholder?: string;
}

/** A searchable, filterable, paged list of any platform resource. */
export function ResourceList<T extends Row>({ load, columns, link, filters = [], pageSize = 25, empty, reloadKey = "", extraQuery = {} }: {
  load: (query: Query, signal: AbortSignal) => Promise<PageOf<T>>;
  columns: Column<T>[];
  link?: (row: T) => string;
  filters?: FilterDef[];
  pageSize?: number;
  empty?: string;
  reloadKey?: string;
  extraQuery?: Query;
}) {
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const query: Query = { ...extraQuery, ...values, q: search, limit: pageSize, offset };
  const key = JSON.stringify(query) + reloadKey;
  const { data, error, loading, refresh } = useLoad((signal) => load(query, signal), key);

  useEffect(() => setOffset(0), [search, JSON.stringify(values)]);

  return (
    <div className="card">
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setSearch(q.trim());
        }}
      >
        <input className="input toolbar__search" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        {filters.map((f) =>
          f.options ? (
            <select
              key={f.key}
              className="input toolbar__filter"
              aria-label={f.label}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
            >
              <option value="">{f.label}: any</option>
              {f.options.map((o) => (
                <option key={o} value={o}>
                  {o.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          ) : (
            <input
              key={f.key}
              className="input toolbar__filter"
              placeholder={f.placeholder ?? f.label}
              aria-label={f.label}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
            />
          ),
        )}
        <button className="button button--ghost button--small" type="submit">
          Search
        </button>
      </form>
      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !data ? (
        <Loading />
      ) : data ? (
        <>
          <DataTable rows={data.items} columns={columns} link={link} empty={empty} />
          <div className="pager">
            <span className="muted small tabular">
              {data.total === 0 ? "0 results" : `${data.offset + 1}–${data.offset + data.items.length} of ${data.total.toLocaleString()}`}
            </span>
            <div className="actions">
              <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>
                Previous
              </button>
              <button className="button button--ghost button--small" disabled={!data.has_more} onClick={() => setOffset(offset + pageSize)}>
                Next
              </button>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
