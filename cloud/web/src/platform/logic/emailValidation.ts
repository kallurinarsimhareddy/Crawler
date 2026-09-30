// Email validation page logic, kept pure (no JSX, no imports) so `npm test` can run it.

export const STATUSES = ["VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER"] as const;
export type Status = (typeof STATUSES)[number];

export interface ResultTab {
  key: string;
  label: string;
  /** Item statuses this tab shows; empty = every row. */
  statuses: string[];
  /** Only rows this provider answered: "local" (built-in) or "emaillistverify". */
  provider?: string;
}

// --- the three user-facing statuses -------------------------------------------------------

/** The only statuses a user sees. Role, disposable, free provider, risky, catch-all, MX, SPF,
 * SMTP ... are evidence behind them, never statuses of their own. */
export const FINAL_STATUSES = ["VALID", "INVALID", "NOT_VERIFIED"] as const;
export type FinalStatus = (typeof FINAL_STATUSES)[number];
export const FINAL_LABELS: Record<FinalStatus, string> = { VALID: "Valid", INVALID: "Invalid", NOT_VERIFIED: "Not verified" };
/** Internal results that are NOT VERIFIED to the user. */
export const NOT_VERIFIED_STATUSES = ["UNKNOWN", "RISKY", "ROLE", "DISPOSABLE", "FREE_PROVIDER"];
/** Mailbox-level verifiers: the only providers whose VALID is a final VALID (mirrors engine.VERIFIERS). */
export const VERIFIERS = ["emaillistverify"];

/** Mirrors the server's engine.final_status(). */
export function finalStatus(status: unknown, provider: unknown): FinalStatus {
  if (status === "INVALID") return "INVALID";
  if (status === "VALID" && VERIFIERS.includes(String(provider))) return "VALID";
  return "NOT_VERIFIED";
}

export const RESULT_TABS: (ResultTab & { countKey?: string })[] = [
  { key: "all", label: "All", statuses: [] },
  { key: "valid", label: "Valid", statuses: ["VALID"], provider: "emaillistverify", countKey: "final_valid" },
  { key: "invalid", label: "Invalid", statuses: ["INVALID"], countKey: "final_invalid" },
  { key: "not_verified", label: "Not Verified", statuses: NOT_VERIFIED_STATUSES, countKey: "final_not_verified" },
];

export const ACCEPTED_EXTENSIONS = [".csv", ".xlsx"];
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
/** Rows per job (the server streams and batches them; no manual splitting). */
export const MAX_UPLOAD_ROWS = 1_000_000;

/** Why a file cannot be uploaded, or null when it can. */
export function uploadProblem(name: string, size: number): string | null {
  const lower = name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "Choose a CSV or XLSX file.";
  if (size <= 0) return "The file is empty.";
  if (size > MAX_UPLOAD_BYTES) return "The file is larger than 100 MB. Save it as CSV (smaller than XLSX) or remove unused columns.";
  return null;
}

export type Counts = Partial<Record<string, number>>;

/** Tab label counts: the sum of the tab's statuses (All = total). */
export function tabCount(tab: ResultTab & { countKey?: string }, counts: Counts): number {
  if (tab.countKey && counts[tab.countKey] !== undefined) return counts[tab.countKey] ?? 0;
  if (tab.statuses.length === 0) return counts.total ?? 0;
  return tab.statuses.reduce((sum, s) => sum + (counts[s] ?? 0), 0);
}

/** 0..100, rounded down so 99.6% never shows as done. */
export function progressPercent(counts: Counts): number {
  const total = counts.total ?? 0;
  if (!total) return 0;
  return Math.min(100, Math.floor(((counts.processed ?? 0) / total) * 100));
}

export interface ItemFilters {
  email?: string;
  domain?: string;
  provider?: string;
  from?: string;
  to?: string;
}

/** The query string fields for GET /email/jobs/{id}/items. */
export function itemQuery(tab: ResultTab, filters: ItemFilters, limit: number, offset: number): Record<string, string | number> {
  const query: Record<string, string | number> = { limit, offset };
  if (tab.statuses.length === 1) query.status = tab.statuses[0];
  else if (tab.statuses.length > 1) query.status__in = tab.statuses.join(",");
  if (filters.email?.trim()) query.email__ilike = filters.email.trim();
  if (filters.domain?.trim()) query.domain = filters.domain.trim().toLowerCase();
  if (tab.provider) query.provider = tab.provider;
  else if (filters.provider?.trim()) query.provider = filters.provider.trim();
  if (filters.from) query.validated_at__gte = filters.from;
  if (filters.to) query.validated_at__lte = filters.to.length === 10 ? `${filters.to}T23:59:59` : filters.to;
  return query;
}

