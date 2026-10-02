// The 14 mandatory job fields and how a job is displayed. Pure helpers (no JSX,
// no runtime imports) so `npm test` can run them under Node.
//
// Nothing here invents a value: a field the source did not show stays blank, and
// the original job link is always the job_url the API returned, never rebuilt.

/** The mandatory schema, in the exact column order used by imports, previews and exports. */
export const JOB_FIELDS = [
  "Job URL", "Job Title", "Company Name", "Location", "Experience Level", "Salary Budget",
  "Keyword 1", "Keyword 2", "Keyword 3", "Keyword 4", "Keyword 5", "Remote", "Source", "Scraped Date",
] as const;

export type JobField = (typeof JOB_FIELDS)[number];

/** field label -> job_postings column */
export const FIELD_COLUMNS: Record<JobField, string> = {
  "Job URL": "job_url", "Job Title": "title", "Company Name": "company_name", "Location": "location",
  "Experience Level": "experience_level", "Salary Budget": "salary_budget",
  "Keyword 1": "keyword_1", "Keyword 2": "keyword_2", "Keyword 3": "keyword_3", "Keyword 4": "keyword_4", "Keyword 5": "keyword_5",
  "Remote": "remote", "Source": "source", "Scraped Date": "scraped_date",
};

/** An import can only run when every row can carry an identity and a title. */
export const REQUIRED_IMPORT_FIELDS: JobField[] = ["Job URL", "Job Title"];

/** The Jobs table, column by column. */
export const JOB_TABLE_COLUMNS = [
  "Job Title", "Company", "Location", "Experience", "Salary", "Keywords", "Remote", "Source", "Board", "Search Term",
  "Scraped Date", "First Seen", "Last Seen", "Last Changed", "Stale Date", "Closed Date", "Status", "New/Changed",
  "Relevance", "Score", "Reason", "Job URL",
] as const;

export type JobTableColumn = (typeof JOB_TABLE_COLUMNS)[number];

export const REMOTE_OPTIONS = ["Remote", "Hybrid", "On-site", "blank"] as const;
export const STATUS_OPTIONS = ["ACTIVE", "STALE", "EXPIRED", "CLOSED", "UNKNOWN"] as const;
export const CHANGE_OPTIONS = ["new", "changed", "unchanged", "stale", "expired", "closed", "reopened"] as const;

const STATUS_LABELS: Record<string, string> = {
  open: "ACTIVE", stale: "STALE", expired: "EXPIRED", closed: "CLOSED", unknown: "UNKNOWN",
};

export interface JobLike {
  [key: string]: unknown;
}

/** A display string for one value; missing values are "" (the page shows them as blank / "—"). */
export function display(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(display).filter(Boolean).join(", ");
  return "";
}

/** True when a field has nothing to show. */
export function isBlank(value: unknown): boolean {
  return display(value) === "";
}

/** The job's keywords, in source order: the API's `keywords` list, else Keyword 1-5. */
export function keywordsOf(job: JobLike): string[] {
  const listed = job.keywords;
  const values = Array.isArray(listed)
    ? listed
    : ["keyword_1", "keyword_2", "keyword_3", "keyword_4", "keyword_5"].map((k) => job[k]);
  return values.map(display).filter(Boolean);
}

/** "SAP, ABAP, S/4HANA" — or "" when the source showed none. */
export function joinKeywords(job: JobLike | unknown[]): string {
  return Array.isArray(job) ? job.map(display).filter(Boolean).join(", ") : keywordsOf(job).join(", ");
}

/** ACTIVE / STALE / EXPIRED / CLOSED / UNKNOWN from status_label, falling back to the raw status. */
export function statusLabel(job: JobLike): string {
  const label = display(job.status_label);
  if (label) return label.toUpperCase();
  const raw = display(job.status).toLowerCase();
  return raw ? STATUS_LABELS[raw] ?? raw.toUpperCase() : "";
}

export type BadgeTone = "completed" | "running" | "failed" | "queued" | "cancelled" | "expired";

/** The badge tone for a status label: ACTIVE blue, STALE amber, EXPIRED outlined gray, CLOSED red, UNKNOWN gray. */
export function statusTone(label: string): BadgeTone {
  switch (label) {
    case "ACTIVE": return "running";
    case "STALE": return "cancelled";
    case "EXPIRED": return "expired";
    case "CLOSED": return "failed";
    default: return "queued";
  }
}

