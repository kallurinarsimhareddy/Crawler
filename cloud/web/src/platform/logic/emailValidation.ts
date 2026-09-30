// Email validation page logic, kept pure (no JSX, no imports) so `npm test` can run it.

export const STATUSES = ["VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER"] as const;
export type Status = (typeof STATUSES)[number];

export interface ResultTab {
  key: string;
  label: string;
  /** Item statuses this tab shows; empty = every row. */
  statuses: string[];
}

export const RESULT_TABS: ResultTab[] = [
  { key: "all", label: "All", statuses: [] },
  { key: "valid", label: "Valid", statuses: ["VALID"] },
  { key: "invalid", label: "Invalid", statuses: ["INVALID"] },
  { key: "risky", label: "Risky", statuses: ["RISKY"] },
  { key: "role", label: "Role", statuses: ["ROLE"] },
  { key: "disposable", label: "Disposable", statuses: ["DISPOSABLE"] },
  { key: "free", label: "Free provider", statuses: ["FREE_PROVIDER"] },
  { key: "unknown", label: "Unknown", statuses: ["UNKNOWN"] },
];

export const ACCEPTED_EXTENSIONS = [".csv", ".xlsx"];
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

/** Why a file cannot be uploaded, or null when it can. */
export function uploadProblem(name: string, size: number): string | null {
  const lower = name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) return "Choose a CSV or XLSX file.";
  if (size <= 0) return "The file is empty.";
  if (size > MAX_UPLOAD_BYTES) return "The file is larger than 25 MB. Split it into smaller files.";
  return null;
}

export type Counts = Partial<Record<string, number>>;

/** Tab label counts: the sum of the tab's statuses (All = total). */
export function tabCount(tab: ResultTab, counts: Counts): number {
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
  if (filters.provider?.trim()) query.provider = filters.provider.trim();
  if (filters.from) query.validated_at__gte = filters.from;
  if (filters.to) query.validated_at__lte = filters.to.length === 10 ? `${filters.to}T23:59:59` : filters.to;
  return query;
}

export function exportPath(jobId: string, format: "csv" | "xlsx", tab: ResultTab): string {
  const params = [`format=${format}`];
  if (tab.statuses.length) params.push(`status_filter=${encodeURIComponent(tab.statuses.join(","))}`);
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
      return "Mail server exists; mailbox not verified";
    case "PENDING":
      return "Waiting to be checked";
    default:
      return "";
  }
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
