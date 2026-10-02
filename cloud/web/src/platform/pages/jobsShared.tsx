// Shared pieces of the Job source monitor feature: types, the API calls that do not
// fit the generic client (flat PATCH, single-file upload), badges, the original-job
// link, a compact job table and the SANA chat update card.

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { request } from "../../api/client";
import type { Row, WsClient } from "../api";
import { changeTone, display, joinKeywords, safeJobUrl, statusLabel, statusTone, urlLabel } from "../logic/jobFields";
import { relevanceClass, relevanceScoreText, relevanceTone, type JobSpyBoard, type JobSpyDefaults } from "../logic/jobspy";
import { DataTable, fmtDate, type Column, type Empty } from "../ui";
import "../styles/jobs.css";

export type Job = Row & {
  job_url?: string | null;
  title?: string | null;
  company_name?: string | null;
  company_id?: string | null;
  status?: string | null;
  status_label?: string | null;
  change_badge?: string | null;
  keywords?: string[];
  fields?: Record<string, unknown>;
  source?: string | null;
  source_board?: string | null;
  search_term?: string | null;
  matched_keywords?: string[] | null;
  matched_categories?: string[] | null;
  relevance_score?: number | null;
  relevance_class?: string | null;
  relevance_reason?: string | null;
  posted_at?: string | null;
  description?: string | null;
  listing_date?: string | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  last_changed_at?: string | null;
  stale_at?: string | null;
  expired_at?: string | null;
  closed_at?: string | null;
  reopened_at?: string | null;
  closure_reason?: string | null;
  missed_full_sweeps?: number | null;
  gone_checked_at?: string | null;
  gone_status?: number | null;
};

export interface JobPage {
  rows: Job[];
  total: number;
  limit: number;
  offset: number;
}

export type Monitor = Row & {
  name?: string;
  source_url?: string;
  source_name?: string | null;
  schedule?: string;
  enabled?: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_status?: string | null;
  last_result?: Record<string, unknown> | null;
  max_pages_incremental?: number | null;
  strategy?: string | null;
  filters?: Record<string, unknown> | null;
  stale_after_days?: number | null;
  visible_window_days?: number | null;
  gone_checks_per_run?: number | null;
  close_after_missed?: number | null;
  next_lifecycle_at?: string | null;
  last_lifecycle_at?: string | null;
  last_lifecycle_result?: LifecycleResult | null;
};

/** The daily lifecycle evaluation summary stored on a monitor. */
export interface LifecycleResult {
  evaluated_at?: string;
  stale_after_days?: number;
  became_stale?: number;
  last_24h?: Record<string, number>;
  by_status?: Record<string, number>;
  closure_candidates?: number;
  signals?: Record<string, unknown>;
}

export type MonitorRun = Row & {
  monitor_id?: string;
  mode?: string;
  trigger?: string;
  status?: string;
  started_at?: string | null;
  finished_at?: string | null;
  pages?: number | null;
  found?: number | null;
  new_count?: number | null;
  changed_count?: number | null;
  unchanged_count?: number | null;
  reopened_count?: number | null;
  closed_count?: number | null;
  expired_count?: number | null;
  gone_checked_count?: number | null;
  gone_closed_count?: number | null;
  error_count?: number | null;
  stop_reason?: string | null;
};

export const ACTIVE_RUN = ["queued", "running"];

/** PATCH with a flat body (the job monitor API does not use the {changes} envelope). */
export function patchFlat<T = Row>(client: WsClient, path: string, body: Record<string, unknown>): Promise<T> {
  return request<T>(`${client.base}${path}`, { method: "PATCH", body: JSON.stringify(body) });
}

/** Multipart upload of one file under the field name "file". */
export function uploadOne<T = Row>(client: WsClient, path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
  const form = new FormData();
  form.append("file", file, file.name);
  for (const [key, value] of Object.entries(fields)) if (value) form.append(key, value);
  return request<T>(`${client.base}${path}`, { method: "POST", body: form });
}

/** A blank-safe cell: the value, or a muted dash when the source did not show it. */
export function Cell({ value }: { value: unknown }) {
  const text = display(value);
  return text ? <>{text}</> : <span className="muted" aria-label="not shown">—</span>;
}

export function Badge({ text, tone }: { text: string; tone: string }) {
  if (!text) return <span className="muted">—</span>;
  return (
    <span className={`badge badge--${tone}`}>
      <span className="badge__dot" aria-hidden="true" />
      {text}
    </span>
  );
}

/** Relevance: the 0–100 score and a HIGH (green) / REVIEW (amber) / REJECT (gray) badge. */
export function Relevance({ score, cls }: { score: unknown; cls: unknown }) {
  const text = relevanceScoreText(score);
  const klass = relevanceClass(cls);
  const tone = relevanceTone(cls);
  if (!text && !klass) return <span className="muted" aria-label="not scored">—</span>;
  return (
    <span className="jm-relevance">
      {text && <span className="jm-relevance__score tabular" title={`Relevance ${text} / 100`}>{text}</span>}
      {klass && tone && <Badge text={klass} tone={tone} />}
    </span>
  );
}

/** A list of chips, or a dash when empty. */
export function Chips({ values }: { values: unknown }) {
  const list = Array.isArray(values) ? values.map(display).filter(Boolean) : [];
  if (list.length === 0) return <span className="muted">—</span>;
  return <span className="chips">{list.map((v, i) => <span key={`${v}-${i}`} className="chip">{v}</span>)}</span>;
}

