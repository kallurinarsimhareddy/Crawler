// Users & Permissions / Audit Log helpers (pure; covered by tests/admin.test.ts).
// The server is the authority on rights; these only decide what the UI offers.

export const ROLE_LABELS: Record<string, string> = {
  owner: "Owner",
  admin: "Admin",
  manager: "Manager",
  member: "User",
  viewer: "Read-only",
};

/** Roles an admin can give someone (the owner is never assignable). */
export const ASSIGNABLE_ROLES = ["admin", "manager", "member", "viewer"] as const;

export function roleLabel(role: string | undefined | null): string {
  if (!role) return "—";
  return ROLE_LABELS[role] ?? role;
}

export function canWrite(role: string | undefined | null): boolean {
  return role === "owner" || role === "admin" || role === "manager" || role === "member";
}

export function canManage(role: string | undefined | null): boolean {
  return role === "owner" || role === "admin" || role === "manager";
}

export function canAdmin(role: string | undefined | null): boolean {
  return role === "owner" || role === "admin";
}

/** Split pasted ids (commas, spaces, new lines), dropping blanks and duplicates. */
export function parseIds(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[\s,;]+/)) {
    const id = part.trim();
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

/** The query for /audit/search and /audit/export.csv: only filled filters, dates validated. */
export function auditQuery(values: Record<string, string>, search = ""): { query: Record<string, string>; error: string | null } {
  const query: Record<string, string> = {};
  for (const key of ["action", "actor", "entity_type", "entity_id", "actor_kind", "from", "to"]) {
    const value = (values[key] ?? "").trim();
    if (value) query[key] = value;
  }
  if (search.trim()) query.q = search.trim();
  for (const key of ["from", "to"]) {
    if (query[key] && !/^\d{4}-\d{2}-\d{2}$/.test(query[key])) return { query, error: `${key === "from" ? "From" : "To"} must be a date like 2026-09-30` };
  }
  if (query.from && query.to && query.from > query.to) return { query, error: "From is after To" };
  return { query, error: null };
}

/** "a=1&b=2" for a download URL (the API client adds no query to downloads). */
export function toSearch(query: Record<string, string>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) params.set(key, value);
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** One line per changed field for the audit detail drawer. */
export function describeChanges(changes: unknown): string[] {
  if (!changes || typeof changes !== "object") return [];
  return Object.entries(changes as Record<string, unknown>).map(([key, value]) => {
    const text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);
    return `${key}: ${text.length > 200 ? `${text.slice(0, 200)}…` : text}`;
  });
}

/** Comma separated events for an integration form -> list. */
export function parseEvents(text: string): string[] {
  return text.split(",").map((e) => e.trim()).filter(Boolean);
}
