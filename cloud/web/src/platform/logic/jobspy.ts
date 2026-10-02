// JobSpy monitors and job relevance: the New monitor form's JobSpy branch (search
// terms, validation, the POST job-monitors body), how a JobSpy monitor reads back,
// and the relevance badge. Pure: no JSX, no runtime imports, so `npm test` runs it.

export const MAX_SEARCH_TERMS = 25;
export const HOURS_MIN = 1;
export const HOURS_MAX = 720;
export const RESULTS_MIN = 1;
export const RESULTS_MAX = 200;

/** Freshness presets (hours_old); anything else is "custom". */
export const FRESHNESS_OPTIONS = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 48, label: "Last 48 hours" },
  { hours: 72, label: "Last 3 days" },
  { hours: 168, label: "Last 7 days" },
] as const;

export const BOARD_DISABLED_NOTE = "disabled until access is authorized";

export interface JobSpyBoard {
  id: string;
  label: string;
  enabled: boolean;
  note?: string | null;
}

export interface JobSpyDefaults {
  boards: string[];
  location: string;
  hours_old: number;
  results_wanted: number;
  country: string;
}

export const JOBSPY_DEFAULTS: JobSpyDefaults = { boards: ["indeed"], location: "United States", hours_old: 24, results_wanted: 25, country: "USA" };

export interface JobSpyForm {
  name: string;
  schedule: string;
  runNow: boolean;
  boards: string[];
  terms: string[];
  location: string;
  hoursOld: string;
  resultsWanted: string;
}

/**
 * Search terms from a comma / newline separated text (or a list): trimmed, blanks
 * dropped, case-insensitive duplicates removed (first spelling kept), at most 25.
 */
export function parseSearchTerms(input: string | readonly string[], max = MAX_SEARCH_TERMS): string[] {
  const parts = typeof input === "string" ? input.split(/[,\n\r;]+/) : input.flatMap((s) => String(s).split(/[,\n\r;]+/));
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of parts) {
    const term = raw.replace(/\s+/g, " ").trim();
    if (!term) continue;
    const key = term.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(term);
    if (out.length >= max) break;
  }
  return out;
}

/** How many distinct terms the text holds before the cap (to warn that some were dropped). */
export function countSearchTerms(input: string | readonly string[]): number {
  return parseSearchTerms(input, Number.MAX_SAFE_INTEGER).length;
}

function intIn(raw: string, low: number, high: number): number | null {
  const text = String(raw).trim();
  if (!/^\d+$/.test(text)) return null;
  const n = Number(text);
  return n >= low && n <= high ? n : null;
}

/** Every problem with a JobSpy form ([] when it can be saved). */
export function validateJobSpyForm(form: JobSpyForm): string[] {
  const problems: string[] = [];
  if (form.boards.length === 0) problems.push("Choose at least one board.");
  if (form.terms.length === 0) problems.push("Add at least one keyword.");
  if (form.terms.length > MAX_SEARCH_TERMS) problems.push(`At most ${MAX_SEARCH_TERMS} keywords.`);
  if (intIn(form.hoursOld, HOURS_MIN, HOURS_MAX) === null) problems.push(`Freshness must be a whole number of hours from ${HOURS_MIN} to ${HOURS_MAX}.`);
  if (intIn(form.resultsWanted, RESULTS_MIN, RESULTS_MAX) === null) problems.push(`Results limit must be from ${RESULTS_MIN} to ${RESULTS_MAX} per search.`);
  return problems;
}

/** The POST job-monitors body for a JobSpy monitor. Throws when the form is invalid. */
export function buildJobSpyBody(form: JobSpyForm, country = JOBSPY_DEFAULTS.country): Record<string, unknown> {
  const problems = validateJobSpyForm(form);
  if (problems.length) throw new Error(problems.join(" "));
  const body: Record<string, unknown> = {
    strategy: "jobspy",
    schedule: form.schedule,
    filters: {
      boards: [...new Set(form.boards)],
      search_terms: parseSearchTerms(form.terms),
      location: form.location.trim() || JOBSPY_DEFAULTS.location,
      hours_old: Number(form.hoursOld.trim()),
      results_wanted: Number(form.resultsWanted.trim()),
      country,
    },
  };
  if (form.name.trim()) body.name = form.name.trim();
  if (form.runNow) body.run_now = true;
  return body;
}

/** Selected boards that are not enabled yet (their runs are reported as partial). */
export function disabledSelected(boards: readonly JobSpyBoard[], selected: readonly string[]): JobSpyBoard[] {
  return boards.filter((b) => selected.includes(b.id) && !b.enabled);
}

