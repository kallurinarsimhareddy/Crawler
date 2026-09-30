// User invitations: pure helpers for the Invite User form, the Invitations tab and
// the /invite page (covered by tests/invitations.test.ts). The server enforces
// every rule; these only shape what the UI offers and shows.

/** "<workspace uuid>.<url-safe secret>" as the API issues it. */
const TOKEN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{20,}$/i;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isInviteToken(value: string | null | undefined): boolean {
  return !!value && value.length <= 200 && TOKEN.test(value);
}

/**
 * The invite link for a token. The token rides in the fragment (#), which browsers
 * never send to a server, so it stays out of access logs, analytics and Referer headers.
 */
export function inviteLink(origin: string, token: string): string {
  return `${origin.replace(/\/+$/, "")}/invite#${token}`;
}

/** The token on the invite page: from `/invite#<token>` or the older `/invite/<token>` form. */
export function tokenFromLocation(pathname: string, hash: string): string | null {
  const fromHash = decodeURIComponent(hash.replace(/^#/, "").trim());
  if (isInviteToken(fromHash)) return fromHash;
  const match = /^\/invite\/([^/?#]+)\/?$/.exec(pathname);
  const fromPath = match ? decodeURIComponent(match[1]) : "";
  return isInviteToken(fromPath) ? fromPath : null;
}

/** A pasted invite link or bare code -> the token, or null. */
export function extractToken(text: string): string | null {
  const value = text.trim();
  if (isInviteToken(value)) return value;
  const hash = value.indexOf("#");
  if (hash >= 0 && isInviteToken(value.slice(hash + 1))) return value.slice(hash + 1);
  const path = /\/invite\/([^/?#\s]+)/.exec(value);
  return path && isInviteToken(decodeURIComponent(path[1])) ? decodeURIComponent(path[1]) : null;
}

export interface InviteForm {
  email: string;
  first_name: string;
  last_name: string;
  role: string;
  team_id: string;
}

export const EMPTY_INVITE: InviteForm = { email: "", first_name: "", last_name: "", role: "member", team_id: "" };

/** The first problem with the form, or null when it can be sent. ``roles``: the assignable roles. */
export function validateInvite(form: InviteForm, roles: readonly string[]): string | null {
  const email = form.email.trim();
  if (!email) return "Enter the invitee's email address.";
  if (!EMAIL.test(email) || email.length > 320) return "That email address does not look right.";
  if (!roles.includes(form.role)) return "Choose a role.";
  if (form.first_name.trim().length > 100 || form.last_name.trim().length > 100) return "Names can be 100 characters at most.";
  return null;
}

/** The request body: trimmed, lower-cased email, blanks left out. */
export function inviteBody(form: InviteForm): Record<string, string> {
  const body: Record<string, string> = { email: form.email.trim().toLowerCase(), role: form.role };
  if (form.first_name.trim()) body.first_name = form.first_name.trim();
  if (form.last_name.trim()) body.last_name = form.last_name.trim();
  if (form.team_id) body.team_id = form.team_id;
  return body;
}

export type InviteStatus = "pending" | "accepted" | "expired" | "revoked";

export const STATUS_LABELS: Record<InviteStatus, string> = {
  pending: "Pending",
  accepted: "Accepted",
  expired: "Expired",
  revoked: "Revoked",
};

export function statusLabel(status: unknown): string {
  return STATUS_LABELS[status as InviteStatus] ?? String(status ?? "—");
}

/** Which row actions an admin gets for an invitation in this status. */
export function invitationActions(status: unknown): { resend: boolean; revoke: boolean; copy: boolean } {
  return {
    resend: status === "pending" || status === "expired",
    revoke: status === "pending" || status === "expired",
    copy: status === "pending",
  };
}

export function fullName(first: unknown, last: unknown): string {
  return [first, last].filter((p) => typeof p === "string" && p.trim()).join(" ");
}

/** How a member is named in lists: their name, else their email, else a short id. */
export function memberLabel(m: { name?: string | null; email?: string | null; user_id: string }): string {
  return m.name || m.email || `${m.user_id.slice(0, 8)}…`;
}

export function sameEmail(a: string | null | undefined, b: string | null | undefined): boolean {
  return !!a && !!b && a.trim().toLowerCase() === b.trim().toLowerCase();
}

// --- the invitation waiting for sign-in -------------------------------------------------
// Kept in this browser while the invitee signs in or confirms a new account (the
// confirmation email lands on the home page, not /invite), removed once used.

const PENDING_KEY = "sana.pending-invite";

interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function browserStorage(): KeyValueStore | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

interface Pending {
  token: string;
  /** The invited address, once the invite page has looked it up. */
  email: string | null;
}

function readPending(storage: KeyValueStore | null): Pending | null {
  try {
    const raw = storage?.getItem(PENDING_KEY) ?? null;
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Pending>;
    return isInviteToken(value.token) ? { token: value.token as string, email: typeof value.email === "string" ? value.email : null } : null;
  } catch {
    return null;
  }
}

export function savePendingInvite(token: string, email: string | null = null, storage: KeyValueStore | null = browserStorage()): void {
  try {
    if (isInviteToken(token)) storage?.setItem(PENDING_KEY, JSON.stringify({ token, email }));
  } catch {
    // blocked storage: the invitee just opens the link again after signing in
  }
}

export function pendingInvite(storage: KeyValueStore | null = browserStorage()): string | null {
  return readPending(storage)?.token ?? null;
}

/**
 * The pending invitation to finish for this signed-in address, or null. Only the invited
 * account is sent back to /invite, so someone signed in as another account (an admin
 * testing their own link, say) is never trapped there.
 */
export function pendingInviteFor(email: string | null | undefined, storage: KeyValueStore | null = browserStorage()): string | null {
  const pending = readPending(storage);
  return pending && sameEmail(pending.email, email) ? pending.token : null;
}

export function clearPendingInvite(storage: KeyValueStore | null = browserStorage()): void {
  try {
    storage?.removeItem(PENDING_KEY);
  } catch {
    // nothing stored
  }
}
