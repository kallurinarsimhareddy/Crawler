import type { CompanyInput, JobStatus, JobType } from "../api/types";

export const JOB_TYPE_LABELS: Record<JobType, string> = {
  single_company: "Single company",
  bulk_companies: "Bulk companies",
  weekly_crawl: "Weekly crawl",
  discovery: "Discovery",
};

export const JOB_TYPE_DESCRIPTIONS: Record<JobType, string> = {
  single_company: "Crawl every open job on one company's careers site.",
  bulk_companies: "Crawl a list of companies — paste them or upload a CSV.",
  weekly_crawl: "Run the full scheduled roster crawl.",
  discovery: "Find a company's careers page and job platform, without crawling postings.",
};

export const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
}

export function formatRelative(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(seconds)) return "—";
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return formatDateTime(iso);
}

export function formatDuration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - new Date(startIso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
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
