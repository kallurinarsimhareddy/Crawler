// Account Builder (Jobs -> Hiring companies): pure helpers, unit-tested.

export const ACCOUNT_STATES = ["NEW ACCOUNT", "ALREADY IN CRM", "MATCHED", "REVIEW", "LINKED"] as const;
export type AccountState = (typeof ACCOUNT_STATES)[number];

export interface CrmRef {
  id: string;
  name: string;
  domain?: string | null;
}

export interface HiringCompany {
  key: string;
  display_name: string;
  names: string[];
  jobs: number;
  open_jobs: number;
  high: number;
  review: number;
  linked_jobs?: number;
  technologies?: string[];
  signals?: string[];
  signal_ids?: string[];
  job_ids?: string[];
  hiring_score?: number | null;
  score_source?: "crm" | "signals";
  state: AccountState;
  crm?: CrmRef[];
  match_reason?: string | null;
  review_id?: string | null;
}

export interface BuildItem {
  key: string;
  names: string[];
  display_name: string;
  signal_ids: string[];
  job_ids: string[];
  create_new?: boolean;
}

/** Limits the server enforces (shown before the user acts). */
export const LIMITS = { maxPerAction: 500, confirmAbove: 50, syncLimit: 5 } as const;

export function stateLabel(state: string): string {
  return ({
    "NEW ACCOUNT": "New account", "ALREADY IN CRM": "Already in CRM", MATCHED: "Matched", REVIEW: "Review",
    LINKED: "Linked",
  } as Record<string, string>)[state] ?? state;
}

export function stateTone(state: string): string {
  return ({
    "NEW ACCOUNT": "queued", "ALREADY IN CRM": "running", MATCHED: "running", REVIEW: "cancelled", LINKED: "completed",
  } as Record<string, string>)[state] ?? "queued";
}

/** What "Create account" will do for a row, in words. */
export function createEffect(row: HiringCompany): string {
  switch (row.state) {
    case "LINKED": return "Already linked — refreshes its jobs and signals";
    case "ALREADY IN CRM":
    case "MATCHED": return `Links to ${row.crm?.[0]?.name ?? "the existing company"} (no duplicate)`;
    case "REVIEW": return "Ambiguous — goes to review unless you choose “Create new anyway”";
    default: return "Creates a new CRM account";
  }
}

export function toItem(row: HiringCompany, createNew = false): BuildItem {
  return {
    key: row.key, names: row.names, display_name: row.display_name, signal_ids: row.signal_ids ?? [],
    job_ids: row.job_ids ?? [], ...(createNew ? { create_new: true } : {}),
  };
}

/** The selection check the dialog shows before sending (mirrors the server's guard). */
export function selectionCheck(count: number, confirmed: boolean): { ok: boolean; message: string | null; background: boolean } {
  if (count === 0) return { ok: false, message: "Select at least one company.", background: false };
  if (count > LIMITS.maxPerAction) return { ok: false, message: `At most ${LIMITS.maxPerAction} companies per action (${count} selected).`, background: false };
  if (count > LIMITS.confirmAbove && !confirmed) return { ok: false, message: `${count} companies selected — confirm to continue.`, background: count > LIMITS.syncLimit };
  return { ok: true, message: null, background: count > LIMITS.syncLimit };
}

/** List-member provenance written by the Account Builder (JSON in added_reason). */
export interface MemberProvenance {
  source: string;
  at?: string;
  signals: number;
  jobs: number;
  company?: string;
}

export function parseProvenance(reason: unknown): MemberProvenance | null {
  if (typeof reason !== "string" || !reason.startsWith("{")) return null;
  try {
    const value = JSON.parse(reason) as Record<string, unknown>;
    if (value.source !== "account_builder") return null;
    return {
      source: "Account Builder", at: typeof value.at === "string" ? value.at : undefined,
      signals: Array.isArray(value.signal_ids) ? value.signal_ids.length : 0,
      jobs: Array.isArray(value.job_ids) ? value.job_ids.length : 0,
      company: typeof value.company_name === "string" ? value.company_name : undefined,
    };
  } catch {
    return null;
  }
}

