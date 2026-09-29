// Pure helpers for analytics reports and score explanations (covered by `npm test`).

export interface ReportColumn {
  key: string;
  label: string;
}

export interface ScoreFactor {
  name: string;
  weight: number;
  value: unknown;
  points: number;
  reason: string;
}

const PERCENT_KEYS = ["rate", "share", "conversion"];

/** A report cell: ratios as percentages, missing values as "—" (never a fake 0). */
export function formatCell(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") {
    if (PERCENT_KEYS.some((p) => key.includes(p))) return `${(value * 100).toFixed(1)}%`;
    if (key.includes("value") || key.includes("cost")) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  }
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** Inclusive [start, end] for a preset, as YYYY-MM-DD, relative to `today`. */
export function presetRange(preset: string, today: Date = new Date()): { start: string; end: string } {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  const days: Record<string, number> = { "7d": 7, "30d": 30, "90d": 90, "365d": 365 };
  if (preset === "qtd") {
    start.setUTCMonth(Math.floor(end.getUTCMonth() / 3) * 3, 1);
  } else if (preset === "ytd") {
    start.setUTCMonth(0, 1);
  } else {
    start.setUTCDate(end.getUTCDate() - ((days[preset] ?? 30) - 1));
  }
  return { start: iso(start), end: iso(end) };
}

export const PRESETS: { key: string; label: string }[] = [
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "90d", label: "Last 90 days" },
  { key: "qtd", label: "Quarter to date" },
  { key: "ytd", label: "Year to date" },
  { key: "365d", label: "Last 12 months" },
];

/** Query parameters for a report request: range plus non-empty filters. */
export function reportQuery(start: string, end: string, filters: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  if (start) out.start = start;
  if (end) out.end = end;
  for (const [key, value] of Object.entries(filters)) if (value && value.trim()) out[key] = value.trim();
  return out;
}

/** Check a factor list adds up to the score (the explanation is complete). */
export function factorsTotal(factors: ScoreFactor[]): number {
  return Math.min(100, Math.round(factors.reduce((sum, f) => sum + (Number(f.points) || 0), 0) * 10) / 10);
}

/** Share of a factor's weight it earned, 0..100, for the bar in the explanation. */
export function factorFill(factor: ScoreFactor): number {
  if (!factor.weight) return 0;
  return Math.max(0, Math.min(100, (factor.points / factor.weight) * 100));
}

/** Points for a line chart in a w×h box; x is the index, y scaled to the global max. */
export function linePath(values: number[], width: number, height: number, max: number): string {
  if (!values.length) return "";
  const top = Math.max(1, max);
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  return values
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(height - 2 - (v / top) * (height - 4)).toFixed(1)}`)
    .join(" ");
}

export const BUYING_STAGE_ORDER = ["unaware", "problem_aware", "researching", "engaged", "evaluating", "customer"];

/** 1-based position of a buying stage, 0 when unknown. */
export function stagePosition(label: unknown): number {
  return BUYING_STAGE_ORDER.indexOf(String(label ?? "")) + 1;
}
