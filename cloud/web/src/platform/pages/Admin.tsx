// Settings → Users & Permissions, Audit Log, Integrations, and the Notifications page.
// Every action is also enforced by the API (and RLS); the UI only hides what a
// role cannot do so read-only members are not offered dead buttons.

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { request } from "../../api/client";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { copyText } from "../../lib/clipboard";
import { Icon } from "../../shell/Icon";
import type { PageOf, Row } from "../api";
import { ASSIGNABLE_ROLES, auditQuery, canAdmin, canManage, canWrite, describeChanges, parseEvents, parseIds, roleLabel, toSearch } from "../logic/admin";
import { EMPTY_INVITE, extractToken, fullName, invitationActions, inviteBody, inviteLink, memberLabel, statusLabel, validateInvite, type InviteForm } from "../logic/invitations";
import { DataTable, FilterBar, KeyValues, PageHeader, Pill, Tabs, fmt, fmtDate, useAction, useLoad, type Loaded } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import "../styles/admin.css";

function useRole(): string {
  return useWorkspace().current?.role ?? "viewer";
}

function ReadOnlyNote({ need }: { need: string }) {
  return (
    <p className="alert alert--info admin-note" role="note">
      <Icon name="shield" size={16} /> You can view this page. {need} can make changes.
    </p>
  );
}

// --- Users & Permissions -------------------------------------------------------------

interface Member {
  user_id: string;
  role: string;
  role_label: string;
  email: string | null;
  name: string | null;
  teams: string[];
  team_ids: string[];
  status: string;
  joined_at: string | null;
  is_owner: boolean;
  is_you: boolean;
}

interface Overview {
  you: { role: string; role_label: string; permissions: Record<string, boolean> };
  roles: { role: string; label: string }[];
  permissions: { key: string; label: string; group: string; roles: string[] }[];
}

type Team = Row & { name: string; members: { user_id: string; role: string }[] };

/** What the API returns after creating, resending or re-linking an invitation. */
interface Issued extends Row {
  email: string;
  token: string;
  invite_url: string | null;
  email_sent: boolean;
  delivery_message: string;
}

function issuedLink(issued: Issued): string {
  return issued.invite_url ?? inviteLink(window.location.origin, issued.token);
}

/** A modal dialog: scrim, Escape to close, focus moved inside. */
function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    (panel.current?.querySelector<HTMLElement>("input, select, textarea") ?? panel.current?.querySelector<HTMLElement>("button"))?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="admin-dialog" role="dialog" aria-modal="true" aria-label={title}>
      <button type="button" className="admin-dialog__scrim" aria-label="Close" onClick={onClose} />
      <div className="admin-dialog__panel card" ref={panel}>
        <div className="admin-dialog__head">
          <h2>{title}</h2>
          <button type="button" className="button button--ghost button--small" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** The invite link with a copy button; says plainly whether an email went out. */
function IssuedLink({ issued }: { issued: Issued }) {
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<Error | null>(null);
  const link = issuedLink(issued);
  return (
    <div className="admin-issued" role="status">
      {issued.email_sent ? (
        <p className="alert alert--info">{issued.delivery_message}</p>
      ) : (
        <p className="alert alert--warning">{issued.delivery_message}</p>
      )}
      <label className="field">
        <span className="field__label">Invite link for {issued.email}</span>
        <input className="input mono" readOnly value={link} onFocus={(e) => e.currentTarget.select()} />
      </label>
      <p className="muted small">The link works once, only for {issued.email}, and expires on {fmtDate(issued.expires_at)}. It is shown only now; copying a new link later replaces it.</p>
      <div className="actions">
        <button
          type="button"
          className="button button--primary button--small"
          onClick={() => {
            setCopyError(null);
            copyText(link).then(() => setCopied("Invite link copied."), (err: Error) => setCopyError(err));
          }}
        >
          Copy Invite Link
        </button>
        {copied && <span className="muted small">{copied}</span>}
      </div>
      {copyError && <ErrorBanner error={copyError} />}
    </div>
  );
}

