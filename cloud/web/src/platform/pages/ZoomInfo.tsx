// ZoomInfo company search: credit-free, through the workspace's own API connection. Results are not saved
// here; "Send to Discover" queues them for the usual review (dedupe, verify, approve) before any CRM write.

import { useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner } from "../../components/Feedback";
import type { Row } from "../api";
import { DataTable, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

type SearchResult = { items: Row[]; meta?: { total_results?: number | null; total_pages?: number | null }; credits_used?: number };

export function ZoomInfoSearch() {
  const client = useWs();
  const providers = useLoad((signal) => client.get<{ items: Row[] }>("/providers", undefined, signal).catch(() => ({ items: [] as Row[] })), client.base + "providers");
  const zoominfo = (providers.data?.items ?? []).find((p) => p.provider === "zoominfo");
  const [filters, setFilters] = useState({ companyName: "", state: "", country: "" });
  const [result, setResult] = useState<SearchResult | null>(null);
  const [queued, setQueued] = useState<string | null>(null);
  const action = useAction();
  const active = Object.fromEntries(Object.entries(filters).filter(([, v]) => v.trim())) as Record<string, string>;

  if (providers.data && !zoominfo?.configured) {
    return (
      <EmptyState title="ZoomInfo is not connected" description="Add the workspace's ZoomInfo API client ID and secret in Settings, then test the connection.">
        <Link className="button button--primary" to="/settings">Open Settings</Link>
      </EmptyState>
    );
  }
  return (
    <div className="card pad">
      <h3>ZoomInfo company search</h3>
      <p className="muted small">Credit-free search through your ZoomInfo API connection{zoominfo?.verified ? "" : " (not verified yet: use Test connection in Settings)"}. Only enrichment uses credits.</p>
      {action.error && <ErrorBanner error={action.error} />}
      <form
        className="field-row"
        onSubmit={(e) => {
          e.preventDefault();
          setQueued(null);
          void action.run(async () => setResult(await client.post<SearchResult>("/providers/zoominfo/company-search", { filters: active, limit: 25 })));
        }}
      >
        <label className="field"><span className="field__label">Company name</span><input className="input" value={filters.companyName} onChange={(e) => setFilters({ ...filters, companyName: e.target.value })} /></label>
        <label className="field"><span className="field__label">State</span><input className="input" value={filters.state} onChange={(e) => setFilters({ ...filters, state: e.target.value })} /></label>
        <label className="field"><span className="field__label">Country</span><input className="input" value={filters.country} onChange={(e) => setFilters({ ...filters, country: e.target.value })} /></label>
        <div className="form__actions">
          <button className="button button--primary" type="submit" disabled={action.busy || !Object.keys(active).length}>Search ZoomInfo</button>
        </div>
      </form>
      {result && (
        <>
          <p className="muted small">
            {fmt(result.items.length)} shown{result.meta?.total_results != null ? ` of ${fmt(result.meta.total_results)} matching` : ""} · {fmt(result.credits_used ?? 0)} credits used
          </p>
          <DataTable
            rows={result.items.map((r, i) => ({ ...r, id: String(r.zoominfo_id || i) }) as Row)}
            empty="No ZoomInfo companies match."
            columns={[
              { key: "name", label: "Company" },
              { key: "website", label: "Website", className: "mono small" },
              { key: "city", label: "Location", render: (r) => [r.city, r.state, r.country].filter(Boolean).join(", ") || "—" },
              { key: "employee_count", label: "Employees", className: "tabular", render: (r) => fmt(r.employee_count) },
              { key: "revenue", label: "Revenue", className: "tabular", render: (r) => fmt(r.revenue) },
            ]}
          />
          {result.items.length > 0 && (
            <div className="form__actions">
              <button
                className="button"
                disabled={action.busy}
                onClick={() => void action.run(async () => {
                  await client.post("/discovery/run", { source: "zoominfo", filters: active, limit: result.items.length });
                  setQueued(`${result.items.length} companies queued for review in Discover`);
                })}
              >
                Send to Discover for review
              </button>
              {queued && <span className="muted small">{queued} · <Link to="/prospecting?tab=discover">open Discover</Link></span>}
            </div>
          )}
        </>
      )}
    </div>
  );
}
