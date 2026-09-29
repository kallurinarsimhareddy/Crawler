// Pure helpers for Email & Sending, campaigns, sequences and suppression pages.
// No JSX and no imports: `npm test` runs this file directly under Node.

/** Step delays (days after the previous step) -> the day each step runs; Day 1 is the first. */
export function cadenceDays(delays: number[]): number[] {
  const out: number[] = [];
  let day = 1;
  delays.forEach((delay, i) => {
    day = i === 0 ? 1 + Math.max(0, delay) : day + Math.max(0, delay);
    out.push(day);
  });
  return out;
}

/** The inverse: days (1, 3, 6, 10) -> delays (0, 2, 3, 4). */
export function delaysFromDays(days: number[]): number[] {
  return days.map((day, i) => (i === 0 ? Math.max(0, day - 1) : Math.max(0, day - days[i - 1])));
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DOMAIN = /^@?([a-z0-9-]+\.)+[a-z]{2,}$/i;

/** Split pasted text (lines, commas, semicolons) into suppression values, de-duplicated. */
export function parseBulkValues(text: string): { values: string[]; invalid: string[] } {
  const seen = new Set<string>();
  const values: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\n,;]+/)) {
    const value = raw.trim().toLowerCase();
    if (!value) continue;
    if (!EMAIL.test(value) && !DOMAIN.test(value)) {
      invalid.push(raw.trim());
      continue;
    }
    if (seen.has(value)) continue;
    seen.add(value);
    values.push(value);
  }
  return { values, invalid };
}

export interface ProviderInfo {
  provider: string;
  configured: boolean;
  missing: string[];
  auth: string;
}

/** What a provider card says: never "connected" unless the server can actually offer it. */
export function providerState(p: ProviderInfo, connectedCount: number): { label: string; tone: "ok" | "warn" | "off"; detail: string } {
  if (!p.configured) {
    return { label: "Not configured", tone: "off", detail: p.missing.length ? `Server needs ${p.missing.join(", ")}` : "Not available on this server" };
  }
  if (connectedCount > 0) return { label: `${connectedCount} connected`, tone: "ok", detail: "Ready to send when sending is allowed" };
  return { label: "Available", tone: "warn", detail: p.auth === "oauth" ? "Connect a mailbox with OAuth" : "Add a sender" };
}

/** "12.5%" for a ratio; "—" when the provider does not report it or nothing was sent. */
export function ratio(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

const DAY_NAMES = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export interface Schedule {
  timezone?: string;
  days?: number[];
  start_hour?: number;
  end_hour?: number;
  daily_cap?: number;
}

/** "Mon–Fri 9:00–17:00 America/New_York · 100/day", or "Any time". */
export function scheduleSummary(s: Schedule | null | undefined): string {
  if (!s || Object.keys(s).length === 0) return "Any time";
  const days = (s.days ?? []).slice().sort((a, b) => a - b);
  let dayText = "Every day";
  if (days.length && days.length < 7) {
    const contiguous = days.every((d, i) => i === 0 || d === days[i - 1] + 1);
    dayText = contiguous && days.length > 2 ? `${DAY_NAMES[days[0]]}–${DAY_NAMES[days[days.length - 1]]}` : days.map((d) => DAY_NAMES[d]).join(", ");
  }
  const hours = s.start_hour !== undefined || s.end_hour !== undefined ? ` ${s.start_hour ?? 0}:00–${s.end_hour ?? 24}:00` : "";
  const tz = s.timezone ? ` ${s.timezone}` : "";
  const cap = s.daily_cap ? ` · ${s.daily_cap}/day` : "";
  return `${dayText}${hours}${tz}${cap}`;
}

export interface EditableStep {
  channel: string;
  delay_days: number;
  step_type: string;
  template_id?: string | null;
}

/** Problems that would make the server refuse a step list, found before saving. */
export function validateSteps(steps: EditableStep[]): string[] {
  const problems: string[] = [];
  if (steps.length === 0) problems.push("Add at least one step.");
  if (steps.length > 30) problems.push("A sequence can have at most 30 steps.");
  steps.forEach((step, i) => {
    if (step.channel === "email" && !step.template_id) problems.push(`Step ${i + 1}: choose a template.`);
    if (!(step.delay_days >= 0 && step.delay_days <= 365)) problems.push(`Step ${i + 1}: delay must be 0–365 days.`);
  });
  return problems;
}

/** Move a step up (-1) or down (+1); returns a new array. */
export function moveStep<T>(steps: T[], index: number, direction: -1 | 1): T[] {
  const target = index + direction;
  if (target < 0 || target >= steps.length) return steps;
  const next = steps.slice();
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** Suppression reasons that only a workspace admin may remove. */
export const PROTECTED_REASONS = ["unsubscribe", "complaint", "hard_bounce", "bounce", "legal"];
