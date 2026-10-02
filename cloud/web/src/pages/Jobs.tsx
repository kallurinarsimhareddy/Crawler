import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { JOB_STATUSES, type JobStatus } from "../api/types";
import { EmptyState, ErrorBanner, Loading } from "../components/Feedback";
import { JobsTable } from "../components/JobsTable";
import { usePolling } from "../hooks/usePolling";
import { STATUS_LABELS } from "../lib/format";

const PAGE_SIZE = 25;

export function Jobs() {
  const [params, setParams] = useSearchParams();
  const rawStatus = params.get("status");
  const status = JOB_STATUSES.includes(rawStatus as JobStatus) ? (rawStatus as JobStatus) : undefined;
  const page = Math.max(0, Number.parseInt(params.get("page") ?? "0", 10) || 0);

  const { data, error, loading, refresh } = usePolling(
    (signal) => api.listJobs({ status, limit: PAGE_SIZE, offset: page * PAGE_SIZE }, signal),
    3000,
    `jobs:${status ?? "all"}:${page}`,
  );

  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allCount = data ? Object.values(data.counts).reduce((sum, n) => sum + n, 0) : undefined;

  function update(next: { status?: JobStatus; page?: number }) {
    const search = new URLSearchParams();
    if (next.status) search.set("status", next.status);
    if (next.page) search.set("page", String(next.page));
    setParams(search);
  }

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>Crawls</h1>
          <p className="muted">Every crawl, newest first.</p>
        </div>
        <Link to="/new" className="button button--primary">
          New crawl
        </Link>
      </div>

      <div className="tabs" role="tablist" aria-label="Filter by status">
        <button type="button" role="tab" aria-selected={!status} className={`tab${!status ? " tab--active" : ""}`} onClick={() => update({})}>
          All {allCount !== undefined && <span className="tab__count">{allCount}</span>}
        </button>
        {JOB_STATUSES.map((option) => (
          <button
            key={option}
            type="button"
            role="tab"
            aria-selected={status === option}
            className={`tab${status === option ? " tab--active" : ""}`}
            onClick={() => update({ status: option })}
          >
            {STATUS_LABELS[option]} {data && <span className="tab__count">{data.counts[option]}</span>}
          </button>
        ))}
      </div>

      {error && <ErrorBanner error={error} onRetry={refresh} />}

      <section className="card">
        {loading && !data ? (
          <Loading />
        ) : data && data.jobs.length > 0 ? (
          <>
            <JobsTable jobs={data.jobs} />
            {pages > 1 && (
              <div className="pager">
                <button type="button" className="button button--ghost button--small" disabled={page === 0} onClick={() => update({ status, page: page - 1 })}>
                  Previous
                </button>
                <span className="muted tabular">
                  Page {page + 1} of {pages}
                </span>
                <button type="button" className="button button--ghost button--small" disabled={page + 1 >= pages} onClick={() => update({ status, page: page + 1 })}>
                  Next
                </button>
              </div>
            )}
          </>
        ) : (
          !error && <EmptyState title={status ? `No ${STATUS_LABELS[status].toLowerCase()} jobs` : "No jobs yet"} />
        )}
      </section>
    </div>
  );
}