export interface JobSources {
  site_profiles?: { name: string; source_name: string; hosts: string[] }[];
  jobspy?: { boards?: JobSpyBoard[]; defaults?: Partial<JobSpyDefaults> };
}

export function JobStatus({ job }: { job: Row }) {
  const label = statusLabel(job);
  return <Badge text={label} tone={statusTone(label)} />;
}

export function ChangeBadge({ value }: { value: unknown }) {
  const text = display(value);
  return text ? <Badge text={text} tone={changeTone(text)} /> : <span className="muted">—</span>;
}

/** The original job posting, opened in a new tab. Never a rebuilt URL: job_url as stored, http(s) only. */
export function JobUrl({ url, children, className = "link", stop = true }: { url: unknown; children?: ReactNode; className?: string; stop?: boolean }) {
  const href = safeJobUrl(url);
  if (!href) return display(url) ? <span className="muted small" title="Not an http(s) link">{display(url)}</span> : <span className="muted">—</span>;
  return (
    <a className={className} href={href} target="_blank" rel="noopener noreferrer" title={href} onClick={stop ? (e) => e.stopPropagation() : undefined}>
      {children ?? urlLabel(href)}
    </a>
  );
}

/** A compact table of jobs (title links to the job page). */
export function MiniJobs({ rows, empty, extra = [] }: { rows: Job[]; empty: Empty; extra?: Column<Job>[] }) {
  return (
    <DataTable<Job>
      rows={rows}
      empty={empty}
      columns={[
        { key: "title", label: "Job Title", render: (r) => <Link className="link" to={`/jobs/${r.id}`}><Cell value={r.title} /></Link> },
        { key: "location", label: "Location", render: (r) => <Cell value={r.location} /> },
        { key: "remote", label: "Remote", render: (r) => <Cell value={r.remote} /> },
        { key: "keywords", label: "Keywords", render: (r) => <Cell value={joinKeywords(r)} /> },
        ...extra,
        { key: "first_seen_at", label: "First Seen", render: (r) => (r.first_seen_at ? fmtDate(r.first_seen_at) : <span className="muted">—</span>) },
        { key: "status", label: "Status", render: (r) => <JobStatus job={r} /> },
        { key: "job_url", label: "Job URL", render: (r) => <JobUrl url={r.job_url} /> },
      ]}
    />
  );
}

// --- SANA chat ---------------------------------------------------------------------------------

interface UpdateJob {
  id?: string;
  title?: string | null;
  company?: string | null;
  location?: string | null;
  remote?: string | null;
  keywords?: string[] | null;
  job_url?: string | null;
}

interface UpdateData {
  kind: "job_monitor_update";
  monitor_id?: string;
  run_id?: string;
  source?: string | null;
  status?: string;
  counts?: Record<string, number | null | undefined>;
  jobs?: UpdateJob[];
  links?: { new?: string; changed?: string; closed?: string };
}

export function isJobMonitorUpdate(data: unknown): data is UpdateData {
  return Boolean(data && typeof data === "object" && (data as { kind?: unknown }).kind === "job_monitor_update");
}

/** App-relative links only (the chat never navigates off-site except "Open Job"). */
function appLink(link: unknown): string | null {
  return typeof link === "string" && link.startsWith("/") && !link.startsWith("//") ? link : null;
}

/** A job monitor update in the SANA chat: the newest jobs and links to the new / changed / closed lists. */
export function JobMonitorUpdate({ data, onNavigate }: { data: UpdateData; onNavigate?: () => void }) {
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  const counts = data.counts ?? {};
  const links: [string, string | null, number][] = [
    ["new", appLink(data.links?.new), Number(counts.new_count ?? 0)],
    ["changed", appLink(data.links?.changed), Number(counts.changed_count ?? 0)],
    ["closed", appLink(data.links?.closed), Number(counts.closed_count ?? 0)],
  ];
  return (
    <div className="jm-update">
      {jobs.length > 0 && (
        <ul className="jm-update__jobs">
          {jobs.map((job, i) => {
            const meta = [display(job.company), display(job.location), display(job.remote)].filter(Boolean).join(" · ");
            const keywords = joinKeywords(Array.isArray(job.keywords) ? job.keywords : []);
            return (
              <li key={job.id ?? i} className="jm-update__job">
                <div className="min-w-0">
                  <strong>{display(job.title) || <span className="muted">Untitled job</span>}</strong>
                  {meta && <div className="muted small">{meta}</div>}
                  {keywords && <div className="small">{keywords}</div>}
                </div>
                <JobUrl url={job.job_url} className="button button--ghost button--small" stop={false}>Open Job</JobUrl>
              </li>
            );
          })}
        </ul>
      )}
      <div className="actions">
        {links.map(([change, to, count]) =>
          to && (change === "new" || count > 0) ? (
            <Link key={change} className="link small" to={to} onClick={onNavigate}>
              Show {count.toLocaleString()} {change} job{count === 1 ? "" : "s"}
            </Link>
          ) : null,
        )}
        {data.monitor_id && (
          <Link className="link small" to={`/monitors/${encodeURIComponent(data.monitor_id)}`} onClick={onNavigate}>Monitor</Link>
        )}
      </div>
    </div>
  );
}
