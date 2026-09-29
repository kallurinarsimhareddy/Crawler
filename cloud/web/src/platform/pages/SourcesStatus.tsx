// Job sources and enrichment sources: access method, what each needs, whether it
// is configured and verified, the last check and recorded usage. Nothing here
// calls a provider; verification is an explicit action in Settings.

import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import "../styles/internalData.css";
import { Pill, Tags, fmt, fmtDate, useLoad } from "../ui";
import { useWs } from "../workspace";

const STATE_LABEL: Record<string, string> = {
  ok: "verified", configured_unverified: "configured, not verified", not_configured: "not configured", error: "error",
};

export function JobSourceStatus() {
  const client = useWs();
  const { data, error, loading, refresh } = useLoad(
    (signal) => client.get<{ items: Row[]; policy: string; summary: Record<string, number>; job_schema: string[] }>("/sources/status", undefined, signal),
    client.base + "sources/status",
  );
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  return (
    <section className="card pad">
      <h3>Job sources</h3>
      <p className="small muted">{data.policy}. Every source is normalised into one job schema ({data.job_schema.length} fields).</p>
      <div className="chips">
        {Object.entries(data.summary).map(([k, v]) => <span key={k} className="chip">{STATE_LABEL[k] ?? k}: {v}</span>)}
      </div>
      <div className="src-grid">
        {data.items.map((s) => {
          const usage = (s.usage ?? {}) as Row;
          const missing = (s.missing as string[] | undefined) ?? [];
          return (
            <div key={String(s.name)} className="src-card">
              <h4>
                <span>{String(s.label)}</span>
                <Pill value={s.state === "ok" ? "verified" : s.state} />
              </h4>
              <p className="muted">Access: {String(s.access_method).replace(/_/g, " ")}{s.paid ? " · paid quota" : ""}</p>
              {missing.length > 0 && <p>Needs: <Tags values={missing} /></p>}
              {s.state !== "ok" && s.requirement ? <p className="muted">{String(s.requirement)}</p> : null}
              <p className="muted">
                Last check: {fmtDate(s.last_checked_at)}
                {s.last_error ? ` · ${String(s.last_error).slice(0, 120)}` : ""}
              </p>
              {usage.searches ? <p className="muted">{fmt(usage.searches)} search(es), {fmt(usage.postings)} posting(s), {fmt(usage.failures)} failed</p> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function EnrichmentSources() {
  const client = useWs();
  const { data, error, loading, refresh } = useLoad((signal) => client.get<{ items: Row[] }>("/enrichment/sources", undefined, signal), client.base + "enrichment/sources");
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (loading && !data) return <Loading />;
  return (
    <section className="card pad">
      <h3>Enrichment sources (in priority order)</h3>
      <p className="small muted">
        Enrichment fills blank fields only, checks the company identity first, keeps provenance for every value, and uses a paid source only when it is
        connected, verified and the action is started with paid use allowed.
      </p>
      <div className="src-grid">
        {(data?.items ?? []).map((s) => {
          const credits = (s.credits ?? null) as Row | null;
          return (
            <div key={String(s.name)} className="src-card">
              <h4>
                <span>{String(s.rank)}. {String(s.label)}</span>
                <Pill value={s.status} />
              </h4>
              <p className="muted">{s.paid ? "Paid" : "Free"} · {String(s.access_method ?? "")}</p>
              {s.status === "not_configured" && s.requirement ? <p className="muted">{String(s.requirement)}</p> : null}
              {credits && credits.known ? <p className="muted">Credits remaining: {fmt(credits.remaining)}</p> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}
