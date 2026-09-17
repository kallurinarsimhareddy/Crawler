import type { CompanyInput, JobStatus, JobType, ResultKind, TargetStatus } from "../api/types";

export const JOB_TYPE_LABELS: Record<JobType, string> = {
  single_company: "Single company",
  bulk_companies: "Bulk companies",
  weekly_crawl: "Weekly crawl",
  discovery: "Discovery",
};

export const JOB_TYPE_DESCRIPTIONS: Record<JobType, string> = {
  single_company: "Crawl every open job on one company's careers site.",
  bulk_companies: "Crawl a list of companies — paste them or upload a CSV.",
  weekly_crawl: "Run the full scheduled roster crawl. Not yet available in the cloud.",
  discovery: "Find a company's careers page and job platform. Not yet available in the cloud.",
};

export const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const TARGET_STATUS_LABELS: Record<TargetStatus, string> = {
  pending: "Pending",
  running: "Crawling",
  completed: "Done",
  failed: "Failed",
  skipped: "Skipped",
};

const PHASE_LABELS: Record<string, string> = {
  queued: "Waiting for a worker",
  starting: "Starting",
  crawling: "Crawling",
  saving_results: "Saving results",
  retry_scheduled: "Retry scheduled",
  requeued: "Requeued after a worker stopped",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  unsupported: "Not supported yet",
};

export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "—";
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");
}

export const RESULT_LABELS: Record<ResultKind, string> = {
  jobs_xlsx: "Jobs (Excel)",
  jobs_csv: "Jobs (CSV)",
  summary_json: "Summary (JSON)",
  crawl_log: "Crawl log",
};

const EVENT_LABELS: Record<string, string> = {
  created: "Job created",
  claimed: "Picked up by a worker",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  cancel_requested: "Cancellation requested",
  retry_scheduled: "Retry scheduled",
  reaped_requeued: "Worker stopped responding — requeued",
  reaped_failed: "Worker stopped responding — failed",
  reaped_cancelled: "Worker stopped responding — cancelled",
  released_on_shutdown: "Worker restarting — requeued",
};

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] ?? kind.replace(/_/g, " ");
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const timeOnly = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
}

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : timeOnly.format(date);
}

export function formatSeconds(total: number | null): string {
  if (total === null || !Number.isFinite(total)) return "—";
  const seconds = Math.max(0, Math.round(total));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatDuration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  return formatSeconds((end - new Date(startIso).getTime()) / 1000);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function shortId(jobId: string): string {
  return jobId.replace(/^job_/, "").slice(0, 8);
}

// A token that looks like a domain: has a dot, no spaces, and no characters a
// company name would have but a hostname would not.
const WEBSITE_LIKE = /^(https?:\/\/)?[^\s,@]+\.[^\s,@]+$/i;

/**
 * Parse pasted or uploaded company lines. Each line is a website, a company
 * name, or "Name, website" in either order. A header row naming the columns is
 * skipped. The API does the real validation; this only shapes the request.
 */
export function parseCompanyLines(text: string): CompanyInput[] {
  const companies: CompanyInput[] = [];
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

  lines.forEach((line, index) => {
    const cells = line.split(/[,\t;]/).map((cell) => cell.trim().replace(/^"|"$/g, "")).filter(Boolean);
    if (index === 0 && cells.some((cell) => /^(company|company[ _]name|name|website|domain|url)$/i.test(cell))) {
      return;
    }
    const company: CompanyInput = {};
    for (const cell of cells) {
      if (!company.website && WEBSITE_LIKE.test(cell)) company.website = cell;
      else if (!company.company_name) company.company_name = cell;
    }
    if (company.website || company.company_name) companies.push(company);
  });
  return companies;
}
