// Jobs table filters: the URL query string <-> filter object (so deep links from
// notifications and SANA chat work), the deep-link banner, and the advanced
// AND/OR condition tree POSTed to job-feed/search. Pure: no JSX, no runtime imports.

/** Every simple filter the Jobs page keeps in its URL, in display order. */
export const FILTER_KEYS = [
  "q", "source", "company", "title", "location", "country", "experience", "salary", "remote", "keyword", "status",
  "relevance", "relevance_min", "category", "source_board", "search_term",
  "scraped_from", "scraped_to", "first_seen_from", "first_seen_to", "last_changed_from", "last_changed_to",
  "change", "since", "monitor", "run", "import", "since_last_run", "company_id", "order",
] as const;

export type FilterKey = (typeof FILTER_KEYS)[number];
export type JobFilters = Partial<Record<FilterKey, string>>;

export const PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 500;

export const ORDER_OPTIONS = [
  "-first_seen_at", "first_seen_at", "-last_changed_at", "-last_seen_at", "-scraped_date", "scraped_date",
  "title", "company_name", "location", "-relevance_score",
] as const;

const KEYS = new Set<string>(FILTER_KEYS);
/**
 * The change scopes the job feed understands (?change=). "unchanged" = seen in the run but neither new nor
 * changed. "stale" and "expired" are lifecycle states (every STALE / EXPIRED job, optionally ?since=YYYY-MM-DD).
 */
export const CHANGE_FILTERS = ["new", "changed", "unchanged", "stale", "expired", "closed", "reopened"] as const;
export type ChangeFilter = (typeof CHANGE_FILTERS)[number];
/** Change scopes that are lifecycle states rather than events of one run. */
export const STATE_CHANGES: readonly ChangeFilter[] = ["stale", "expired"];

const CHANGES = new Set<string>(CHANGE_FILTERS);
const RELEVANCE = ["HIGH", "REVIEW", "REJECT"];

/** "review, high,bogus" -> "HIGH,REVIEW" (known classes, stable order); "" when none. */
export function normalizeRelevance(value: string): string {
  const picked = new Set(value.split(",").map((s) => s.trim().toUpperCase()));
  return RELEVANCE.filter((c) => picked.has(c)).join(",");
}

/** Read the filters (known keys only, trimmed, blanks dropped) from a query string. */
export function parseFilters(search: string | URLSearchParams): JobFilters {
  const params = typeof search === "string" ? new URLSearchParams(search.startsWith("?") ? search.slice(1) : search) : search;
  const out: JobFilters = {};
  for (const [key, raw] of params) {
    if (!KEYS.has(key)) continue;
    const value = raw.trim();
    if (value) out[key as FilterKey] = value;
  }
  if (out.change && !CHANGES.has(out.change.toLowerCase())) delete out.change;
  else if (out.change) out.change = out.change.toLowerCase();
  if (out.since_last_run && !["1", "true"].includes(out.since_last_run)) delete out.since_last_run;
  if (out.since !== undefined) {
    const day = /^(\d{4}-\d{2}-\d{2})/.exec(out.since);
    if (day) out.since = day[1];
    else delete out.since;
  }
  if (out.relevance !== undefined) {
    const relevance = normalizeRelevance(out.relevance);
    if (relevance) out.relevance = relevance;
    else delete out.relevance;
  }
  if (out.relevance_min !== undefined) {
    const n = Number(out.relevance_min);
    if (!Number.isFinite(n) || n < 0 || n > 100) delete out.relevance_min;
  }
  return out;
}

/** Paging from the URL: ?offset=&limit= (limit capped at 500). */
export function parsePage(search: string | URLSearchParams): { limit: number; offset: number } {
  const params = typeof search === "string" ? new URLSearchParams(search.startsWith("?") ? search.slice(1) : search) : search;
  const limit = Number(params.get("limit"));
  const offset = Number(params.get("offset"));
  return {
    limit: Number.isInteger(limit) && limit > 0 ? Math.min(limit, MAX_PAGE_SIZE) : PAGE_SIZE,
    offset: Number.isInteger(offset) && offset > 0 ? offset : 0,
  };
}

