// Small, dependency-free charts for summaries: horizontal bars and a 30-day sparkline.

export function Bars({ data, limit = 10, empty = "No data yet." }: { data: Record<string, number>; limit?: number; empty?: string }) {
  const entries = Object.entries(data)
    .filter(([, v]) => typeof v === "number" && v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (entries.length === 0) return <p className="muted small">{empty}</p>;
  return (
    <ul className="bars">
      {entries.map(([label, value]) => (
        <li key={label} className="bars__row">
          <span className="bars__label">{label.replace(/_/g, " ").toLowerCase()}</span>
          <span className="bars__track">
            <span className="bars__fill" style={{ width: `${(value / max) * 100}%` }} />
          </span>
          <span className="bars__value tabular">{value.toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}

export function Sparkline({ points, label }: { points: { date: string; count: number }[]; label: string }) {
  if (!points.length) return null;
  const max = Math.max(1, ...points.map((p) => p.count));
  const w = 120;
  const h = 32;
  const step = points.length > 1 ? w / (points.length - 1) : w;
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - 2 - (p.count / max) * (h - 4)).toFixed(1)}`).join(" ");
  const total = points.reduce((sum, p) => sum + p.count, 0);
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} width={w} height={h} role="img" aria-label={`${label}: ${total} in the last ${points.length} days`}>
      <path d={path} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/** Pick a numeric map out of an analytics section (by_type, by_status…). */
export function counts(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([, v]) => typeof v === "number")) as Record<string, number>;
}

/** Read a nested number, 0 when missing. */
export function num(value: unknown, ...path: string[]): number {
  let current: unknown = value;
  for (const key of path) current = current && typeof current === "object" ? (current as Record<string, unknown>)[key] : undefined;
  return typeof current === "number" ? current : 0;
}
