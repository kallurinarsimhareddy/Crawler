import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { TERMINAL_STATUSES, type Job } from "../api/types";
import { ErrorBanner, Loading } from "../components/Feedback";
import { ProgressBar } from "../components/ProgressBar";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { JOB_TYPE_LABELS, formatDateTime, formatDuration } from "../lib/format";

const TARGETS_SHOWN = 50;

export function JobDetail() {
  const { jobId = "" } = useParams();
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState<Error | null>(null);

  const { data: job, error, loading, refresh } = usePolling<Job>(
    (signal) => api.getJob(jobId, signal),
    2000,
    `job:${jobId}`,
    (latest) => !latest || !TERMINAL_STATUSES.has(latest.status),
  );

  async function cancel() {
    setCancelling(true);
    setActionError(null);
    try {
      await api.cancelJob(jobId);
    } catch (err) {
      setActionError(err as Error);
    } finally {
      setCancelling(false);
      refresh();
    }
  }

  if (loading && !job) return <div className="page"><Loading label="Loading job…" /></div>;

  if (!job) {
    return (
      <div className="page">
        <Link to="/jobs" className="link back">← Jobs</Link>
        {error && <ErrorBanner error={error} onRetry={refresh} />}
      </div>
    );
  }

  const terminal = TERMINAL_STATUSES.has(job.status);

  return (
    <div className="page">
      <Link to="/jobs" className="link back">← Jobs</Link>

      <div className="page__header">
        <div className="min-w-0">
          <div className="title-row">
            <h1 className="truncate">{job.target}</h1>
            <StatusBadge status={job.status} />
          </div>
          <p className="muted mono small">{job.job_id}</p>
        </div>
        <div className="actions">
          {!terminal && (
            <button type="button" className="button button--danger" onClick={cancel} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
          <button type="button" className="button button--ghost" disabled title="Result downloads arrive in Phase 5B">
            Download results
          </button>
        </div>
      </div>

      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {actionError && <ErrorBanner error={actionError} />}

      {job.status === "failed" && (
        <div className="alert alert--error" role="alert">
          <div>
            <strong>This crawl failed.</strong>
            <pre className="error-text">{job.error ?? "No error message was recorded."}</pre>
          </div>
        </div>
      )}

      <section className="card">
        <div className="card__header">
          <h2>Progress</h2>
          {!terminal && <span className="muted small">Updating live</span>}
        </div>
        <ProgressBar progress={job.progress} status={job.status} />
        <div className="placeholder-grid">
          {["Companies crawled", "Jobs found", "New postings", "Closed postings"].map((label, index) => (
            <div key={label} className="placeholder-stat">
              <span className="stat__label">{label}</span>
              <span className="stat__value tabular">{index === 0 && job.progress.total !== null ? job.progress.completed : "—"}</span>
            </div>
          ))}
        </div>
        <p className="field__hint">Result counts are placeholders until the real crawler worker is connected.</p>
      </section>

      <div className="detail-grid">
        <section className="card">
          <h2>Details</h2>
          <dl className="details">
            <dt>Type</dt>
            <dd>{JOB_TYPE_LABELS[job.type]}</dd>
            <dt>Target</dt>
            <dd>{job.target}</dd>
            <dt>Created</dt>
            <dd className="tabular">{formatDateTime(job.created_at)}</dd>
            <dt>Started</dt>
            <dd className="tabular">{formatDateTime(job.started_at)}</dd>
            <dt>{terminal ? "Finished" : "Completed"}</dt>
            <dd className="tabular">{formatDateTime(job.completed_at)}</dd>
            <dt>Duration</dt>
            <dd className="tabular">{formatDuration(job.started_at, job.completed_at)}</dd>
          </dl>
        </section>

        <section className="card">
          <h2>
            Companies <span className="muted">({job.targets.length})</span>
          </h2>
          {job.targets.length === 0 ? (
            <p className="muted">The full scheduled roster.</p>
          ) : (
            <ul className="target-list">
              {job.targets.slice(0, TARGETS_SHOWN).map((target, index) => (
                <li key={index}>
                  {target.company_name && <span>{target.company_name}</span>}
                  {target.website && <span className="muted mono small">{target.website}</span>}
                </li>
              ))}
              {job.targets.length > TARGETS_SHOWN && <li className="muted">and {job.targets.length - TARGETS_SHOWN} more</li>}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