export function exportPath(jobId: string, format: "csv" | "xlsx", tab: ResultTab): string {
  const params = [`format=${format}`];
  if (tab.statuses.length) params.push(`status_filter=${encodeURIComponent(tab.statuses.join(","))}`);
  if (tab.provider) params.push(`provider_filter=${encodeURIComponent(tab.provider)}`);
  return `/email/jobs/${encodeURIComponent(jobId)}/export?${params.join("&")}`;
}

export interface ProviderInfo {
  status?: string;
  configured?: boolean;
  verified?: boolean;
}

/** EmailListVerify is active only when a key is stored AND verified. */
export function providerState(info: ProviderInfo | null | undefined): { label: string; tone: "active" | "warn" | "off"; active: boolean } {
  if (info?.verified === true || info?.status === "active") return { label: "Active", tone: "active", active: true };
  if (info?.configured) return { label: "Configured but not verified", tone: "warn", active: false };
  return { label: "Not configured", tone: "off", active: false };
}

/** Plain-language explanation of one result; mirrors the server's reason_for(). */
export function reasonFor(status: string, checks: Record<string, unknown> | null | undefined): string {
  const c = checks ?? {};
  if (c.empty) return "No email address in this row";
  if (c.paid_error) return `Mailbox not verified (paid check failed: ${String(c.paid_error)})`;
  switch (status) {
    case "VALID":
      return "Mailbox verified by the external provider";
    case "INVALID":
      if (c.syntax === false) return "Not a valid email address";
      if (c.placeholder) return "Placeholder address";
      if (c.mx === false) return "The domain does not accept email";
      return c.result_code ? `Rejected by the external provider (${String(c.result_code)})` : "Undeliverable";
    case "DISPOSABLE":
      return "Disposable (throw-away) mailbox domain";
    case "ROLE":
      return "Role / shared inbox (info@, sales@, hr@ …)";
    case "FREE_PROVIDER":
      return "Free mailbox provider (Gmail, Outlook.com …)";
    case "RISKY":
      return `Accept-all or protected server (${String(c.result_code ?? "risky")})`;
    case "UNKNOWN":
      if (c.dns === "transient failure") return "DNS lookup failed temporarily; try again later";
      if (c.result_code) return `EmailListVerify could not confirm the mailbox (${String(c.result_code)})`;
      if (c.strict_demoted) return "Not externally verified (strict validation)";
      return "Mail server exists; mailbox not verified";
    case "PENDING":
      return "Waiting to be checked";
    default:
      return "";
  }
}

/** Result provenance; mirrors the server's engine.source_label(). */
export function sourceLabel(provider: unknown, checks: Record<string, unknown> | null | undefined): string {
  const c = checks ?? {};
  if (provider === "emaillistverify") {
    return "builtin_status" in c || "syntax" in c || "mx" in c ? "Built-in + EmailListVerify" : "EmailListVerify";
  }
  if (VERIFIERS.includes(String(provider))) return "Other authorized provider";
  const evidence = (c.evidence ?? {}) as { public?: { public_email_evidence?: boolean } };
  return evidence.public?.public_email_evidence === true ? "Built-in + Public evidence" : "Built-in";
}

/** "Use the results" groups: the final statuses, as internal status lists for the server. */
export const ACTION_GROUPS: { key: FinalStatus; statuses: string[] }[] = [
  { key: "VALID", statuses: ["VALID"] },
  { key: "NOT_VERIFIED", statuses: NOT_VERIFIED_STATUSES },
];

/** The evidence card the server attaches to each result (engine.evidence_summary). */
export interface EvidenceSummary {
  technical: string;
  domain: string;
  mx: string;
  spf: string;
  dmarc: string;
  smtp: string;
  catch_all: string;
  role: string;
  disposable: string;
  free_provider: string;
  person_match: string;
  public_evidence: string;
  mailbox_verification: string;
  source: string;
}

export const SUMMARY_LABELS: [keyof EvidenceSummary, string][] = [
  ["technical", "Syntax"], ["domain", "Domain"], ["mx", "MX"], ["spf", "SPF"], ["dmarc", "DMARC"],
  ["smtp", "SMTP"], ["catch_all", "Catch-all"], ["role", "Role"], ["disposable", "Disposable"],
  ["free_provider", "Free provider"], ["person_match", "Person match"], ["public_evidence", "Public evidence"],
  ["mailbox_verification", "Mailbox verification"], ["source", "Source"],
];

/** Tone for an evidence value: good, bad or neutral (UNKNOWN/NO for neutral signals). */
export function signalTone(key: keyof EvidenceSummary, value: string): "good" | "bad" | "neutral" {
  if (key === "source") return "neutral";
  const risky = key === "catch_all" || key === "role" || key === "disposable" || key === "free_provider";
  if (risky) return value === "YES" ? "bad" : value === "NO" ? "good" : "neutral";
  if (value === "PASS" || value === "YES" || value === "VERIFIED") return "good";
  if (value === "FAIL" || value === "INVALID") return key === "spf" || key === "dmarc" ? "neutral" : "bad";
  return "neutral";
}

