// Settings → Users & Permissions, Audit Log, Integrations, and the Notifications page.
// Every action is also enforced by the API (and RLS); the UI only hides what a
// role cannot do so read-only members are not offered dead buttons.

import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { request } from "../../api/client";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { Icon } from "../../shell/Icon";
import type { PageOf, Row } from "../api";
import { ASSIGNABLE_ROLES, auditQuery, canAdmin, canManage, canWrite, describeChanges, parseEvents, parseIds, roleLabel, toSearch } from "../logic/admin";
import { DataTable, FilterBar, KeyValues, PageHeader, Pill, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
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
  teams: string[];
  is_you: boolean;
}

interface Overview {
  you: { role: string; role_label: string; permissions: Record<string, boolean> };
  roles: { role: string; label: string }[];
  permissions: { key: string; label: string; group: string; roles: string[] }[];
}

function memberName(m: { email?: string | null; user_id: string }): string {
  return m.email ?? `${m.user_id.slice(0, 8)}…`;
}

function MembersTab({ members, reload }: { members: Member[]; reload: () => void }) {
  const client = useWs();
  const role = useRole();
  const action = useAction();
  const admin = canAdmin(role);
  return (
    <div className="card">
      {!admin && <ReadOnlyNote need="Workspace admins" />}
      {action.error && <ErrorBanner error={action.error} />}
      <DataTable
        rows={members.map((m) => ({ ...m, id: m.user_id }))}
        empty={{ title: "No members", description: "Invite teammates from the Invitations tab.", icon: "users" }}
        columns={[
          { key: "email", label: "Member", render: (m) => <span>{memberName(m)}{m.is_you && <span className="chip admin-you">you</span>}<span className="muted small block mono">{m.user_id}</span></span> },
          {
            key: "role",
            label: "Role",
            render: (m) =>
              admin && m.role !== "owner" ? (
                <select
                  className="input input--small"
                  aria-label={`Role for ${memberName(m)}`}
                  value={m.role}
                  disabled={action.busy}
                  onChange={(e) => void action.run(async () => { await client.patch(`/admin/members/${m.user_id}`, { role: e.target.value }); reload(); })}
                >
                  {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                </select>
              ) : (
                <Pill value={m.role_label} />
              ),
          },
          { key: "teams", label: "Teams", render: (m) => (m.teams.length ? m.teams.join(", ") : <span className="muted">—</span>) },
          {
            key: "actions",
            label: "",
            render: (m) =>
              admin && m.role !== "owner" && !m.is_you ? (
                <button
                  type="button"
                  className="button button--ghost button--small"
                  disabled={action.busy}
                  onClick={() => {
                    if (!window.confirm(`Remove ${memberName(m)} from this workspace?`)) return;
                    void action.run(async () => { await client.del(`/admin/members/${m.user_id}`); reload(); });
                  }}
                >
                  Remove
                </button>
              ) : null,
          },
        ]}
      />
    </div>
  );
}

function InvitationsTab() {
  const client = useWs();
  const admin = canAdmin(useRole());
  const list = useLoad((signal) => client.get<{ items: Row[] }>("/admin/invitations", undefined, signal), client.base + "/inv");
  const action = useAction();
  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [created, setCreated] = useState<{ email: string; token: string } | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const result = await action.run(() => client.post<Row & { token: string }>("/admin/invitations", { email, role: inviteRole }));
    if (result) {
      setCreated({ email: String(result.email), token: result.token });
      setEmail("");
      list.refresh();
    }
  };

  return (
    <div className="stack">
      {admin ? (
        <form className="card form admin-inline" onSubmit={submit}>
          <label className="field admin-grow">
            <span className="field__label">Email</span>
            <input className="input" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="teammate@company.com" />
          </label>
          <label className="field">
            <span className="field__label">Role</span>
            <select className="input" value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
              {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
          </label>
          <div className="form__actions"><button className="button button--primary" disabled={action.busy || !email.trim()}>Create invitation</button></div>
          {action.error && <ErrorBanner error={action.error} />}
        </form>
      ) : (
        <ReadOnlyNote need="Workspace admins" />
      )}
      {created && (
        <div className="card admin-token" role="status">
          <strong>Invitation for {created.email}</strong>
          <p className="muted small">Send this code to the invitee. It is shown only now and stored only as a hash. They sign in as {created.email} and paste it under Settings → Users &amp; Permissions → Join a workspace.</p>
          <code className="admin-token__value">{created.token}</code>
          <div className="actions">
            <button type="button" className="button button--ghost button--small" onClick={() => void navigator.clipboard?.writeText(created.token)}>Copy</button>
            <button type="button" className="button button--ghost button--small" onClick={() => setCreated(null)}>Done</button>
          </div>
        </div>
      )}
      <div className="card">
        {list.error && <ErrorBanner error={list.error} onRetry={list.refresh} />}
        {list.loading && !list.data ? <Loading /> : (
          <DataTable
            rows={list.data?.items ?? []}
            empty={{ title: "No invitations", description: "Invite teammates by email; they join with the role you choose.", icon: "users" }}
            columns={[
              { key: "email", label: "Email" },
              { key: "role", label: "Role", render: (r) => roleLabel(String(r.role)) },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "expires_at", label: "Expires", render: (r) => fmtDate(r.expires_at) },
              {
                key: "actions",
                label: "",
                render: (r) => admin && r.status === "pending" ? (
                  <button type="button" className="button button--ghost button--small" onClick={() => void action.run(async () => { await client.post(`/admin/invitations/${r.id}/revoke`); list.refresh(); })}>Revoke</button>
                ) : null,
              },
            ]}
          />
        )}
      </div>
    </div>
  );
}