export function changeTone(badge: string): BadgeTone {
  switch (badge) {
    case "New": return "completed";
    case "Changed": return "running";
    case "Reopened": return "running";
    case "Closed": return "failed";
    case "Stale": return "cancelled";
    case "Expired": return "expired";
    default: return "queued";
  }
}

/** The HTTP status of a direct job-URL check, in words ("HTTP 410 (gone)"). */
export function httpStatusText(status: unknown): string {
  const n = typeof status === "number" ? status : Number(status);
  if (status === null || status === undefined || status === "" || !Number.isFinite(n) || n <= 0) return "";
  const word = n === 410 || n === 404 ? "gone" : n >= 200 && n < 300 ? "still live" : n === 429 || n === 403 ? "refused" : "";
  return word ? `HTTP ${n} (${word})` : `HTTP ${n}`;
}

function positive(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Why a job was closed, in words. */
export function closureReasonText(reason: unknown, details: { missed?: unknown; httpStatus?: unknown } = {}): string {
  const raw = display(reason);
  if (!raw) return "";
  if (raw === "missed_full_sweeps") {
    const n = positive(details.missed);
    return n ? `Missing from ${n} completed full sweep${n === 1 ? "" : "s"}` : "Missing from consecutive completed full sweeps";
  }
  if (raw === "source_gone") {
    const n = positive(details.httpStatus);
    return n ? `Removed at the source (HTTP ${n})` : "Removed at the source";
  }
  if (raw === "manual") return "Closed manually";
  return raw.replace(/_/g, " ");
}

/** A closed job's closure reason in words ("" for any other status). */
export function jobClosureText(job: JobLike): string {
  if (statusLabel(job) !== "CLOSED") return "";
  return closureReasonText(job.closure_reason, { missed: job.missed_full_sweeps, httpStatus: job.gone_status });
}

/** "2026-10-02 · HTTP 410 (gone)" — the last direct check of the job URL; "" when never checked. */
export function goneCheckText(job: JobLike): string {
  return [dateOnly(job.gone_checked_at), httpStatusText(job.gone_status)].filter(Boolean).join(" · ");
}

/** Details of one history entry (a job_posting_changes row) beyond its field diff. */
export function historyDetails(change: JobLike): string[] {
  const kind = display(change.change).toLowerCase();
  const before = (change.before && typeof change.before === "object" ? change.before : {}) as Record<string, unknown>;
  const after = (change.after && typeof change.after === "object" ? change.after : {}) as Record<string, unknown>;
  const out: string[] = [];
  if (kind === "stale") {
    const since = dateOnly(after.active_since);
    const days = display(after.stale_after_days);
    out.push(`Still active${since ? ` since ${since}` : ""}${days ? ` — more than ${days} days` : ""}`);
  } else if (kind === "expired") {
    if (display(after.reason)) out.push(`Expired: ${display(after.reason)}`);
    const listed = dateOnly(after.listing_date);
    if (listed) out.push(`Last listing date ${listed}`);
  } else if (kind === "closed") {
    const reason = display(after.reason) || (positive(after.missed_full_sweeps) ? "missed_full_sweeps" : "");
    const text = closureReasonText(reason, { missed: after.missed_full_sweeps, httpStatus: after.http_status });
    if (text) out.push(text);
  } else if (kind === "reopened") {
    const was = display(before.status);
    const why = closureReasonText(before.closure_reason);
    if (was) out.push(`Seen again after it was ${was}${why ? ` (${why})` : ""}`);
  }
  return out;
}

/**
 * The job_url exactly as stored, when it is a safe http(s) link to open in a new tab.
 * Anything else (javascript:, data:, relative paths, blanks) gives null — never a rebuilt URL.
 */
export function safeJobUrl(url: unknown): string | null {
  if (typeof url !== "string") return null;
  const text = url.trim();
  if (!text) return null;
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  if (!parsed.hostname) return null;
  return text;
}

/** "boards.greenhouse.io/acme/jobs/123" — a readable label for a link; the href stays the original. */
export function urlLabel(url: unknown, max = 60): string {
  const safe = safeJobUrl(url);
  if (!safe) return display(url);
  const parsed = new URL(safe);
  const path = parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/$/, "");
  const label = `${parsed.hostname.replace(/^www\./, "")}${path}${parsed.search}`;
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/** The 14 labelled values of a job: its `fields` object when present, else read from the columns. */
export function fieldValues(job: JobLike): Record<JobField, string> {
  const fields = (job.fields && typeof job.fields === "object" ? job.fields : {}) as Record<string, unknown>;
  const out = {} as Record<JobField, string>;
  for (const label of JOB_FIELDS) {
    out[label] = display(label in fields ? fields[label] : job[FIELD_COLUMNS[label]]);
  }
  return out;
}

/** "2026-09-30" from a timestamp (date only, as stored — no timezone shift); "" when missing. */
export function dateOnly(value: unknown): string {
  const text = display(value);
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(text);
  return match ? match[1] : text;
}

/** A 0–100 relevance score as a whole number ("87"); "" when not scored. */
export function scoreText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? String(Math.round(Math.max(0, Math.min(100, n)))) : "";
}