/** The query string for filters (+ paging), in a stable key order; "" when there is nothing to say. */
export function toQueryString(filters: JobFilters, page?: { limit?: number; offset?: number }): string {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = filters[key]?.trim();
    if (value) params.set(key, value);
  }
  if (page?.offset) params.set("offset", String(page.offset));
  if (page?.limit && page.limit !== PAGE_SIZE) params.set("limit", String(page.limit));
  return params.toString();
}

/** Set (or clear, with "") one filter; any filter change goes back to the first page. */
export function withFilter(filters: JobFilters, key: FilterKey, value: string): JobFilters {
  const next = { ...filters };
  if (value.trim()) next[key] = value.trim();
  else delete next[key];
  return next;
}

export function activeCount(filters: JobFilters): number {
  return FILTER_KEYS.filter((k) => k !== "order" && k !== "q" && filters[k]).length;
}

// --- deep links ------------------------------------------------------------------------------

export interface DeepLink {
  monitor: string | null;
  run: string | null;
  change: ChangeFilter | null;
  importId: string | null;
  sinceLastRun: boolean;
  since: string | null;
}

/** What a deep link such as /jobs?monitor=m&run=r&change=new asks for. */
export function deepLink(filters: JobFilters): DeepLink {
  return {
    monitor: filters.monitor ?? null,
    run: filters.run ?? null,
    change: (filters.change as DeepLink["change"]) ?? null,
    importId: filters.import ?? null,
    sinceLastRun: filters.since_last_run === "1" || filters.since_last_run === "true",
    since: filters.since ?? null,
  };
}

/** True when the page should explain the scope (a change / monitor / run / import in the URL). */
export function hasScope(filters: JobFilters): boolean {
  const link = deepLink(filters);
  return Boolean(link.change || link.monitor || link.run || link.importId);
}

/** "Showing 50 new jobs from run sc_1 of Acme careers" — the banner over a deep-linked table. */
export function scopeBanner(filters: JobFilters, total: number | null, monitorName?: string | null): string {
  const link = deepLink(filters);
  const count = total === null ? "" : `${total.toLocaleString("en-US")} `;
  const change = link.change ?? (link.sinceLastRun ? "new" : null);
  const noun = total === 1 ? "job" : "jobs";
  const what = change ? `${count}${change} ${noun}` : `${count}${noun}`;
  const monitor = link.monitor ? (monitorName?.trim() || `monitor ${link.monitor}`) : null;
  let text = `Showing ${what}`;
  const state = change !== null && STATE_CHANGES.includes(change);
  if (link.run) text += ` from run ${link.run}`;
  else if (!state && (link.sinceLastRun || (link.monitor && change && !link.since))) text += " from the last run";
  if (monitor) text += `${link.run || link.sinceLastRun || change ? " of" : " from"} ${monitor}`;
  if (link.importId) text += ` imported in ${link.importId}`;
  if (link.since && change) text += ` since ${link.since}`;
  return text;
}

/** The filters without the deep-link scope (what "clear" keeps). */
export function clearScope(filters: JobFilters): JobFilters {
  const next = { ...filters };
  for (const key of ["change", "since", "monitor", "run", "import", "since_last_run"] as FilterKey[]) delete next[key];
  return next;
}

/** /jobs?monitor=<id>&run=<run>&change=<change> */
export function jobsLink(params: { monitor?: string; run?: string; change?: string; import?: string; since_last_run?: boolean; since?: string }): string {
  const filters: JobFilters = {};
  if (params.monitor) filters.monitor = params.monitor;
  if (params.run) filters.run = params.run;
  if (params.change) filters.change = params.change;
  if (params.since) filters.since = params.since;
  if (params.import) filters.import = params.import;
  if (params.since_last_run) filters.since_last_run = "1";
  const qs = toQueryString(filters);
  return qs ? `/jobs?${qs}` : "/jobs";
}

/** /monitors?new=<url> — the New monitor form pre-filled with a source URL. */
export function newMonitorLink(url: string): string {
  return `/monitors?new=${encodeURIComponent(url.trim())}`;
}

// --- advanced conditions ---------------------------------------------------------------------

export const CONDITION_FIELDS = [
  "title", "company", "location", "country", "experience", "salary", "remote", "keyword", "source", "status",
  "scraped_date", "first_seen", "last_seen", "last_changed", "monitor", "company_id",
  "relevance_score", "relevance", "source_board", "search_term",
] as const;