function InviteDialog({ teams, onClose, onInvited }: { teams: Team[]; onClose: () => void; onInvited: () => void }) {
  const client = useWs();
  const action = useAction();
  const [form, setForm] = useState<InviteForm>(EMPTY_INVITE);
  const [problem, setProblem] = useState<string | null>(null);
  const [issued, setIssued] = useState<Issued | null>(null);
  const set = (key: keyof InviteForm) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const invalid = validateInvite(form, ASSIGNABLE_ROLES);
    setProblem(invalid);
    if (invalid) return;
    const result = await action.run(() => client.post<Issued>("/admin/invitations", inviteBody(form)));
    if (result) {
      setIssued(result);
      onInvited();
    }
  };

  return (
    <Dialog title={issued ? "Invitation created" : "Invite user"} onClose={onClose}>
      {issued ? (
        <>
          <IssuedLink issued={issued} />
          <div className="form__actions">
            <button type="button" className="button button--ghost" onClick={() => { setIssued(null); setForm(EMPTY_INVITE); }}>Invite another</button>
            <button type="button" className="button button--primary" onClick={onClose}>Done</button>
          </div>
        </>
      ) : (
        <form className="form" onSubmit={submit} noValidate>
          <label className="field">
            <span className="field__label">Email *</span>
            <input className="input" type="email" autoComplete="off" required value={form.email} onChange={set("email")} placeholder="teammate@company.com" />
          </label>
          <div className="form-grid">
            <label className="field">
              <span className="field__label">First name</span>
              <input className="input" autoComplete="off" maxLength={100} value={form.first_name} onChange={set("first_name")} />
            </label>
            <label className="field">
              <span className="field__label">Last name</span>
              <input className="input" autoComplete="off" maxLength={100} value={form.last_name} onChange={set("last_name")} />
            </label>
          </div>
          <div className="form-grid">
            <label className="field">
              <span className="field__label">Role *</span>
              <select className="input" required value={form.role} onChange={set("role")}>
                {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </select>
            </label>
            <label className="field">
              <span className="field__label">Team (optional)</span>
              <select className="input" value={form.team_id} onChange={set("team_id")}>
                <option value="">No team</option>
                {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </label>
          </div>
          <p className="muted small">They join with this role after signing in, or creating an account, as this email address. The invitation expires in 7 days.</p>
          {problem && <p className="alert alert--error" role="alert">{problem}</p>}
          {action.error && <ErrorBanner error={action.error} />}
          <div className="form__actions">
            <button type="button" className="button button--ghost" onClick={onClose}>Cancel</button>
            <button className="button button--primary" disabled={action.busy}>{action.busy ? "Sending…" : "Send Invitation"}</button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function TeamsDialog({ member, teams, onClose, onSaved }: { member: Member; teams: Team[]; onClose: () => void; onSaved: () => void }) {
  const client = useWs();
  const action = useAction();
  const [picked, setPicked] = useState<string[]>(member.team_ids);
  const toggle = (id: string) => setPicked(picked.includes(id) ? picked.filter((t) => t !== id) : [...picked, id]);
  return (
    <Dialog title={`Teams for ${memberLabel(member)}`} onClose={onClose}>
      {teams.length === 0 ? (
        <EmptyState icon="users" title="No teams yet" description="Create a team on the Teams tab first." />
      ) : (
        <fieldset className="admin-checks">
          <legend className="sr-only">Teams</legend>
          {teams.map((t) => (
            <label key={t.id} className="admin-check">
              <input type="checkbox" checked={picked.includes(t.id)} onChange={() => toggle(t.id)} /> {t.name}
            </label>
          ))}
        </fieldset>
      )}
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button type="button" className="button button--ghost" onClick={onClose}>Cancel</button>
        <button
          type="button"
          className="button button--primary"
          disabled={action.busy || teams.length === 0}
          onClick={() => void action.run(async () => {
            await client.patch(`/admin/members/${member.user_id}/teams`, { team_ids: picked });
            onSaved();
            onClose();
          })}
        >
          Save teams
        </button>
      </div>
    </Dialog>
  );
}

function MembersTab({ members, teams, reload }: { members: Member[]; teams: Team[]; reload: () => void }) {
  const client = useWs();
  const role = useRole();
  const action = useAction();
  const admin = canAdmin(role);
  const manage = canManage(role);
  const [editing, setEditing] = useState<Member | null>(null);
  return (
    <div className="card">
      {!admin && <ReadOnlyNote need="Workspace admins" />}
      {action.error && <ErrorBanner error={action.error} />}
      <DataTable
        rows={members.map((m) => ({ ...m, id: m.user_id }))}
        empty={{ title: "No members", description: "Invite teammates with Invite User.", icon: "users" }}
        columns={[
          { key: "name", label: "Name", render: (m) => <span className="admin-name">{m.name ?? (m.email ? m.email.split("@")[0] : <span className="muted">—</span>)}{m.is_you && <span className="chip admin-you">you</span>}</span> },
          { key: "email", label: "Email", render: (m) => m.email ? <span className="admin-email">{m.email}</span> : <span className="muted mono small" title={m.user_id}>{m.user_id.slice(0, 8)}…</span> },
          {
            key: "role",
            label: "Role",
            render: (m) =>
              admin && !m.is_owner ? (
                <select
                  className="input input--small"
                  aria-label={`Role for ${memberLabel(m)}`}
                  value={m.role}
                  disabled={action.busy}
                  onChange={(e) => void action.run(async () => { await client.patch(`/admin/members/${m.user_id}/role`, { role: e.target.value }); reload(); })}
                >
                  {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                </select>
              ) : (
                <Pill value={m.role_label} />
              ),
          },
          { key: "teams", label: "Teams", render: (m) => (m.teams.length ? m.teams.join(", ") : <span className="muted">—</span>) },
          { key: "status", label: "Status", render: () => <Pill value="Active" /> },
          { key: "joined_at", label: "Joined", render: (m) => (m.joined_at ? fmtDate(m.joined_at) : <span className="muted">—</span>) },
          {
            key: "actions",
            label: "Actions",
            render: (m) => (
              <span className="actions admin-row-actions">
                {manage && <button type="button" className="button button--ghost button--small" onClick={() => setEditing(m)}>Assign team</button>}
                {admin && !m.is_owner && !m.is_you && (
                  <button
                    type="button"
                    className="button button--ghost button--small"
                    disabled={action.busy}
                    onClick={() => {
                      if (!window.confirm(`Remove ${memberLabel(m)} from this workspace? They lose access immediately.`)) return;
                      void action.run(async () => { await client.del(`/admin/members/${m.user_id}`); reload(); });
                    }}
                  >
                    Remove
                  </button>
                )}
                {m.is_owner && <span className="muted small">Owner</span>}
              </span>
            ),
          },
        ]}
      />
      {editing && <TeamsDialog member={editing} teams={teams} onClose={() => setEditing(null)} onSaved={reload} />}
    </div>
  );
}

function InvitationsTab({ reloadKey }: { reloadKey: number }) {
  const client = useWs();
  const admin = canAdmin(useRole());
  const list = useLoad(
    (signal) => (admin ? client.get<{ items: Row[] }>("/admin/invitations", undefined, signal) : Promise.resolve({ items: [] as Row[] })),
    `${client.base}/inv/${reloadKey}/${admin}`,
  );
  const action = useAction();
  const [issued, setIssued] = useState<Issued | null>(null);
  if (!admin) return <div className="card"><ReadOnlyNote need="Workspace admins" /><p className="muted small">Invitations are visible to workspace admins only.</p></div>;

  const reissue = async (path: string) => {
    const result = await action.run(() => client.post<Issued>(path));
    if (result) {
      setIssued(result);
      list.refresh();
    }
  };

  return (
    <div className="stack">
      {issued && (
        <div className="card">
          <IssuedLink issued={issued} />
          <div className="form__actions"><button type="button" className="button button--ghost button--small" onClick={() => setIssued(null)}>Done</button></div>
        </div>
      )}
      {action.error && <ErrorBanner error={action.error} />}
      <div className="card">
        {list.error && <ErrorBanner error={list.error} onRetry={list.refresh} />}
        {list.loading && !list.data ? <Loading /> : (
          <DataTable
            rows={list.data?.items ?? []}
            empty={{ title: "No invitations", description: "Use Invite User to add teammates; they join with the role you choose.", icon: "users" }}
            columns={[
              { key: "email", label: "Email", render: (r) => <span><span className="admin-email">{String(r.email)}</span>{fullName(r.first_name, r.last_name) && <span className="muted small block">{fullName(r.first_name, r.last_name)}</span>}</span> },
              { key: "role", label: "Role", render: (r) => roleLabel(String(r.role)) },
              { key: "team_name", label: "Team", render: (r) => (r.team_name ? String(r.team_name) : <span className="muted">—</span>) },
              { key: "status", label: "Status", render: (r) => <Pill value={statusLabel(r.status)} /> },
              { key: "invited_by_label", label: "Invited by", render: (r) => (r.invited_by_label ? String(r.invited_by_label) : <span className="muted">—</span>) },
              { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
              { key: "expires_at", label: "Expires", render: (r) => (r.status === "pending" || r.status === "expired" ? fmtDate(r.expires_at) : <span className="muted">—</span>) },
              {
                key: "actions",
                label: "Actions",
                render: (r) => {
                  const can = invitationActions(r.status);
                  return (
                    <span className="actions admin-row-actions">
                      {can.resend && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void reissue(`/admin/invitations/${r.id}/resend`)}>Resend</button>}
                      {can.copy && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void reissue(`/admin/invitations/${r.id}/link`)}>Copy Invite Link</button>}
                      {can.revoke && (
                        <button
                          type="button"
                          className="button button--ghost button--small"
                          disabled={action.busy}
                          onClick={() => {
                            if (!window.confirm(`Revoke the invitation for ${String(r.email)}? The link stops working.`)) return;
                            void action.run(async () => { await client.post(`/admin/invitations/${r.id}/revoke`); setIssued(null); list.refresh(); });
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </span>
                  );
                },
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}

/** For someone holding an invitation link or code: redeem it into the workspace it names. */
function JoinWorkspace() {
  const { reload, select } = useWorkspace();
  const action = useAction();
  const [text, setText] = useState("");
  const [joined, setJoined] = useState<string | null>(null);
  const token = extractToken(text);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token) return;
    const result = await action.run(() =>
      request<{ workspace_id: string; workspace_name: string; role: string }>("/api/v1/invitations/accept", { method: "POST", body: JSON.stringify({ token }) }),
    );
    if (result) {
      setJoined(`Joined ${result.workspace_name} as ${roleLabel(result.role)}.`);
      setText("");
      reload();
      select(result.workspace_id);
    }
  };
  return (
    <form className="card form admin-inline" onSubmit={submit}>
      <label className="field admin-grow">
        <span className="field__label">Have an invitation link for another workspace?</span>
        <input className="input mono" value={text} onChange={(e) => setText(e.target.value)} placeholder="paste the invite link" autoComplete="off" />
      </label>
      <div className="form__actions"><button className="button button--ghost" disabled={action.busy || !token}>Join</button></div>
      {action.error && <ErrorBanner error={action.error} />}
      {joined && <p className="muted small">{joined}</p>}
    </form>
  );
}

function TeamsTab({ members, teams, reloadTeams }: { members: Member[]; teams: Loaded<{ items: Team[] }>; reloadTeams: () => void }) {
  const client = useWs();
  const manage = canManage(useRole());
  const action = useAction();
  const [name, setName] = useState("");
  const [pick, setPick] = useState<Record<string, string>>({});
  const byId = Object.fromEntries(members.map((m) => [m.user_id, m]));
  const run = (fn: () => Promise<unknown>) => void action.run(async () => { await fn(); reloadTeams(); });

  return (
    <div className="stack">
      {manage ? (
        <form className="card form admin-inline" onSubmit={(e) => { e.preventDefault(); run(async () => { await client.post("/admin/teams", { name }); setName(""); }); }}>
          <label className="field admin-grow">
            <span className="field__label">New team</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Enterprise SDRs" />
          </label>
          <div className="form__actions"><button className="button button--primary" disabled={action.busy || !name.trim()}>Create team</button></div>
        </form>
      ) : (
        <ReadOnlyNote need="Managers and admins" />
      )}
      {action.error && <ErrorBanner error={action.error} />}
      {teams.error && <ErrorBanner error={teams.error} onRetry={teams.refresh} />}
      {teams.loading && !teams.data ? <Loading /> : (teams.data?.items ?? []).length === 0 ? (
        <div className="card"><EmptyState icon="users" title="No teams yet" description="Group members into teams to organise ownership and reporting." /></div>
      ) : (
        <div className="admin-cards">
          {(teams.data?.items ?? []).map((team) => (
            <div key={team.id} className="card">
              <div className="admin-card__head">
                <h3>{String(team.name)}</h3>
                {manage && <button type="button" className="button button--ghost button--small" onClick={() => window.confirm(`Delete team ${String(team.name)}?`) && run(() => client.del(`/admin/teams/${team.id}`))}>Delete</button>}
              </div>
              {team.members.length === 0 ? <p className="muted small">No members yet.</p> : (
                <ul className="admin-list">
                  {team.members.map((tm) => (
                    <li key={tm.user_id}>
                      <span>{byId[tm.user_id] ? memberLabel(byId[tm.user_id]) : `${tm.user_id.slice(0, 8)}…`}{tm.role === "lead" && <span className="chip admin-you">lead</span>}</span>
                      {manage && <button type="button" className="button button--ghost button--small" onClick={() => run(() => client.del(`/admin/teams/${team.id}/members/${tm.user_id}`))}>Remove</button>}
                    </li>
                  ))}
                </ul>
              )}
              {manage && (
                <div className="admin-inline">
                  <select className="input input--small" aria-label="Add member" value={pick[team.id] ?? ""} onChange={(e) => setPick({ ...pick, [team.id]: e.target.value })}>
                    <option value="">Add a member…</option>
                    {members.filter((m) => !team.members.some((tm) => tm.user_id === m.user_id)).map((m) => <option key={m.user_id} value={m.user_id}>{memberLabel(m)}</option>)}
                  </select>
                  <button type="button" className="button button--ghost button--small" disabled={!pick[team.id]} onClick={() => run(async () => { await client.post(`/admin/teams/${team.id}/members`, { user_id: pick[team.id] }); setPick({ ...pick, [team.id]: "" }); })}>Add</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AssignmentTab({ members }: { members: Member[] }) {
  const client = useWs();
  const manage = canManage(useRole());
  const action = useAction();
  const [entity, setEntity] = useState("companies");
  const [ids, setIds] = useState("");
  const [owner, setOwner] = useState("");
  const [result, setResult] = useState<{ updated: number; missing: string[] } | null>(null);
  if (!manage) return <div className="card"><ReadOnlyNote need="Managers and admins" /></div>;
  const parsed = parseIds(ids);
  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        const out = await action.run(() => client.post<{ updated: number; missing: string[] }>("/admin/assign", { entity, ids: parsed, owner_id: owner || null }));
        if (out) setResult(out);
      }}
    >
      <p className="muted small">Set the owner of records in bulk. Paste record ids (from an export or a list); the new owner is notified.</p>
      <div className="form-grid">
        <label className="field">
          <span className="field__label">Records</span>
          <select className="input" value={entity} onChange={(e) => setEntity(e.target.value)}>
            <option value="companies">Companies</option>
            <option value="contacts">Contacts</option>
            <option value="opportunities">Deals</option>
            <option value="campaigns">Campaigns</option>
          </select>
        </label>
        <label className="field">
          <span className="field__label">New owner</span>
          <select className="input" value={owner} onChange={(e) => setOwner(e.target.value)}>
            <option value="">Unassigned</option>
            {members.map((m) => <option key={m.user_id} value={m.user_id}>{memberLabel(m)} · {m.role_label}</option>)}
          </select>
        </label>
      </div>
      <label className="field">
        <span className="field__label">Record ids ({parsed.length})</span>
        <textarea className="input mono" rows={4} value={ids} onChange={(e) => setIds(e.target.value)} placeholder="co_… co_…" />
      </label>
      {action.error && <ErrorBanner error={action.error} />}
      {result && <p className="alert alert--info" role="status">{result.updated} updated{result.missing.length ? `; ${result.missing.length} not found` : ""}.</p>}
      <div className="form__actions"><button className="button button--primary" disabled={action.busy || parsed.length === 0}>Assign</button></div>
    </form>
  );
}

function PermissionsTab({ overview }: { overview: Overview }) {
  let group = "";
  return (
    <div className="card">
      <p className="muted small admin-pad">Your role: <strong>{overview.you.role_label}</strong>. The server enforces this matrix on every request; read-only members can never write.</p>
      <div className="table-wrap">
        <table className="table admin-matrix">
          <thead>
            <tr>
              <th>Permission</th>
              {overview.roles.map((r) => <th key={r.role} className="admin-matrix__role">{r.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {overview.permissions.flatMap((p) => {
              const rows: ReactNode[] = [];
              if (p.group !== group) {
                group = p.group;
                rows.push(<tr key={`g:${p.group}`} className="admin-matrix__group"><td colSpan={overview.roles.length + 1}>{p.group}</td></tr>);
              }
              rows.push(
                <tr key={p.key}>
                  <td>{p.label}</td>
                  {overview.roles.map((r) => (
                    <td key={r.role} className="admin-matrix__cell">
                      {p.roles.includes(r.role) ? <span aria-label="allowed" className="admin-yes">✓</span> : <span aria-label="not allowed" className="muted">—</span>}
                    </td>
                  ))}
                </tr>,
              );
              return rows;
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function UsersPermissions() {
  const client = useWs();
  const admin = canAdmin(useRole());
  const overview = useLoad((signal) => client.get<Overview>("/admin/overview", undefined, signal), client.base + "/ov");
  const members = useLoad((signal) => client.get<{ items: Member[] }>("/admin/members", undefined, signal), client.base + "/members");
  const teams = useLoad((signal) => client.get<{ items: Team[] }>("/admin/teams", undefined, signal), client.base + "/teams");
  const [tab, setTab] = useState("members");
  const [inviting, setInviting] = useState(false);
  const [invited, setInvited] = useState(0);
  const list = members.data?.items ?? [];
  const failure = overview.error ?? members.error;
  const reload = () => { members.refresh(); teams.refresh(); };
  return (
    <div className="page">
      <PageHeader
        title="Users & Permissions"
        subtitle="Members, roles (Admin, Manager, User, Read-only), invitations, teams and record ownership."
        actions={admin ? (
          <button type="button" className="button button--primary" onClick={() => setInviting(true)}>
            <Icon name="plus" size={16} /> Invite User
          </button>
        ) : undefined}
      />
      {failure && <ErrorBanner error={failure} onRetry={() => { overview.refresh(); members.refresh(); }} />}
      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { key: "members", label: "Members", count: list.length },
          { key: "invitations", label: "Invitations" },
          { key: "teams", label: "Teams" },
          { key: "assignment", label: "Assignment" },
          { key: "permissions", label: "Permissions" },
        ]}
      />
      <div className="admin-body">
        {members.loading && !members.data ? <Loading /> : (
          <>
            {tab === "members" && <MembersTab members={list} teams={teams.data?.items ?? []} reload={reload} />}
            {tab === "invitations" && <InvitationsTab reloadKey={invited} />}
            {tab === "teams" && <TeamsTab members={list} teams={teams} reloadTeams={reload} />}
            {tab === "assignment" && <AssignmentTab members={list} />}
            {tab === "permissions" && (overview.data ? <PermissionsTab overview={overview.data} /> : <Loading />)}
          </>
        )}
        {tab === "members" && <JoinWorkspace />}
      </div>
      {inviting && (
        <InviteDialog
          teams={teams.data?.items ?? []}
          onClose={() => setInviting(false)}
          onInvited={() => { setInvited((n) => n + 1); setTab("invitations"); }}
        />
      )}
    </div>
  );
}

// --- Audit Log ------------------------------------------------------------------------

const AUDIT_ENTITIES = ["companies", "contacts", "opportunities", "campaigns", "sequence_enrollments", "suppressions", "workflows", "import_batches", "exports", "provider_connections", "workspace_members", "workspace_invitations", "teams", "scrape_runs", "mailboxes"];

export function AuditLog() {
  const client = useWs();
  const role = useRole();
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [applied, setApplied] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Row | null>(null);
  const exporting = useAction();
  const { query, error: queryError } = auditQuery(applied, search);
  const pageSize = 50;
  const key = JSON.stringify({ query, offset });
  const page = useLoad<PageOf>(
    (signal) => (queryError ? Promise.resolve({ items: [], total: 0, limit: pageSize, offset: 0, has_more: false }) : client.list("/audit/search", { ...query, limit: pageSize, offset }, signal)),
    client.base + key,
  );
  const apply = () => { setApplied(values); setSearch(text); setOffset(0); };

  return (
    <div className="page">
      <PageHeader
        title="Audit Log"
        subtitle="Who did what, when: sign-ins, imports, exports, CRM changes, approvals, provider connections, campaigns, workflows, sends, suppression, scraper runs and admin changes. Secrets are redacted before they are written."
        actions={canManage(role) ? (
          <button type="button" className="button button--ghost button--small" disabled={exporting.busy || Boolean(queryError)} onClick={() => void exporting.run(() => client.download(`/audit/export.csv${toSearch(query)}`, "audit-log.csv"))}>
            <Icon name="download" size={14} /> Export CSV
          </button>
        ) : undefined}
      />
      {exporting.error && <ErrorBanner error={exporting.error} />}
      <div className="card">
        <FilterBar
          search={text}
          onSearch={setText}
          values={values}
          onChange={(v) => { setValues(v); if (Object.values(v).every((x) => !x)) { setApplied({}); setOffset(0); } }}
          onSubmit={apply}
          defaultOpen
          filters={[
            { key: "actor", label: "Actor", placeholder: "email contains…" },
            { key: "action", label: "Action", placeholder: "e.g. session, campaigns, admin" },
            { key: "entity_type", label: "Entity", options: AUDIT_ENTITIES },
            { key: "actor_kind", label: "Actor kind", options: ["user", "system", "agent", "workflow"] },
            { key: "from", label: "From", placeholder: "YYYY-MM-DD" },
            { key: "to", label: "To", placeholder: "YYYY-MM-DD" },
          ]}
        />
        {queryError && <p className="alert alert--error" role="alert">{queryError}</p>}
        {page.error && <ErrorBanner error={page.error} onRetry={page.refresh} />}
        {page.loading && !page.data ? <Loading /> : (
          <>
            <DataTable
              rows={page.data?.items ?? []}
              empty={{ title: "No audit entries", description: "Significant actions appear here as soon as they happen.", icon: "history" }}
              columns={[
                { key: "created_at", label: "When", render: (r) => <span className="tabular">{fmt(r.created_at)}</span> },
                { key: "actor_label", label: "Actor", render: (r) => String(r.actor_label ?? r.actor_kind ?? "—") },
                { key: "action", label: "Action", render: (r) => <code className="small">{String(r.action)}</code> },
                { key: "entity_type", label: "Entity", render: (r) => (r.entity_type ? <span className="small">{String(r.entity_type)}</span> : <span className="muted">—</span>) },
                { key: "summary", label: "Summary", render: (r) => <span className="small">{fmt(r.summary)}</span> },
                { key: "details", label: "", render: (r) => <button type="button" className="button button--ghost button--small" onClick={() => setSelected(r)}>Details</button> },
              ]}
            />
            {page.data && (
              <div className="pager">
                <span className="muted small tabular">{page.data.total === 0 ? "0 results" : `${page.data.offset + 1}–${page.data.offset + page.data.items.length} of ${page.data.total.toLocaleString()}`}</span>
                <div className="actions">
                  <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</button>
                  <button className="button button--ghost button--small" disabled={!page.data.has_more} onClick={() => setOffset(offset + pageSize)}>Next</button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
      {selected && (
        <>
          <div className="snav__scrim" onClick={() => setSelected(null)} aria-hidden="true" />
          <aside className="cr-drawer" role="dialog" aria-modal="true" aria-label="Audit entry">
            <div className="admin-card__head">
              <h3>{String(selected.action)}</h3>
              <button type="button" className="iconbtn" aria-label="Close" onClick={() => setSelected(null)}><Icon name="close" /></button>
            </div>
            <KeyValues
              items={[
                ["When", fmt(selected.created_at)],
                ["Actor", String(selected.actor_label ?? "—")],
                ["Actor id", <code className="small">{String(selected.actor_id ?? "—")}</code>],
                ["Actor kind", String(selected.actor_kind ?? "—")],
                ["Entity", selected.entity_type ? `${String(selected.entity_type)} ${String(selected.entity_id ?? "")}` : "—"],
                ["Summary", fmt(selected.summary)],
                ["Request id", fmt(selected.request_id)],
              ]}
            />
            <h3 className="admin-sub">Changes <span className="muted small">(secrets redacted)</span></h3>
            {describeChanges(selected.changes).length === 0 ? <p className="muted small">No field changes recorded.</p> : (
              <ul className="admin-list mono small">{describeChanges(selected.changes).map((line) => <li key={line}>{line}</li>)}</ul>
            )}
          </aside>
        </>
      )}
    </div>
  );
}

// --- Integrations -----------------------------------------------------------------------

interface Integration {
  provider: string;
  label: string;
  kind: string;
  category: string;
  description: string;
  requirement: string;
  secret_fields: string[];
  setting_fields: string[];
  status: string;
  connected: boolean;
  secret_hint: string | null;
  settings: Record<string, unknown>;
  last_checked_at: string | null;
  last_error: string | null;
  needs?: string;
}

function IntegrationCard({ item, events, onChange }: { item: Integration; events: string[]; onChange: () => void }) {
  const client = useWs();
  const admin = canAdmin(useRole());
  const action = useAction();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const secrets: Record<string, string> = {};
    const settings: Record<string, unknown> = {};
    for (const f of item.secret_fields) if (form[f]) secrets[f] = form[f];
    for (const f of item.setting_fields) {
      if (form[f] === undefined) continue;
      settings[f] = f === "events" ? parseEvents(form[f]) : form[f];
    }
    const out = await action.run(() => client.put<Integration & { signing_secret?: string }>(`/integrations/${item.provider}`, { secrets, settings }));
    if (out) {
      setMessage(out.signing_secret ? `Signing secret (shown once, save it now): ${out.signing_secret}` : "Saved.");
      setForm({});
      setOpen(false);
      onChange();
    }
  };

  const settingRows: [string, ReactNode][] = Object.entries(item.settings).map(([k, v]) => [k.replace(/_/g, " "), Array.isArray(v) ? v.join(", ") || "all events" : fmt(v)]);

  return (
    <div className="card admin-integration">
      <div className="admin-card__head">
        <div>
          <h3>{item.label}</h3>
          <span className="muted small">{item.category}</span>
        </div>
        <Pill value={item.connected ? item.status : "not_configured"} />
      </div>
      <p className="small">{item.description}</p>
      <p className="muted small">Needs: {item.requirement}.</p>
      {item.needs && !item.connected && <p className="muted small">Next: {item.needs}.</p>}
      {item.connected && <KeyValues items={[["Secret", item.secret_hint ?? "—"], ...settingRows, ["Last checked", fmt(item.last_checked_at)]]} />}
      {item.last_error && <p className="alert alert--error small">{item.last_error}</p>}
      {message && <p className="alert alert--info small" role="status">{message}</p>}
      {action.error && <ErrorBanner error={action.error} />}
      {admin ? (
        <div className="actions">
          <button type="button" className="button button--ghost button--small" onClick={() => setOpen((o) => !o)}>{item.connected ? "Edit" : "Configure"}</button>
          {item.connected && (
            <>
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void action.run(async () => { const r = await client.post<{ status: string; detail: string }>(`/integrations/${item.provider}/test`); setMessage(`Test: ${r.status.replace(/_/g, " ")} — ${r.detail}`); onChange(); })}>Test</button>
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => { if (window.confirm(`Disconnect ${item.label}? Its stored secret is deleted.`)) void action.run(async () => { await client.del(`/integrations/${item.provider}`); setMessage("Disconnected."); onChange(); }); }}>Disconnect</button>
            </>
          )}
        </div>
      ) : (
        <p className="muted small">Only workspace admins can configure integrations.</p>
      )}
      {open && admin && (
        <form className="form admin-form" onSubmit={save}>
          {item.secret_fields.map((f) => (
            <label key={f} className="field">
              <span className="field__label">{f.replace(/_/g, " ")} {item.connected && <span className="muted small">(leave blank to keep)</span>}</span>
              <input className="input mono" type="password" autoComplete="off" value={form[f] ?? ""} onChange={(e) => setForm({ ...form, [f]: e.target.value })} />
            </label>
          ))}
          {item.setting_fields.map((f) => (
            <label key={f} className="field">
              <span className="field__label">{f.replace(/_/g, " ")}</span>
              <input
                className="input"
                value={form[f] ?? (Array.isArray(item.settings[f]) ? (item.settings[f] as string[]).join(", ") : String(item.settings[f] ?? ""))}
                onChange={(e) => setForm({ ...form, [f]: e.target.value })}
                placeholder={f === "events" ? `comma separated; blank = all (${events.join(", ")})` : f === "url" ? "https://…" : ""}
              />
            </label>
          ))}
          <p className="muted small">Secrets are encrypted on the server and never shown again.</p>
          <div className="form__actions"><button className="button button--primary button--small" disabled={action.busy}>Save</button></div>
        </form>
      )}
    </div>
  );
}

function RetryButton({ id, onDone }: { id: string; onDone: () => void }) {
  const client = useWs();
  const role = useRole();
  const action = useAction();
  if (!canWrite(role)) return null;
  return <button type="button" className="button button--ghost button--small" disabled={action.busy} title={action.error?.message} onClick={() => void action.run(async () => { await client.post(`/integrations/deliveries/${id}/retry`); onDone(); })}>Retry</button>;
}

export function Integrations() {
  const client = useWs();
  const list = useLoad((signal) => client.get<{ items: Integration[]; events: string[]; mock_mode: boolean }>("/integrations", undefined, signal), client.base + "/integrations");
  const deliveries = useLoad((signal) => client.get<{ items: Row[] }>("/integrations/deliveries", { limit: 50 }, signal), client.base + "/deliveries");
  const refresh = () => { list.refresh(); deliveries.refresh(); };
  return (
    <div className="page">
      <PageHeader title="Integrations" subtitle="Google Workspace, Microsoft 365, calendars, Slack and webhooks. Live calls happen only for configured integrations; everything else shows Not configured." />
      {list.data?.mock_mode && <p className="alert alert--info" role="note">Mock mode is on: deliveries are recorded but nothing is sent.</p>}
      {list.error && <ErrorBanner error={list.error} onRetry={refresh} />}
      {list.loading && !list.data ? <Loading /> : (
        <div className="admin-cards">
          {(list.data?.items ?? []).map((item) => <IntegrationCard key={item.provider} item={item} events={list.data?.events ?? []} onChange={refresh} />)}
        </div>
      )}
      <h2 className="admin-sub">Delivery log</h2>
      <div className="card">
        {deliveries.error && <ErrorBanner error={deliveries.error} onRetry={deliveries.refresh} />}
        <DataTable
          rows={deliveries.data?.items ?? []}
          empty={{ title: "No deliveries yet", description: "Tests and events sent to Slack, webhooks or calendars are listed here with their outcome.", icon: "plug" }}
          columns={[
            { key: "created_at", label: "When", render: (r) => fmt(r.created_at) },
            { key: "provider", label: "Integration" },
            { key: "event", label: "Event", render: (r) => <code className="small">{String(r.event)}</code> },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "response_code", label: "HTTP" },
            { key: "error", label: "Detail", render: (r) => <span className="muted small">{fmt(r.error)}</span> },
            { key: "retry", label: "", render: (r) => (r.status === "failed" ? <RetryButton id={r.id} onDone={refresh} /> : null) },
          ]}
        />
      </div>
    </div>
  );
}

// --- Notifications -----------------------------------------------------------------------

export function Notifications() {
  const client = useWs();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const list = useLoad((signal) => client.get<{ items: Row[]; unread: number }>("/notifications", { unread: unreadOnly, limit: 100 }, signal), client.base + `/notif${unreadOnly}`);
  const action = useAction();
  const items = list.data?.items ?? [];
  const failure = list.error ?? action.error;
  return (
    <div className="page">
      <PageHeader
        title="Notifications"
        subtitle="Assignments, finished imports and scrapes, workflow results and other workspace events."
        actions={
          <>
            <label className="admin-check small"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} /> Unread only</label>
            <button type="button" className="button button--ghost button--small" disabled={action.busy || !list.data?.unread} onClick={() => void action.run(async () => { await client.post("/notifications/read-all"); list.refresh(); })}>Mark all read</button>
          </>
        }
      />
      {failure && <ErrorBanner error={failure} onRetry={list.refresh} />}
      <div className="card">
        {list.loading && !list.data ? <Loading /> : items.length === 0 ? (
          <EmptyState icon="bell" title={unreadOnly ? "No unread notifications" : "No notifications yet"} description="You will be notified here when records are assigned to you or background work finishes." />
        ) : (
          <ul className="admin-notifs">
            {items.map((n) => (
              <li key={n.id} className={`admin-notif${n.read_at ? "" : " admin-notif--unread"}`}>
                <Pill value={n.severity} />
                <div className="admin-grow">
                  <strong>{String(n.title)}</strong>
                  {n.body ? <p className="muted small">{String(n.body)}</p> : null}
                  <span className="muted small">{fmt(n.created_at)}{n.user_id ? "" : " · everyone"}</span>
                </div>
                {n.link ? <Link className="button button--ghost button--small" to={String(n.link)}>Open</Link> : null}
                {!n.read_at && <button type="button" className="button button--ghost button--small" onClick={() => void action.run(async () => { await client.post(`/notifications/${n.id}/read`); list.refresh(); })}>Mark read</button>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// --- Settings entry points ------------------------------------------------------------------

const ADMIN_LINKS: { to: string; label: string; icon: "mail" | "plug" | "shield" | "history" | "bell"; text: string }[] = [
  { to: "/settings/sending", label: "Email & Sending", icon: "mail", text: "Connect Google Workspace, Microsoft 365 or SMTP mailboxes and set sending limits." },
  { to: "/settings/users", label: "Users & Permissions", icon: "shield", text: "Members, roles, invitations, teams and record ownership." },
  { to: "/settings/integrations", label: "Integrations", icon: "plug", text: "Slack, webhooks, Google Workspace, Microsoft 365 and calendars." },
  { to: "/settings/audit", label: "Audit Log", icon: "history", text: "Every significant action, searchable and exportable." },
  { to: "/notifications", label: "Notifications", icon: "bell", text: "Assignments and finished background work." },
];

/** A card of links to the administration pages (Settings → Administration tab). */
export function AdminLinks() {
  return (
    <div className="admin-cards">
      {ADMIN_LINKS.map((link) => (
        <Link key={link.to} to={link.to} className="card admin-link">
          <span className="admin-card__head"><h3><Icon name={link.icon} size={16} /> {link.label}</h3><Icon name="chevron" size={14} /></span>
          <span className="muted small">{link.text}</span>
        </Link>
      ))}
    </div>
  );
}
