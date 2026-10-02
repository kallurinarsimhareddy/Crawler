import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { TERMINAL_STATUSES, type Job, type ResultFile, type TargetRecord } from "../api/types";
import { EmptyState, ErrorBanner, Loading } from "../components/Feedback";
import { ProgressBar } from "../components/ProgressBar";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import {
  JOB_TYPE_LABELS,
  RESULT_LABELS,
  TARGET_STATUS_LABELS,
  eventLabel,
  formatBytes,
  formatDateTime,
  formatSeconds,
  formatTime,
  phaseLabel,
} from "../lib/format";

const TARGETS_SHOWN = 200;
const RESULT_ORDER = ["jobs_xlsx", "jobs_csv", "summary_json", "crawl_log"];

function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function elapsed(job: Job, now: number): number | null {
  if (!job.started_at) return null;
  const end = job.completed_at ? new Date(job.completed_at).getTime() : now;
  return (end - new Date(job.started_at).getTime()) / 1000;
}

export function JobDetail() {
  const { jobId = "" } = useParams();
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);

  const isLive = (latest: Job | null) => !latest || !TERMINAL_STATUSES.has(latest.status);
  const { data: job, error, loading, refresh } = usePolling<Job>((signal) => api.getJob(jobId, signal), 2000, `job:${jobId}`, isLive);
  const live = job ? !TERMINAL_STATUSES.has(job.status) : true;

  const targets = usePolling<TargetRecord[]>(
    async (signal) => (await api.listTargets(jobId, signal)).targets,
    3000,
    `targets:${jobId}:${job?.status ?? ""}`,
    () => live,
  );
  const events = usePolling(async (signal) => (await api.listEvents(jobId, signal)).events, 4000, `events:${jobId}:${job?.status ?? ""}`, () => live);
  const results = usePolling<ResultFile[]>(
    async (signal) => (await api.listResults(jobId, signal)).results,
    4000,
    `results:${jobId}:${job?.status ?? ""}`,
    () => live,
  );
  const now = useTicker(job?.status === "running");

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

  async function download(result: ResultFile) {
    setDownloading(result.result_id);
    setActionError(null);
    try {
      await api.downloadResult(jobId, result);
    } catch (err) {
      setActionError(err as Error);
    } finally {
      setDownloading(null);
    }
  }

  if (loading && !job) return <div className="page"><Loading label="Loading job…" /></div>;

  if (!job) {
    return (
      <div className="page">
        <Link to="/settings/crawls" className="link back">← Crawls</Link>
        {error && <ErrorBanner error={error} onRetry={refresh} />}
      </div>
    );
  }

  const terminal = TERMINAL_STATUSES.has(job.status);
  const progress = job.progress;
  const sortedResults = [...(results.data ?? [])].sort((a, b) => RESULT_ORDER.indexOf(a.kind) - RESULT_ORDER.indexOf(b.kind));
  const stats = [
    { label: "Companies", value: progress.total ?? job.targets.length ?? "—" },
    { label: "Crawled", value: progress.completed },
    { label: "Failed", value: progress.failed, tone: progress.failed > 0 ? "failed" : undefined },
    { label: "Jobs found", value: progress.jobs_found },
    { label: "Elapsed", value: formatSeconds(elapsed(job, now)) },
  ];

  return (
    <div className="page">
      <Link to="/settings/crawls" className="link back">← Crawls</Link>

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
            <button type="button" className="button button--danger" onClick={cancel} disabled={cancelling || job.cancel_requested}>
              {job.cancel_requested ? "Cancelling…" : cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {actionError && <ErrorBanner error={actionError} />}

      {!job.runnable && job.status === "queued" && (
        <p className="alert alert--info" role="status">
          {progress.message ?? "This job type is not supported by the cloud runner yet; it will not start."}
        </p>
      )}

      {job.status === "failed" && (
        <div className="alert alert--error" role="alert">
          <div>
            <strong>This crawl failed{job.attempts > 1 ? ` after ${job.attempts} attempts` : ""}.</strong>
            <pre className="error-text">{job.error ?? "No error message was recorded."}</pre>
          </div>
        </div>
      )}

      <section className="card">
        <div className="card__header">
          <h2>Progress</h2>
          {!terminal && <span className="muted small live-dot">Live</span>}
        </div>
        <ProgressBar progress={progress} status={job.status} cancelRequested={job.cancel_requested} />
        <div className="placeholder-grid placeholder-grid--five">
          {stats.map((stat) => (
            <div key={stat.label} className={`placeholder-stat${stat.tone ? ` placeholder-stat--${stat.tone}` : ""}`}>
              <span className="stat__label">{stat.label}</span>
              <span className="stat__value tabular">{stat.value}</span>
            </div>
          ))}
        </div>
        <dl className="details details--inline">
          <dt>Phase</dt>
          <dd>{job.cancel_requested && !terminal ? "Cancelling" : phaseLabel(progress.current_phase)}</dd>
          <dt>Current company</dt>
          <dd className="truncate">{progress.current_company ?? "—"}</dd>
          <dt>Started</dt>
          <dd className="tabular">{formatDateTime(job.started_at)}</dd>
          <dt>Attempt</dt>
          <dd className="tabular">{job.attempts > 0 ? `${job.attempts} of ${job.max_attempts}` : "—"}</dd>
        </dl>
      </section>

      <section className="card">
        <div className="card__header">
          <h2>Results</h2>
          {!terminal && <span className="muted small">Available when the crawl completes</span>}
        </div>
        {sortedResults.length === 0 ? (
          <p className="muted small">{terminal ? "No result files for this job." : "Nothing yet."}</p>
        ) : (
          <ul className="results">
            {sortedResults.map((result) => (
              <li key={result.result_id} className="results__item">
                <div className="min-w-0">
                  <span className="results__name">{RESULT_LABELS[result.kind]}</span>
                  <span className="muted small">
                    {result.filename} · {formatBytes(result.size_bytes)}
                    {result.row_count !== null && result.kind !== "crawl_log" ? ` · ${result.row_count} rows` : ""}
                  </span>
                </div>
                <button
                  type="button"
                  className="button button--ghost button--small"
                  onClick={() => void download(result)}
                  disabled={downloading === result.result_id}
                >
                  {downloading === result.result_id ? "Downloading…" : "Download"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <div className="card__header">
          <h2>
            Companies <span className="muted">({progress.total ?? job.targets.length})</span>
          </h2>
        </div>
        {targets.data === null ? (
          <Loading />
        ) : targets.data.length === 0 ? (
          <p className="muted">{job.type === "weekly_crawl" ? "The full scheduled roster." : "No companies."}</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col">Status</th>
                  <th scope="col">Platform</th>
                  <th scope="col">Jobs</th>
                  <th scope="col">Note</th>
                </tr>
              </thead>
              <tbody>
                {targets.data.slice(0, TARGETS_SHOWN).map((target) => (
                  <tr key={target.position}>
                    <td data-label="Company" className="table__target">
                      <span>{target.company_name ?? target.website?.replace(/^https?:\/\//, "")}</span>
                      {target.company_name && target.website && <span className="muted small block">{target.website}</span>}
                    </td>
                    <td data-label="Status">
                      <span className={`target-status target-status--${target.status}`}>{TARGET_STATUS_LABELS[target.status]}</span>
                    </td>
                    <td data-label="Platform">{target.platform ?? "—"}</td>
                    <td data-label="Jobs" className="tabular">{target.status === "pending" ? "—" : target.jobs_found}</td>
                    <td data-label="Note" className="table__note" title={target.error ?? undefined}>
                      {target.error ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {targets.data.length > TARGETS_SHOWN && <p className="muted small pad">and {targets.data.length - TARGETS_SHOWN} more — see the downloaded summary</p>}
          </div>
        )}
      </section>

      <div className="detail-grid">
        <section className="card">
          <h2>Details</h2>
          <dl className="details">
            <dt>Type</dt>
            <dd>{JOB_TYPE_LABELS[job.type]}</dd>
            <dt>Created</dt>
            <dd className="tabular">{formatDateTime(job.created_at)}</dd>
            <dt>Started</dt>
            <dd className="tabular">{formatDateTime(job.started_at)}</dd>
            <dt>Finished</dt>
            <dd className="tabular">{formatDateTime(job.completed_at)}</dd>
            <dt>Duration</dt>
            <dd className="tabular">{formatSeconds(elapsed(job, now))}</dd>
          </dl>
        </section>

        <section className="card">
          <h2>Timeline</h2>
          {events.data && events.data.length > 0 ? (
            <ol className="timeline">
              {events.data.map((event, index) => (
                <li key={index} className={`timeline__item timeline__item--${event.kind}`}>
                  <span className="timeline__time tabular">{formatTime(event.created_at)}</span>
                  <span>
                    {eventLabel(event.kind)}
                    {event.attempt ? <span className="muted small"> · attempt {event.attempt}</span> : null}
                    {event.message ? <span className="muted small block">{event.message}</span> : null}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <EmptyState title="No events yet" />
          )}
        </section>
      </div>
    </div>
  );
}