/** For someone who was given an invitation code: redeem it into the workspace it names. */
function JoinWorkspace() {
  const { reload, select } = useWorkspace();
  const action = useAction();
  const [token, setToken] = useState("");
  const [joined, setJoined] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const code = token.trim();
    const workspaceId = code.split(".")[0];
    const result = await action.run(() =>
      request<{ workspace_id: string; role: string }>(`/api/v1/w/${encodeURIComponent(workspaceId)}/invitations/accept`, { method: "POST", body: JSON.stringify({ token: code }) }),
    );
    if (result) {
      setJoined(`Joined as ${roleLabel(result.role)}.`);
      setToken("");
      reload();
      select(result.workspace_id);
    }
  };
  return (
    <form className="card form admin-inline" onSubmit={submit}>
      <label className="field admin-grow">
        <span className="field__label">Join a workspace with an invitation code</span>
        <input className="input mono" value={token} onChange={(e) => setToken(e.target.value)} placeholder="paste the code you were sent" />
      </label>
      <div className="form__actions"><button className="button button--ghost" disabled={action.busy || !token.includes(".")}>Join</button></div>
      {action.error && <ErrorBanner error={action.error} />}
      {joined && <p className="muted small">{joined}</p>}
    </form>
  );
}

function TeamsTab({ members }: { members: Member[] }) {
  const client = useWs();
  const manage = canManage(useRole());
  const teams = useLoad((signal) => client.get<{ items: (Row & { members: { user_id: string; role: string }[] })[] }>("/admin/teams", undefined, signal), client.base + "/teams");
  const action = useAction();
  const [name, setName] = useState("");
  const [pick, setPick] = useState<Record<string, string>>({});
  const byId = Object.fromEntries(members.map((m) => [m.user_id, m]));
  const run = (fn: () => Promise<unknown>) => void action.run(async () => { await fn(); teams.refresh(); });

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
                      <span>{byId[tm.user_id] ? memberName(byId[tm.user_id]) : `${tm.user_id.slice(0, 8)}…`}{tm.role === "lead" && <span className="chip admin-you">lead</span>}</span>
                      {manage && <button type="button" className="button button--ghost button--small" onClick={() => run(() => client.del(`/admin/teams/${team.id}/members/${tm.user_id}`))}>Remove</button>}
                    </li>
                  ))}
                </ul>
              )}
              {manage && (
                <div className="admin-inline">
                  <select className="input input--small" aria-label="Add member" value={pick[team.id] ?? ""} onChange={(e) => setPick({ ...pick, [team.id]: e.target.value })}>
                    <option value="">Add a member…</option>
                    {members.filter((m) => !team.members.some((tm) => tm.user_id === m.user_id)).map((m) => <option key={m.user_id} value={m.user_id}>{memberName(m)}</option>)}
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
            {members.map((m) => <option key={m.user_id} value={m.user_id}>{memberName(m)} · {m.role_label}</option>)}
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
  const overview = useLoad((signal) => client.get<Overview>("/admin/overview", undefined, signal), client.base + "/ov");
  const members = useLoad((signal) => client.get<{ items: Member[] }>("/admin/members", undefined, signal), client.base + "/members");
  const [tab, setTab] = useState("members");
  const list = members.data?.items ?? [];
  const failure = overview.error ?? members.error;
  return (
    <div className="page">
      <PageHeader title="Users & Permissions" subtitle="Members, roles (Admin, Manager, User, Read-only), invitations, teams and record ownership." />
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
            {tab === "members" && <MembersTab members={list} reload={members.refresh} />}
            {tab === "invitations" && <InvitationsTab />}
            {tab === "teams" && <TeamsTab members={list} />}
            {tab === "assignment" && <AssignmentTab members={list} />}
            {tab === "permissions" && (overview.data ? <PermissionsTab overview={overview.data} /> : <Loading />)}
          </>
        )}
        {tab === "members" && <JoinWorkspace />}
      </div>
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