export const CONDITION_OPS = ["eq", "ne", "contains", "in", "gte", "lte", "gt", "lt", "empty", "not_empty"] as const;

export type ConditionField = (typeof CONDITION_FIELDS)[number];
export type ConditionOp = (typeof CONDITION_OPS)[number];

export interface Leaf {
  field: string;
  op: string;
  value?: unknown;
}

export type Group = { all: Node[] } | { any: Node[] };
export type Node = Leaf | Group;

const NO_VALUE = new Set(["empty", "not_empty"]);

export function isGroup(node: Node): node is Group {
  return typeof node === "object" && node !== null && ("all" in node || "any" in node);
}

export function groupMode(group: Group): "all" | "any" {
  return "any" in group ? "any" : "all";
}

export function children(group: Group): Node[] {
  return ("any" in group ? group.any : group.all) ?? [];
}

export function emptyGroup(mode: "all" | "any" = "all"): Group {
  return mode === "any" ? { any: [] } : { all: [] };
}

/** The value a condition sends: a list for "in", nothing for empty/not_empty, else the text. */
export function parseConditionValue(op: string, raw: string): unknown {
  if (NO_VALUE.has(op)) return undefined;
  if (op === "in") return raw.split(",").map((s) => s.trim()).filter(Boolean);
  return raw;
}

export function conditionValueText(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  return value === undefined || value === null ? "" : String(value);
}

function leafProblem(leaf: Leaf): string | null {
  if (!(CONDITION_FIELDS as readonly string[]).includes(leaf.field)) return `Unknown field "${leaf.field}"`;
  if (!(CONDITION_OPS as readonly string[]).includes(leaf.op)) return `Unknown operator "${leaf.op}"`;
  if (NO_VALUE.has(leaf.op)) return null;
  const value = leaf.value;
  if (Array.isArray(value) ? value.length === 0 : conditionValueText(value).trim() === "") return `${leaf.field} ${leaf.op}: needs a value`;
  return null;
}

/** Every problem in a condition tree ([] when it can be sent). Empty groups are allowed (they are dropped). */
export function validateConditions(group: Group, depth = 0): string[] {
  if (depth > 3) return ["Conditions are nested too deeply"];
  const problems: string[] = [];
  for (const node of children(group)) {
    if (isGroup(node)) problems.push(...validateConditions(node, depth + 1));
    else {
      const problem = leafProblem(node);
      if (problem) problems.push(problem);
    }
  }
  return problems;
}

/**
 * The tree to send: invalid / blank conditions and empty groups removed, values
 * normalised. null when nothing is left (then no `conditions` key is sent).
 */
export function buildConditions(group: Group): Group | null {
  const mode = groupMode(group);
  const kept: Node[] = [];
  for (const node of children(group)) {
    if (isGroup(node)) {
      const inner = buildConditions(node);
      if (inner) kept.push(inner);
    } else if (!leafProblem(node)) {
      const leaf: Leaf = { field: node.field, op: node.op };
      if (!NO_VALUE.has(node.op)) leaf.value = Array.isArray(node.value) ? node.value : conditionValueText(node.value).trim();
      kept.push(leaf);
    }
  }
  if (kept.length === 0) return null;
  return mode === "any" ? { any: kept } : { all: kept };
}

/** The POST job-feed/search body: the simple filters, the condition tree and paging. */
export function searchBody(filters: JobFilters, conditions: Group | null, page: { limit: number; offset: number }): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const key of FILTER_KEYS) if (filters[key]) body[key] = filters[key];
  const tree = conditions ? buildConditions(conditions) : null;
  if (tree) body.conditions = tree;
  body.limit = page.limit;
  body.offset = page.offset;
  if (!body.order) body.order = "-first_seen_at";
  return body;
}

/** Condition trees travel in the URL as JSON (?conditions=…) so an advanced search can be shared. */
export function parseConditionsParam(raw: string | null): Group | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as unknown;
    if (value && typeof value === "object" && ("all" in value || "any" in value)) {
      const list = (value as Record<string, unknown>)["all" in value ? "all" : "any"];
      if (Array.isArray(list)) return value as Group;
    }
  } catch {
    return null;
  }
  return null;
}