/** "Last 24 hours" / "Last 36 hours" for an hours_old value. */
export function freshnessLabel(hours: unknown): string {
  const n = typeof hours === "number" ? hours : Number(hours);
  if (!Number.isFinite(n) || n <= 0) return "";
  const preset = FRESHNESS_OPTIONS.find((o) => o.hours === n);
  return preset ? preset.label : `Last ${n} hour${n === 1 ? "" : "s"}`;
}

// --- reading a JobSpy monitor back ---------------------------------------------------------

export interface JobSpySummary {
  boards: string[];
  terms: string[];
  location: string;
  freshness: string;
  results: string;
}

const BOARD_FALLBACK: Record<string, string> = { indeed: "Indeed", linkedin: "LinkedIn", zip_recruiter: "ZipRecruiter", glassdoor: "Glassdoor", google: "Google" };

export function isJobSpyMonitor(monitor: { strategy?: unknown; source_name?: unknown } | null | undefined): boolean {
  if (!monitor) return false;
  return monitor.strategy === "jobspy" || (monitor.strategy === undefined && monitor.source_name === "JobSpy");
}

/** A board id as its label ("zip_recruiter" -> "ZipRecruiter"), using the job-sources list when known. */
export function boardLabel(id: string, boards: readonly JobSpyBoard[] = []): string {
  return boards.find((b) => b.id === id)?.label ?? BOARD_FALLBACK[id] ?? id;
}

/** The JobSpy settings a monitor stores in `filters`, ready to display. */
export function jobSpySummary(filters: unknown, boards: readonly JobSpyBoard[] = []): JobSpySummary {
  const f = (filters && typeof filters === "object" ? filters : {}) as Record<string, unknown>;
  const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => String(x ?? "").trim()).filter(Boolean) : []);
  const results = typeof f.results_wanted === "number" ? f.results_wanted : Number(f.results_wanted);
  return {
    boards: list(f.boards).map((b) => boardLabel(b, boards)),
    terms: list(f.search_terms),
    location: typeof f.location === "string" ? f.location : "",
    freshness: freshnessLabel(f.hours_old),
    results: Number.isFinite(results) && results > 0 ? `${results} per search` : "",
  };
}

// --- relevance -------------------------------------------------------------------------------

export const RELEVANCE_CLASSES = ["HIGH", "REVIEW", "REJECT"] as const;
export type RelevanceClass = (typeof RELEVANCE_CLASSES)[number];

/** HIGH / REVIEW / REJECT (upper-cased), or null for anything else. */
export function relevanceClass(value: unknown): RelevanceClass | null {
  const text = typeof value === "string" ? value.trim().toUpperCase() : "";
  return (RELEVANCE_CLASSES as readonly string[]).includes(text) ? (text as RelevanceClass) : null;
}

/** The platform badge tone for a relevance class: HIGH green, REVIEW amber, REJECT gray (null: no badge). */
export function relevanceTone(value: unknown): "completed" | "cancelled" | "queued" | null {
  const cls = relevanceClass(value);
  return cls === "HIGH" ? "completed" : cls === "REVIEW" ? "cancelled" : cls === "REJECT" ? "queued" : null;
}

/** A 0–100 score as text ("87", "" when missing). */
export function relevanceScoreText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "";
  return String(Math.round(Math.max(0, Math.min(100, n))));
}

/** "JobSpy · Indeed" when the job came from a board; just the source otherwise. */
export function sourceWithBoard(source: unknown, board: unknown): string {
  const s = typeof source === "string" ? source.trim() : "";
  const b = typeof board === "string" ? board.trim() : "";
  if (!b) return s;
  if (!s) return b;
  return s.toLowerCase() === b.toLowerCase() ? s : `${s} · ${b}`;
}

/** The `relevance` filter as a set of classes, and back (stable HIGH,REVIEW,REJECT order). */
export function parseRelevanceParam(value: string | undefined | null): RelevanceClass[] {
  if (!value) return [];
  const picked = new Set(value.split(",").map(relevanceClass).filter((c): c is RelevanceClass => c !== null));
  return RELEVANCE_CLASSES.filter((c) => picked.has(c));
}

export function toggleRelevance(value: string | undefined | null, cls: RelevanceClass): string {
  const current = parseRelevanceParam(value);
  const next = current.includes(cls) ? current.filter((c) => c !== cls) : [...current, cls];
  return RELEVANCE_CLASSES.filter((c) => next.includes(c)).join(",");
}