export function provenanceText(reason: unknown): string {
  const p = parseProvenance(reason);
  if (!p) return typeof reason === "string" && reason ? reason : "—";
  const parts = [p.source];
  if (p.signals) parts.push(`${p.signals} originating signal${p.signals === 1 ? "" : "s"}`);
  if (p.jobs) parts.push(`${p.jobs} originating job${p.jobs === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** Where a list member links to (never a company page for a job or a deal). */
export function memberLink(entityType: string, id: string): string {
  switch (entityType) {
    case "contacts": return `/contacts/${encodeURIComponent(id)}`;
    case "job_postings": return `/jobs/${encodeURIComponent(id)}`;
    case "opportunities": return `/opportunities/${encodeURIComponent(id)}`;
    default: return `/companies/${encodeURIComponent(id)}`;
  }
}

/** A record's display name, never its raw id. */
export function recordName(record: Record<string, unknown> | null | undefined): string {
  if (!record) return "(removed)";
  for (const key of ["name", "full_name", "title", "email"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "(unnamed)";
}

/** A hiring signal as an Account Builder row (the server resolves the company identity). */
export function signalAsCompany(signal: Record<string, unknown>): HiringCompany | null {
  const name = typeof signal.company_name === "string" ? signal.company_name.trim() : "";
  const companyId = typeof signal.company_id === "string" ? signal.company_id : null;
  if (!name && !companyId) return null;
  const jobs = Array.isArray(signal.job_posting_ids) ? (signal.job_posting_ids as string[]) : [];
  return {
    key: "", display_name: name || "CRM company", names: name ? [name] : [], jobs: jobs.length, open_jobs: jobs.length,
    high: 0, review: 0, signal_ids: typeof signal.id === "string" ? [signal.id] : [], job_ids: jobs.slice(0, 10),
    signals: typeof signal.signal_type === "string" ? [signal.signal_type] : [],
    state: companyId ? "LINKED" : "NEW ACCOUNT", crm: companyId ? [{ id: companyId, name: name || "CRM company" }] : [],
  };
}

/** /jobs filtered to one hiring company's postings. */
export function jobsForCompany(name: string): string {
  return `/jobs?company=${encodeURIComponent(name)}`;
}

/**
 * States that can be filtered and counted across every company. MATCHED is kept for API
 * compatibility but no longer produced by the page (name-only evidence is never a safe match).
 */
export const FILTER_STATES = ["NEW ACCOUNT", "ALREADY IN CRM", "REVIEW", "LINKED"] as const;

/** URL keys the Hiring companies view keeps (alongside the Jobs filters) so Back restores it. */
export const VIEW_KEYS = { state: "hstate", sort: "hsort", offset: "hoffset" } as const;
export const SORTS = ["open", "high", "jobs", "name"] as const;

export function readView(params: URLSearchParams): { state: string; sort: string; offset: number } {
  const state = (params.get(VIEW_KEYS.state) ?? "").toUpperCase();
  const sort = params.get(VIEW_KEYS.sort) ?? "open";
  const offset = Number(params.get(VIEW_KEYS.offset) ?? 0);
  return {
    state: (FILTER_STATES as readonly string[]).includes(state) ? state : "",
    sort: (SORTS as readonly string[]).includes(sort) ? sort : "open",
    offset: Number.isInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** The Jobs-tab filters carried into another Jobs tab (paging and view keys are dropped). */
export function carryFilters(params: URLSearchParams, keys: readonly string[], tab: string): string {
  const next = new URLSearchParams();
  for (const key of keys) {
    const value = params.get(key);
    if (value) next.set(key, value);
  }
  if (tab !== "jobs") next.set("tab", tab);
  const s = next.toString();
  return s ? `/jobs?${s}` : "/jobs";
}

/** A background task has stopped (finished, failed, cancelled or paused) — task statuses as stored. */
export function taskStopped(status: unknown): boolean {
  return ["completed", "failed", "cancelled", "paused"].includes(String(status ?? ""));
}