/** HIGH / REVIEW / REJECT from relevance_class; "" otherwise. */
export function relevanceLabel(value: unknown): string {
  const text = display(value).toUpperCase();
  return text === "HIGH" || text === "REVIEW" || text === "REJECT" ? text : "";
}

/** The value of one Jobs-table column for a job ("" when missing). */
export function tableCell(job: JobLike, column: JobTableColumn): string {
  switch (column) {
    case "Job Title": return display(job.title);
    case "Company": return display(job.company_name);
    case "Location": return display(job.location);
    case "Experience": return display(job.experience_level);
    case "Salary": return display(job.salary_budget);
    case "Remote": return display(job.remote);
    case "Keywords": return joinKeywords(job);
    case "Source": {
      // "JobSpy · Indeed" when the job came from a board through JobSpy.
      const source = display(job.source);
      const board = display(job.source_board);
      return !board ? source : !source ? board : source.toLowerCase() === board.toLowerCase() ? source : `${source} · ${board}`;
    }
    case "Board": return display(job.source_board);
    case "Search Term": return display(job.search_term);
    case "Scraped Date": return display(job.scraped_date);
    case "First Seen": return dateOnly(job.first_seen_at);
    case "Last Seen": return dateOnly(job.last_seen_at);
    case "Last Changed": return dateOnly(job.last_changed_at);
    case "Stale Date": return dateOnly(job.stale_at);
    case "Closed Date": return dateOnly(job.closed_at);
    case "Reason": return display(job.relevance_reason);
    case "Status": return statusLabel(job);
    case "New/Changed": return display(job.change_badge);
    case "Relevance": return relevanceLabel(job.relevance_class);
    case "Score": return scoreText(job.relevance_score);
    case "Job URL": return display(job.job_url);
  }
}

// --- historical import -----------------------------------------------------------------------

export type ImportMapping = Partial<Record<string, string | null>>;

export interface MappingCheck {
  ok: boolean;
  missing: JobField[];
  /** Headers used for more than one field. */
  reused: string[];
  mapped: number;
}

/** An import mapping is usable when Job URL and Job Title are mapped to a file header. */
export function checkMapping(mapping: ImportMapping, headers?: string[]): MappingCheck {
  const known = headers ? new Set(headers) : null;
  const valid = (label: string) => {
    const header = mapping[label];
    return typeof header === "string" && header !== "" && (!known || known.has(header));
  };
  const missing = REQUIRED_IMPORT_FIELDS.filter((label) => !valid(label));
  const counts = new Map<string, number>();
  let mapped = 0;
  for (const label of JOB_FIELDS) {
    if (!valid(label)) continue;
    mapped += 1;
    const header = mapping[label] as string;
    counts.set(header, (counts.get(header) ?? 0) + 1);
  }
  const reused = [...counts].filter(([, n]) => n > 1).map(([h]) => h);
  return { ok: missing.length === 0, missing, reused, mapped };
}

/** A complete mapping object (every field present; null for "not in file"), from the server's suggestion. */
export function normalizeMapping(suggested: ImportMapping | null | undefined, headers: string[]): Record<JobField, string | null> {
  const known = new Set(headers);
  const out = {} as Record<JobField, string | null>;
  for (const label of JOB_FIELDS) {
    const header = suggested?.[label];
    out[label] = typeof header === "string" && known.has(header) ? header : null;
  }
  return out;
}
