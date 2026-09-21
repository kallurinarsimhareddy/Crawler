import { Link } from "react-router-dom";
import { api } from "../api/client";
import { EmptyState, ErrorBanner, Loading } from "../components/Feedback";
import { JobsTable } from "../components/JobsTable";
import { usePolling } from "../hooks/usePolling";
import { WorkerBanner, useWorkerStatus } from "../components/WorkerStatus";

const RECENT = 8;

export function Dashboard() {
  const { data, error, loading, refresh } = usePolling((signal) => api.listJobs({ limit: RECENT }, signal), 3000, "dashboard");
  const { data: status, error: statusError } = useWorkerStatus();

  const counts = data?.counts;
  const total = counts ? Object.values(counts).reduce((sum, n) => sum + n, 0) : null;
  const stats = [
    { label: "Total jobs", value: total, tone: "neutral" },
    { label: "Running", value: counts ? counts.running + counts.queued : null, hint: counts ? `${counts.queued} queued` : undefined, tone: "running" },
    { label: "Completed", value: counts?.completed ?? null, tone: "completed" },
    { label: "Failed", value: counts?.failed ?? null, tone: "failed" },
    {
      label: "Queue",
      value: status ? status.queue.ready + status.queue.delayed : null,
      hint: status ? (status.worker.online ? "worker online" : "worker offline") : undefined,
      tone: status && !status.worker.online && status.queue.ready + status.queue.delayed > 0 ? "failed" : "neutral",
    },
  ];

  return (
    <div className="page">
      <div className="page__header">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">Crawl activity across all jobs.</p>
        </div>
        <Link to="/new" className="button button--primary">
          New crawl
        </Link>
      </div>

      {error && <ErrorBanner error={error} onRetry={refresh} />}

      <WorkerBanner status={status} error={statusError} />

      <section className="stats" aria-label="Job totals">
        {stats.map((stat) => (
          <div key={stat.label} className={`stat stat--${stat.tone}`}>
            <span className="stat__label">{stat.label}</span>
            <span className="stat__value tabular">{stat.value ?? "—"}</span>
            {stat.hint && <span className="stat__hint">{stat.hint}</span>}
          </div>
        ))}
      </section>

      <section className="card">
        <div className="card__header">
          <h2>Recent crawls</h2>
          <Link to="/jobs" className="link">
            View all
          </Link>
        </div>
        {loading && !data ? (
          <Loading />
        ) : data && data.jobs.length > 0 ? (
          <JobsTable jobs={data.jobs} compact />
        ) : (
          !error && (
            <EmptyState title="No crawls yet">
              <Link to="/new" className="button button--primary">
                Run your first crawl
              </Link>
            </EmptyState>
          )
        )}
      </section>
    </div>
  );
}