export interface Candidate {
  column: string;
  score: number;
  reason: string;
}

/** The column to pre-select: the saved choice, else the best candidate, else nothing. */
export function initialColumn(saved: string | null | undefined, candidates: Candidate[] | undefined, columns: string[]): string {
  if (saved && columns.includes(saved)) return saved;
  const best = (candidates ?? []).find((c) => columns.includes(c.column));
  return best ? best.column : "";
}

export function isActive(status: string): boolean {
  return status === "queued" || status === "running";
}

// --- pasted emails ------------------------------------------------------------------------

/** Same row cap as the server's file upload (MAX_ROWS in cloud/intel/email/jobs.py). */
export const MAX_PASTED = 50_000;

// A deliberately loose shape check that only catches obviously broken entries; the server's
// syntax/MX/disposable/role checks (and EmailListVerify) still decide every address.
const EMAIL_SHAPE = /^[^\s@<>(),;:"[\]\\]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$/;

export interface PastedEmails {
  /** Non-empty entries found in the text, duplicates included. */
  total: number;
  /** Distinct entries after lower-casing. */
  unique: number;
  /** Entries dropped because they repeat an earlier one. */
  duplicates: number;
  /** Distinct entries that cannot be an email address (not sent for validation). */
  malformed: string[];
  /** Distinct, well-formed addresses in paste order, capped at MAX_PASTED. */
  emails: string[];
  /** Well-formed addresses left out because of the cap. */
  overLimit: number;
}

/** Clean one pasted token: trim, drop wrapping quotes/brackets and mailto:, lower-case. */
function cleanToken(token: string): string {
  let t = token.trim().replace(/^["'<(\[]+|["'>)\].]+$/g, "");
  if (/^mailto:/i.test(t)) t = t.slice(7);
  return t.trim().toLowerCase();
}

/** Split pasted text on newlines, commas, semicolons, tabs and spaces; normalize and de-duplicate. */
export function parsePastedEmails(text: string, max: number = MAX_PASTED): PastedEmails {
  const seen = new Set<string>();
  const malformed: string[] = [];
  const emails: string[] = [];
  let total = 0;
  let overLimit = 0;
  for (const raw of (text ?? "").split(/[\s,;]+/)) {
    const token = cleanToken(raw);
    if (!token) continue;
    total += 1;
    if (seen.has(token)) continue;
    seen.add(token);
    if (!EMAIL_SHAPE.test(token)) malformed.push(token);
    else if (emails.length < max) emails.push(token);
    else overLimit += 1;
  }
  return { total, unique: seen.size, duplicates: total - seen.size, malformed, emails, overLimit };
}

/** The rows POST /email/jobs {source: "rows"} expects: one {email} object per address. */
export function pastedRows(parsed: PastedEmails): { email: string }[] {
  return parsed.emails.map((email) => ({ email }));
}

/**
 * Worst-case EmailListVerify credits: only addresses the built-in checks cannot decide (and
 * that are not cached) are sent, so the real spend is usually lower. Zero when paid checks are off.
 */
export function estimatedCredits(count: number, costPerCheck: number | null | undefined, paid: boolean): number {
  if (!paid || count <= 0) return 0;
  return Math.ceil(count * (costPerCheck ?? 1));
}

/** Clipboard text: one address per line, blanks and repeats dropped. */
export function emailsToText(emails: Iterable<string | null | undefined>): string {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const e of emails) {
    const v = (e ?? "").trim();
    if (v && !seen.has(v)) {
      seen.add(v);
      out.push(v);
    }
  }
  return out.join("\n");
}

export interface CopyGroup {
  key: string;
  label: string;
  statuses: string[];
  provider?: string;
}

export const COPY_GROUPS: (CopyGroup & { countKey: string })[] = [
  { key: "valid", label: "Copy Valid Emails", statuses: ["VALID"], provider: "emaillistverify", countKey: "final_valid" },
  { key: "invalid", label: "Copy Invalid Emails", statuses: ["INVALID"], countKey: "final_invalid" },
  { key: "not_verified", label: "Copy Not Verified", statuses: NOT_VERIFIED_STATUSES, countKey: "final_not_verified" },
];

type ItemPage = { items: object[]; has_more?: boolean };

/** What a copy button reads. Copy Valid is final VALID only: status VALID *from a mailbox
 * verifier* — never Not Verified (unknown, risky, catch-all, role ...). */
export function copyTarget(group: CopyGroup): { statuses: string[]; provider?: string } {
  return { statuses: group.statuses, provider: group.provider };
}

// --- contact mode: "First Name, Last Name, Company, Title, Email" rows ----------------------

export interface ContactRow {
  first_name: string;
  last_name: string;
  company: string;
  title: string;
  email: string;
}

export interface PastedContacts {
  rows: ContactRow[];
  /** Non-empty lines read as contacts (header excluded). */
  total: number;
  duplicates: number;
  malformed: string[];
  missingEmail: number;
  hasHeader: boolean;
  overLimit: number;
}

const HEADER_KEYS: Record<string, keyof ContactRow> = {
  "first name": "first_name", firstname: "first_name", first: "first_name", "given name": "first_name",
  "last name": "last_name", lastname: "last_name", last: "last_name", surname: "last_name",
  company: "company", "company name": "company", organization: "company", organisation: "company", account: "company",
  title: "title", "job title": "title", position: "title", role: "title",
  email: "email", "e-mail": "email", "email address": "email", "work email": "email",
};
const DEFAULT_ORDER: (keyof ContactRow)[] = ["first_name", "last_name", "company", "title", "email"];

function splitCells(line: string): string[] {
  const sep = line.includes("\t") ? "\t" : line.includes(";") && !line.includes(",") ? ";" : ",";
  return line.split(sep).map((c) => c.trim().replace(/^"|"$/g, "").trim());
}

/** Pasted contact rows (a spreadsheet copy is tab-separated; CSV works too). A header row is
 * optional; without one the order is First Name, Last Name, Company, Title, Email, and the cell
 * holding an "@" is taken as the email wherever it sits. */
export function parseContactRows(text: string, max: number = MAX_PASTED): PastedContacts {
  const lines = (text ?? "").split(/\r?\n/).filter((l) => l.trim());
  let order: (keyof ContactRow | null)[] = DEFAULT_ORDER;
  let hasHeader = false;
  if (lines.length) {
    const cells = splitCells(lines[0]).map((c) => c.toLowerCase().replace(/[_-]+/g, " ").trim());
    if (cells.some((c) => HEADER_KEYS[c] === "email") && !cells.some((c) => c.includes("@"))) {
      order = cells.map((c) => HEADER_KEYS[c] ?? null);
      hasHeader = true;
      lines.shift();
    }
  }
  const seen = new Set<string>();
  const rows: ContactRow[] = [];
  const malformed: string[] = [];
  let duplicates = 0, missingEmail = 0, overLimit = 0;
  for (const line of lines) {
    const cells = splitCells(line);
    const row: ContactRow = { first_name: "", last_name: "", company: "", title: "", email: "" };
    let emailCell = hasHeader ? order.indexOf("email") : cells.findIndex((c) => c.includes("@"));
    if (emailCell < 0) emailCell = -1;
    const others = hasHeader ? order : DEFAULT_ORDER.filter((k) => k !== "email");
    let next = 0;
    cells.forEach((cell, i) => {
      if (i === emailCell) return;
      const key = hasHeader ? order[i] : others[next++];
      if (key && key !== "email") row[key as keyof ContactRow] = cell;
    });
    const parsed = emailCell >= 0 ? parsePastedEmails(cells[emailCell] ?? "") : null;
    if (!parsed || parsed.total === 0) {
      missingEmail += 1;
      continue;
    }
    if (parsed.emails.length === 0) {
      malformed.push(...parsed.malformed);
      continue;
    }
    row.email = parsed.emails[0];
    if (seen.has(row.email)) {
      duplicates += 1;
      continue;
    }
    seen.add(row.email);
    if (rows.length < max) rows.push(row);
    else overLimit += 1;
  }
  return { rows, total: lines.length, duplicates, malformed, missingEmail, hasHeader, overLimit };
}

/**
 * Every result email with one of `statuses`, read page by page through the same items
 * endpoint the table uses (`fetchPage` receives its query), newline-joined for the clipboard.
 */
export async function collectEmails(
  fetchPage: (query: Record<string, string | number>) => Promise<ItemPage>,
  target: string[] | { statuses: string[]; provider?: string },
  pageSize = 500,
): Promise<string> {
  const found: string[] = [];
  const { statuses, provider } = Array.isArray(target) ? { statuses: target, provider: undefined } : target;
  const tab: ResultTab = { key: "copy", label: "copy", statuses, provider };
  for (let offset = 0; ; offset += pageSize) {
    const page = await fetchPage(itemQuery(tab, {}, pageSize, offset));
    for (const item of page.items) {
      const email = (item as { email?: unknown }).email;
      if (typeof email === "string") found.push(email);
    }
    if (!page.has_more || page.items.length === 0) break;
  }
  return emailsToText(found);
}
