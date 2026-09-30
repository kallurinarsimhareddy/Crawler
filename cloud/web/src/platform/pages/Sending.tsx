// Email & Sending (connected mailboxes), the suppression list, and the campaign and
// sequence detail pages. Nothing on these pages sends email by itself: every send
// goes through the server's gates (environment, campaign sending, approval,
// suppression, schedule, mailbox limits), and the UI says which gate is closed.

import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { PageOf, Row } from "../api";
import { cadenceDays, moveStep, parseBulkValues, PROTECTED_REASONS, providerState, ratio, scheduleSummary, validateSteps, type EditableStep, type ProviderInfo, type Schedule } from "../logic/sending";
import { DataTable, FilterBar, KeyValues, PageHeader, Pill, ResourceList, Stat, Tabs, Tags, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import "../styles/sending.css";

type Json = Record<string, unknown>;

const EMPTY_PAGE: PageOf = { items: [], total: 0, limit: 0, offset: 0, has_more: false };

function num(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function Notice({ tone = "info", children }: { tone?: "info" | "warning" | "error"; children: ReactNode }) {
  return <div className={`alert alert--${tone}`} role={tone === "error" ? "alert" : "status"}>{children}</div>;
}

function Done({ text }: { text: string | null }) {
  return text ? <p className="send-done small" role="status">{text}</p> : null;
}

function useIsAdmin(): boolean {
  const { current } = useWorkspace();
  return current?.role === "owner" || current?.role === "admin";
}

// --- Settings → Email & Sending ----------------------------------------------------------

interface Provider extends ProviderInfo {
  label: string;
  scopes: string[];
  vendors: string[];
  events: Record<string, boolean>;
}

interface SendingStatus {
  providers: Provider[];
  outbox: { sending_allowed: boolean; why_not: string | null; environment: string; by_status: Record<string, number> };
  capabilities: Record<string, Record<string, boolean>>;
  webhook_providers: string[];
  webhooks_configured: Record<string, boolean>;
  secrets_key_configured: boolean;
  unsubscribe_configured: boolean;
}

const EVENT_KINDS = ["delivered", "bounced", "deferred", "opened", "clicked", "replied", "unsubscribed", "complained"];

export function EmailSending() {
  const client = useWs();
  const { current } = useWorkspace();
  const admin = useIsAdmin();
  const [params] = useSearchParams();
  const [tick, setTick] = useState(0);
  const status = useLoad((s) => client.get<SendingStatus>("/sending/status", undefined, s), client.base + "ss" + tick);
  const boxes = useLoad((s) => client.get<{ items: Row[] }>("/mailboxes", undefined, s), client.base + "mb" + tick);
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  const [testTo, setTestTo] = useState<Record<string, string>>({});
  const refresh = () => setTick((n) => n + 1);

  const run = (label: string, fn: () => Promise<unknown>) =>
    void action.run(async () => {
      const result = (await fn()) as Json | undefined;
      const detail = result && typeof result.detail === "string" ? `: ${result.detail}` : "";
      setDone(`${label}${detail}`);
      refresh();
    });

  const connected = (provider: string) => (boxes.data?.items ?? []).filter((b) => b.provider === provider && b.status === "connected").length;
  const s = status.data;

  return (
    <div className="page">
      <PageHeader
        title="Email & Sending"
        subtitle="Connect the mailboxes SANA GTM sends from: OAuth for Google Workspace and Microsoft 365, or an API/SMTP relay. Mailbox passwords are never stored."
      />
      {params.get("connected") && <Notice>Connected {params.get("connected")}.</Notice>}
      {params.get("oauth_error") && <Notice tone="error">Mailbox not connected: {params.get("oauth_error")}</Notice>}
      {status.error && <ErrorBanner error={status.error} onRetry={status.refresh} />}
      {s && !s.outbox.sending_allowed && (
        <Notice tone="warning">
          <strong>Sending is off in this environment ({s.outbox.environment}).</strong> Campaigns and sequences can be prepared, approved and queued,
          but every message is recorded as blocked until the server runs in production with sending explicitly enabled.
        </Notice>
      )}
      {s && !s.secrets_key_configured && <Notice tone="warning">The server has no secrets key, so OAuth tokens and API keys cannot be stored yet (CAREERCLOUD_PLATFORM_SECRETS_KEY).</Notice>}
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />

      <h2 className="send-h2">Providers</h2>
      {!s ? (
        <Loading />
      ) : (
        <div className="send-providers">
          {s.providers.map((p) => (
            <ProviderCard key={p.provider} provider={p} connectedCount={connected(p.provider)} admin={admin} busy={action.busy} onRun={run} />
          ))}
        </div>
      )}

      <h2 className="send-h2">Connected mailboxes</h2>
      <div className="card">
        {boxes.error && <ErrorBanner error={boxes.error} onRetry={boxes.refresh} />}
        {!boxes.data ? (
          <Loading />
        ) : (
          <DataTable
            rows={boxes.data.items}
            empty={{ title: "No mailboxes connected", description: "Connect Google Workspace, Microsoft 365 or an email API above. Until then, campaigns can be prepared but not sent.", icon: "mail" }}
            columns={[
              { key: "address", label: "Mailbox", render: (r) => <span><strong>{String(r.address)}</strong>{r.is_default ? <span className="chip send-chip">Default</span> : null}<br /><span className="muted small">{String(r.display_name ?? "")}</span></span> },
              { key: "provider", label: "Provider", render: (r) => <Pill value={r.provider} /> },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "scopes", label: "Scopes", render: (r) => <Tags values={r.scopes} /> },
              { key: "limits", label: "Sent today / limits", render: (r) => <span className="tabular small">{num(r.sent_today)} / {num(r.daily_limit)} per day · {num(r.hourly_limit)} per hour</span> },
              { key: "last_test_at", label: "Last test", render: (r) => <span className="small">{fmtDate(r.last_test_at)}<br /><span className="muted">{String((r.last_test_result as Json | null)?.detail ?? "")}</span></span> },
              { key: "health", label: "Health", render: (r) => <Pill value={r.health} /> },
              {
                key: "actions",
                label: "",
                render: (r) => (
                  <div className="send-actions">
                    <button className="button button--ghost button--small" disabled={!admin || action.busy} onClick={() => run("Connection test", () => client.post(`/mailboxes/${r.id}/test`, {}))}>Test</button>
                    <span className="send-inline">
                      <input className="input input--small" placeholder="test recipient" aria-label="Test recipient" value={testTo[r.id] ?? ""} onChange={(e) => setTestTo({ ...testTo, [r.id]: e.target.value })} />
                      <button className="button button--ghost button--small" disabled={!admin || action.busy || !testTo[r.id]} onClick={() => run("Test send", () => client.post(`/mailboxes/${r.id}/test`, { to: testTo[r.id] }))}>Test send</button>
                    </span>
                    {!r.is_default && r.status === "connected" && <button className="button button--ghost button--small" disabled={!admin || action.busy} onClick={() => run("Default sender set", () => client.post(`/mailboxes/${r.id}/default`, {}))}>Make default</button>}
                    {r.status === "connected" && <button className="button button--ghost button--small" disabled={!admin || action.busy} onClick={() => run("Paused", () => client.patch(`/mailboxes/${r.id}`, { status: "paused" }))}>Pause</button>}
                    {r.status === "paused" && <button className="button button--ghost button--small" disabled={!admin || action.busy} onClick={() => run("Resumed", () => client.patch(`/mailboxes/${r.id}`, { status: "connected" }))}>Resume</button>}
                    {r.status !== "disconnected" && <button className="button button--danger button--small" disabled={!admin || action.busy} onClick={() => run("Disconnected", () => client.post(`/mailboxes/${r.id}/disconnect`, {}))}>Disconnect</button>}
                  </div>
                ),
              },
            ]}
          />
        )}
        {!admin && <p className="muted small">Only workspace admins can connect, test or disconnect mailboxes.</p>}
      </div>

      {s && (
        <>
          <h2 className="send-h2">Delivery events by provider</h2>
          <div className="card">
            <p className="muted small">
              SANA GTM only shows events a provider can actually report. Gmail and Microsoft 365 send from your own mailbox and have no delivery
              webhooks; replies there are recorded by hand below, or by a forwarding rule posting to the generic webhook.
            </p>
            <div className="table-wrap">
              <table className="table send-capabilities">
                <thead>
                  <tr>
                    <th>Provider</th>
                    {EVENT_KINDS.map((k) => <th key={k}>{k}</th>)}
                    <th>Webhook</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(s.capabilities).map(([provider, caps]) => (
                    <tr key={provider}>
                      <td>{provider}</td>
                      {EVENT_KINDS.map((k) => <td key={k} aria-label={caps[k] ? "supported" : "not supported"}>{caps[k] ? "✓" : "—"}</td>)}
                      <td>{s.webhook_providers.includes(provider) ? <Pill value={s.webhooks_configured[provider] ? "configured" : "not_configured"} /> : <span className="muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {current && <p className="muted small">Webhook URL: <code>/api/v1/w/{current.id}/events/&lt;provider&gt;/webhook</code> with <code>X-Webhook-Secret</code> or an <code>X-Signature</code> HMAC-SHA256 of the body.</p>}
          </div>
          <RecordEvent />
        </>
      )}

      <h2 className="send-h2">Outbox</h2>
      <ResourceList
        load={(q, sig) => client.list("/outbox", q, sig)}
        filters={[{ key: "status", label: "Status", options: ["queued", "scheduled", "sending", "sent", "blocked", "failed", "cancelled"] }]}
        empty={{ title: "Nothing queued yet", description: "Every message a sequence renders appears here with the mailbox used and what happened to it.", icon: "send" }}
        columns={[
          { key: "to_email", label: "To" },
          { key: "subject", label: "Subject", render: (r) => String(r.subject).slice(0, 80) },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "block_reason", label: "Reason", render: (r) => <span className="muted small">{fmt(r.block_reason ?? r.error)}</span> },
          { key: "provider", label: "Via" },
          { key: "created_at", label: "Queued", render: (r) => fmtDate(r.created_at) },
        ]}
      />
    </div>
  );
}

function ProviderCard({ provider, connectedCount, admin, busy, onRun }: { provider: Provider; connectedCount: number; admin: boolean; busy: boolean; onRun: (label: string, fn: () => Promise<unknown>) => void }) {
  const client = useWs();
  const state = providerState(provider, connectedCount);
  const [address, setAddress] = useState("");
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [vendor, setVendor] = useState(provider.vendors[0] ?? "sendgrid");
  const connectOAuth = () =>
    onRun("Redirecting to the provider", async () => {
      const result = await client.post<{ authorize_url: string }>(`/mailboxes/oauth/${provider.provider}/start`, { redirect_to: "/settings/sending" });
      window.location.assign(result.authorize_url);
      return result;
    });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (provider.provider === "api") {
      onRun("Mailbox connected", () => client.post("/mailboxes/api", { address, api_key: key, vendor, display_name: name || undefined }));
      setKey("");
    } else {
      onRun("Sender registered", () => client.post("/mailboxes/smtp", { address, display_name: name || undefined }));
    }
  };
  return (
    <div className={`card send-provider send-provider--${state.tone}`}>
      <div className="send-provider__head">
        <h3>{provider.label}</h3>
        <span className={`chip send-state send-state--${state.tone}`}>{state.label}</span>
      </div>
      <p className="muted small">{state.detail}</p>
      {provider.scopes.length > 0 && <p className="small">Scopes: <Tags values={provider.scopes} /></p>}
      {provider.auth === "oauth" && (
        <button className="button button--primary button--small" disabled={!admin || busy || !provider.configured} onClick={connectOAuth}>
          Connect with {provider.provider === "google" ? "Google" : "Microsoft"}
        </button>
      )}
      {provider.auth !== "oauth" && (
        <form className="send-provider__form" onSubmit={submit}>
          <input className="input input--small" type="email" required placeholder="sender@yourdomain.com" aria-label="Sender address" value={address} onChange={(e) => setAddress(e.target.value)} />
          <input className="input input--small" placeholder="Display name (optional)" aria-label="Display name" value={name} onChange={(e) => setName(e.target.value)} />
          {provider.provider === "api" && (
            <>
              <select className="input input--small" aria-label="Vendor" value={vendor} onChange={(e) => setVendor(e.target.value)}>
                {provider.vendors.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <input className="input input--small" type="password" autoComplete="off" required placeholder="API key (stored encrypted)" aria-label="API key" value={key} onChange={(e) => setKey(e.target.value)} />
            </>
          )}
          <button className="button button--ghost button--small" disabled={!admin || busy || !address || (provider.provider === "api" && !key)}>
            {provider.provider === "api" ? "Connect" : "Register sender"}
          </button>
          {provider.provider === "smtp" && <p className="muted small">Uses the server relay only; no mailbox password is ever asked for.</p>}
        </form>
      )}
    </div>
  );
}

function RecordEvent() {
  const client = useWs();
  const [kind, setKind] = useState("reply");
  const [email, setEmail] = useState("");
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  return (
    <form
      className="card send-record"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const r = await client.post<Json>("/events/manual", { kind, email });
          setDone(`Recorded (${num(r.applied)} applied)`);
          setEmail("");
        });
      }}
    >
      <h3>Record a reply, bounce or unsubscribe</h3>
      <p className="muted small">For mailboxes without delivery webhooks. A reply stops the contact's sequences and creates a follow-up task; a bounce or unsubscribe also suppresses the address.</p>
      <div className="send-inline">
        <select className="input input--small" aria-label="Event" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="reply">Reply</option>
          <option value="bounce">Hard bounce</option>
          <option value="unsubscribe">Unsubscribe</option>
          <option value="complaint">Spam complaint</option>
        </select>
        <input className="input input--small" type="email" required placeholder="contact@company.com" aria-label="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <button className="button button--ghost button--small" disabled={action.busy || !email}>Record</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />
    </form>
  );
}

// --- Suppression list ----------------------------------------------------------------------

const REASONS = ["manual", "unsubscribe", "hard_bounce", "bounce", "complaint", "invalid", "blocked", "legal", "customer", "role_policy"];

export function Suppressions() {
  const client = useWs();
  const admin = useIsAdmin();
  const [tick, setTick] = useState(0);
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const query = { ...filters, q: search, limit: 50, offset };
  const rows = useLoad((s) => client.list("/suppressions", query, s), client.base + JSON.stringify(query) + tick);
  const stats = useLoad((s) => client.get<Json>("/suppression/stats", undefined, s), client.base + "st" + tick);
  const glob = useLoad((s) => client.get<{ emails: string[]; domains: string[] }>("/suppression/global", undefined, s), client.base + "gl");
  const campaigns = useLoad((s) => client.list("/campaigns", { limit: 200 }, s).catch(() => EMPTY_PAGE), client.base + "cp");
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  const refresh = () => {
    setSelected([]);
    setTick((n) => n + 1);
  };
  useEffect(() => setOffset(0), [search, JSON.stringify(filters)]);

  const remove = () =>
    void action.run(async () => {
      const r = await client.post<{ removed: number; refused: { id: string; reason: string }[] }>("/suppression/remove", { ids: selected });
      setDone(`Removed ${r.removed}.${r.refused.length ? ` ${r.refused.length} kept: ${r.refused[0].reason}` : ""}`);
      refresh();
    });

  const byReason = (stats.data?.by_reason ?? {}) as Record<string, number>;
  const items = rows.data?.items ?? [];
  return (
    <div className="page">
      <PageHeader
        title="Suppression list"
        subtitle="Addresses and domains that must never be emailed. Checked before every send. Bounces, complaints and unsubscribes are added automatically."
        actions={<button className="button button--ghost" onClick={() => void action.run(() => client.download("/suppression/export", "suppressions.csv"))}>Export CSV</button>}
      />
      <div className="stats">
        <Stat label="Suppressed" value={fmt(stats.data?.total ?? "—")} />
        <Stat label="Unsubscribed" value={fmt(byReason.unsubscribe ?? 0)} />
        <Stat label="Hard bounces" value={fmt((byReason.hard_bounce ?? 0) + (byReason.bounce ?? 0))} />
        <Stat label="Complaints" value={fmt(byReason.complaint ?? 0)} />
        <Stat label="Platform-wide" value={fmt(stats.data?.global ?? 0)} hint="operator list, read-only" />
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />
      <div className="grid-2">
        <BulkSuppress campaigns={campaigns.data?.items ?? []} onDone={(t) => { setDone(t); refresh(); }} />
        <div className="card">
          <h3>Import or check</h3>
          <label className="field">
            <span className="field__label">Import CSV (value/email/domain column, optional reason) or one value per line</span>
            <input
              className="input"
              type="file"
              accept=".csv,.txt,text/csv,text/plain"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                void action.run(async () => {
                  const text = await file.text();
                  const r = await client.post<Json>("/suppression/import", { text });
                  setDone(`Imported: ${num(r.added)} added, ${num(r.existing)} already listed, ${num(r.invalid_count)} invalid`);
                  refresh();
                });
                e.target.value = "";
              }}
            />
          </label>
          <CheckAddress />
        </div>
      </div>
      <div className="card">
        <FilterBar
          search={q}
          onSearch={setQ}
          values={filters}
          onChange={setFilters}
          onSubmit={() => setSearch(q.trim())}
          filters={[
            { key: "kind", label: "Kind", options: ["email", "domain"] },
            { key: "reason", label: "Reason", options: REASONS },
            { key: "scope", label: "Scope", options: ["workspace", "campaign"] },
          ]}
          extra={selected.length > 0 ? <button type="button" className="button button--danger button--small" disabled={action.busy} onClick={remove}>Remove {selected.length}</button> : undefined}
        />
        {rows.error && <ErrorBanner error={rows.error} onRetry={rows.refresh} />}
        {!rows.data ? (
          <Loading />
        ) : (
          <DataTable
            rows={items}
            empty={{ title: "No suppressions", description: "Add addresses or domains that must never be contacted. Bounces and unsubscribes appear here automatically.", icon: "block" }}
            columns={[
              {
                key: "select",
                label: "",
                render: (r) => {
                  const locked = PROTECTED_REASONS.includes(String(r.reason)) && !admin;
                  return (
                    <input
                      type="checkbox"
                      aria-label={`Select ${String(r.value)}`}
                      disabled={locked}
                      title={locked ? "Only an admin can remove this" : undefined}
                      checked={selected.includes(r.id)}
                      onChange={(e) => setSelected(e.target.checked ? [...selected, r.id] : selected.filter((id) => id !== r.id))}
                    />
                  );
                },
              },
              { key: "value", label: "Value", render: (r) => <span className="mono small">{String(r.value)}</span> },
              { key: "kind", label: "Kind", render: (r) => <Pill value={r.kind} /> },
              { key: "reason", label: "Reason", render: (r) => <Pill value={r.reason} /> },
              { key: "scope", label: "Scope", render: (r) => (r.scope === "campaign" ? <span>campaign <Link className="link" to={`/campaigns/${r.campaign_id}`}>open</Link></span> : "workspace") },
              { key: "expires_at", label: "Expires", render: (r) => fmtDate(r.expires_at) },
              { key: "source", label: "Source" },
              { key: "created_at", label: "Added", render: (r) => fmtDate(r.created_at) },
            ]}
          />
        )}
        {rows.data && (
          <div className="pager">
            <span className="muted small tabular">{rows.data.total === 0 ? "0 results" : `${offset + 1}–${offset + items.length} of ${rows.data.total.toLocaleString()}`}</span>
            <div className="actions">
              <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
              <button className="button button--ghost button--small" disabled={!rows.data.has_more} onClick={() => setOffset(offset + 50)}>Next</button>
            </div>
          </div>
        )}
      </div>
      {glob.data && glob.data.emails.length + glob.data.domains.length > 0 && (
        <details className="card">
          <summary>Platform-wide list ({glob.data.emails.length + glob.data.domains.length}, managed by the operator)</summary>
          <p className="small mono">{[...glob.data.domains, ...glob.data.emails].join(", ")}</p>
        </details>
      )}
    </div>
  );
}

function BulkSuppress({ campaigns, onDone }: { campaigns: Row[]; onDone: (text: string) => void }) {
  const client = useWs();
  const [text, setText] = useState("");
  const [reason, setReason] = useState("manual");
  const [scope, setScope] = useState("workspace");
  const [campaignId, setCampaignId] = useState("");
  const [expires, setExpires] = useState("");
  const action = useAction();
  const parsed = useMemo(() => parseBulkValues(text), [text]);
  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const r = await client.post<Json>("/suppression/bulk", {
            values: parsed.values,
            reason,
            scope,
            campaign_id: scope === "campaign" ? campaignId : undefined,
            expires_at: expires ? new Date(expires).toISOString() : undefined,
          });
          onDone(`Added ${num(r.added)}, ${num(r.existing)} already listed, ${num(r.invalid_count)} invalid`);
          setText("");
        });
      }}
    >
      <h3>Add addresses or domains</h3>
      <label className="field">
        <span className="field__label">One per line or comma separated. Use @domain.com for a whole domain.</span>
        <textarea className="input textarea" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={"jane@acme.com\n@competitor.com"} />
      </label>
      <p className="muted small">{parsed.values.length} valid{parsed.invalid.length ? `, ${parsed.invalid.length} not valid (${parsed.invalid.slice(0, 3).join(", ")})` : ""}</p>
      <div className="form-grid">
        <label className="field">
          <span className="field__label">Reason</span>
          <select className="input" value={reason} onChange={(e) => setReason(e.target.value)}>{REASONS.map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}</select>
        </label>
        <label className="field">
          <span className="field__label">Scope</span>
          <select className="input" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="workspace">Whole workspace</option>
            <option value="campaign">One campaign</option>
          </select>
        </label>
        {scope === "campaign" && (
          <label className="field">
            <span className="field__label">Campaign</span>
            <select className="input" required value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
              <option value="">Choose…</option>
              {campaigns.map((c) => <option key={c.id} value={c.id}>{String(c.name)}</option>)}
            </select>
          </label>
        )}
        <label className="field">
          <span className="field__label">Expires (optional)</span>
          <input className="input" type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </label>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button className="button button--primary" disabled={action.busy || parsed.values.length === 0 || (scope === "campaign" && !campaignId)}>Suppress {parsed.values.length || ""}</button>
      </div>
    </form>
  );
}

function CheckAddress() {
  const client = useWs();
  const [email, setEmail] = useState("");
  const [result, setResult] = useState<Json | null | undefined>(undefined);
  const action = useAction();
  return (
    <form
      className="send-inline"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const r = await client.get<{ suppressed: Json | null }>("/suppression/check", { email });
          setResult(r.suppressed);
        });
      }}
    >
      <input className="input input--small" type="email" required placeholder="Check an address…" aria-label="Check an address" value={email} onChange={(e) => { setEmail(e.target.value); setResult(undefined); }} />
      <button className="button button--ghost button--small" disabled={action.busy || !email}>Check</button>
      {result === null && <span className="small">Not suppressed</span>}
      {result && <span className="small">Suppressed: {String(result.scope)} {String(result.kind)} ({String(result.reason)})</span>}
    </form>
  );
}

// --- Campaign detail -------------------------------------------------------------------------

interface Performance {
  sent: number;
  delivered: number;
  replied: number;
  bounced: number;
  unsubscribed: number;
  opened: number | null;
  clicked: number | null;
  blocked: number;
  failed: number;
  reply_rate: number | null;
  bounce_rate: number | null;
  unsubscribe_rate: number | null;
  enrollments: Record<string, number>;
  outbound: Record<string, number>;
  events: Record<string, number>;
  opportunities: number;
}

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function CampaignDetail() {
  const { campaignId = "" } = useParams();
  const client = useWs();
  const [tick, setTick] = useState(0);
  const [tab, setTab] = useState("audience");
  const campaign = useLoad((s) => client.get<Row>(`/campaigns/${campaignId}`, undefined, s), client.base + campaignId + tick);
  const perf = useLoad((s) => client.get<Performance>(`/campaigns/${campaignId}/performance`, undefined, s), client.base + campaignId + "p" + tick);
  const readiness = useLoad((s) => client.get<{ ready: boolean; issues: string[]; notes: string[] }>(`/campaigns/${campaignId}/readiness`, undefined, s), client.base + campaignId + "r" + tick);
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const refresh = () => setTick((n) => n + 1);

  if (campaign.error) return <div className="page"><ErrorBanner error={campaign.error} /></div>;
  if (!campaign.data) return <div className="page"><Loading /></div>;
  const c = campaign.data;
  const status = String(c.status);

  const act = (label: string, fn: () => Promise<Json>) =>
    void action.run(async () => {
      const r = await fn();
      if (label === "Launched") {
        const notes = Array.isArray(r.notes) ? (r.notes as string[]).join(" ") : "";
        setDone(`Launched: ${num(r.enrolled)} enrolled (${num(r.pending_approval)} waiting for approval, ${num(r.approved)} approved). ${notes}`);
      } else setDone(label);
      setConfirmStop(false);
      refresh();
    });

  const save = (changes: Json, label = "Saved") => act(label, () => client.post<Json>(`/campaigns/${campaignId}/configure`, changes));
  const p = perf.data;
  return (
    <div className="page">
      <Link to="/campaigns" className="back">← Campaigns</Link>
      <PageHeader
        title={String(c.name)}
        crumbTitle={String(c.name)}
        subtitle={<><Pill value={status} /> {c.sending_enabled ? "Sending enabled" : "Sending off"}{c.description ? ` · ${String(c.description)}` : ""}</>}
        actions={
          <>
            {status === "draft" && (
              <button className="button button--primary" disabled={action.busy || !readiness.data?.ready} title={readiness.data?.issues.join("; ")} onClick={() => act("Launched", () => client.post<Json>(`/campaigns/${campaignId}/launch`))}>Launch</button>
            )}
            {status === "active" && <button className="button button--ghost" disabled={action.busy} onClick={() => act("Paused", () => client.post<Json>(`/campaigns/${campaignId}/status/pause`))}>Pause</button>}
            {status === "paused" && <button className="button button--primary" disabled={action.busy} onClick={() => act("Resumed", () => client.post<Json>(`/campaigns/${campaignId}/status/resume`))}>Resume</button>}
            {status !== "archived" &&
              (confirmStop ? (
                <button className="button button--danger" disabled={action.busy} onClick={() => act("Stopped", () => client.post<Json>(`/campaigns/${campaignId}/status/stop`))}>Confirm stop</button>
              ) : (
                <button className="button button--ghost" onClick={() => setConfirmStop(true)}>Stop</button>
              ))}
          </>
        }
      />
      {readiness.data && readiness.data.issues.length > 0 && <Notice tone="warning">Before launch: {readiness.data.issues.join("; ")}.</Notice>}
      {readiness.data?.notes.map((n) => <Notice key={n}>{n}</Notice>)}
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />
      <div className="stats">
        <Stat label="Sent" value={fmt(p?.sent ?? "—")} />
        <Stat label="Replies" value={fmt(p?.replied ?? "—")} hint={ratio(p?.reply_rate)} />
        <Stat label="Bounces" value={fmt(p?.bounced ?? "—")} hint={ratio(p?.bounce_rate)} />
        <Stat label="Unsubscribes" value={fmt(p?.unsubscribed ?? "—")} hint={ratio(p?.unsubscribe_rate)} />
        <Stat label="Opens" value={p?.opened === null || p?.opened === undefined ? "—" : fmt(p.opened)} hint={p && p.opened === null ? "not reported by provider" : undefined} />
        <Stat label="Blocked" value={fmt(p?.blocked ?? "—")} hint="gates closed" />
      </div>
      <Tabs
        tabs={[{ key: "audience", label: "Audience" }, { key: "senders", label: "Senders & sequence" }, { key: "schedule", label: "Schedule" }, { key: "performance", label: "Performance" }]}
        active={tab}
        onChange={setTab}
      />
      {tab === "audience" && <CampaignAudience campaign={c} onSave={save} tick={tick} />}
      {tab === "senders" && <CampaignSenders campaign={c} onSave={save} />}
      {tab === "schedule" && <CampaignSchedule campaign={c} onSave={save} />}
      {tab === "performance" && p && (
        <div className="grid-2">
          <div className="card"><h3>Messages</h3><KeyValues items={Object.entries(p.outbound).map(([k, v]) => [k, fmt(v)])} /></div>
          <div className="card"><h3>Enrollments</h3><KeyValues items={Object.entries(p.enrollments).map(([k, v]) => [k.replace(/_/g, " "), fmt(v)])} /></div>
          <div className="card"><h3>Events</h3><KeyValues items={Object.entries(p.events).map(([k, v]) => [k, fmt(v)])} /></div>
          <div className="card"><h3>Deals</h3><KeyValues items={[["opportunities", fmt(p.opportunities)]]} /></div>
        </div>
      )}
    </div>
  );
}

function CampaignAudience({ campaign, onSave, tick }: { campaign: Row; onSave: (changes: Json) => void; tick: number }) {
  const client = useWs();
  const audience = (campaign.audience ?? {}) as { list_ids?: string[]; segment_id?: string };
  const [lists, setLists] = useState<string[]>(audience.list_ids ?? []);
  const [segment, setSegment] = useState(audience.segment_id ?? "");
  const allLists = useLoad((s) => client.list("/lists", { limit: 200 }, s).catch(() => EMPTY_PAGE), client.base + "lists");
  const segments = useLoad((s) => client.list("/segments", { limit: 200 }, s).catch(() => EMPTY_PAGE), client.base + "segs");
  const preview = useLoad((s) => client.get<{ total: number; eligible: number; blocked: Record<string, number>; sample: Row[] }>(`/campaigns/${campaign.id}/audience`, undefined, s), client.base + campaign.id + "aud" + tick);
  const usable = (allLists.data?.items ?? []).filter((l) => l.entity_type === "contacts" || l.entity_type === "companies");
  return (
    <div className="grid-2">
      <div className="card">
        <h3>Who is in this campaign</h3>
        {usable.length === 0 ? (
          <EmptyState title="No contact or company lists" description="Create a list of contacts or companies, then choose it here." icon="list" action={<Link className="button button--ghost button--small" to="/lists">Open lists</Link>} />
        ) : (
          <fieldset className="send-checks">
            <legend className="field__label">Lists (company lists include their contacts)</legend>
            {usable.map((l) => (
              <label key={l.id} className="checkbox">
                <input type="checkbox" checked={lists.includes(l.id)} onChange={(e) => setLists(e.target.checked ? [...lists, l.id] : lists.filter((id) => id !== l.id))} />
                {String(l.name)} <span className="muted small">({String(l.entity_type)}, {fmt(l.member_count)})</span>
              </label>
            ))}
          </fieldset>
        )}
        <label className="field">
          <span className="field__label">Segment (optional)</span>
          <select className="input" value={segment} onChange={(e) => setSegment(e.target.value)}>
            <option value="">None</option>
            {(segments.data?.items ?? []).filter((sg) => sg.entity_type === "contacts" || sg.entity_type === "companies").map((sg) => <option key={sg.id} value={sg.id}>{String(sg.name)}</option>)}
          </select>
        </label>
        <div className="form__actions">
          <button className="button button--primary" onClick={() => onSave({ audience: { ...audience, list_ids: lists, segment_id: segment || undefined } })}>Save audience</button>
        </div>
      </div>
      <div className="card">
        <h3>Preview</h3>
        {!preview.data ? (
          <Loading />
        ) : (
          <>
            <KeyValues items={[["resolved contacts", fmt(preview.data.total)], ["eligible to email", fmt(preview.data.eligible)], ...Object.entries(preview.data.blocked).map(([k, v]) => [`blocked: ${k}`, fmt(v)] as [string, ReactNode])]} />
            <DataTable
              rows={preview.data.sample}
              empty={{ title: "Nobody eligible yet", description: "Save an audience with contacts that have deliverable, unsuppressed emails." }}
              link={(r) => `/contacts/${r.id}`}
              columns={[
                { key: "full_name", label: "Contact" },
                { key: "email", label: "Email", className: "mono small" },
                { key: "email_status", label: "Email status", render: (r) => <Pill value={r.email_status} /> },
              ]}
            />
          </>
        )}
      </div>
    </div>
  );
}

function CampaignSenders({ campaign, onSave }: { campaign: Row; onSave: (changes: Json, label?: string) => void }) {
  const client = useWs();
  const admin = useIsAdmin();
  const [boxes, setBoxes] = useState<string[]>((campaign.mailbox_ids as string[]) ?? []);
  const [sequence, setSequence] = useState(String(campaign.default_sequence_id ?? ""));
  const [policy, setPolicy] = useState(String(campaign.approval_policy ?? "manual"));
  const [templates, setTemplates] = useState<string[]>((campaign.template_ids as string[]) ?? []);
  const mailboxes = useLoad((s) => client.get<{ items: Row[] }>("/mailboxes", undefined, s), client.base + "mbx");
  const sequences = useLoad((s) => client.list("/sequences", { limit: 200 }, s), client.base + "seqs");
  const allTemplates = useLoad((s) => client.list("/templates", { limit: 200 }, s), client.base + "tpls");
  return (
    <div className="grid-2">
      <div className="card">
        <h3>Senders</h3>
        {(mailboxes.data?.items ?? []).length === 0 ? (
          <EmptyState title="No mailboxes" description="Connect a mailbox in Email & Sending. Without one, messages are blocked." icon="mail" action={<Link className="button button--ghost button--small" to="/settings/sending">Email & Sending</Link>} />
        ) : (
          <fieldset className="send-checks">
            <legend className="field__label">Rotate between (most remaining capacity first). None selected = the default sender.</legend>
            {(mailboxes.data?.items ?? []).map((m) => (
              <label key={m.id} className="checkbox">
                <input type="checkbox" checked={boxes.includes(m.id)} onChange={(e) => setBoxes(e.target.checked ? [...boxes, m.id] : boxes.filter((id) => id !== m.id))} />
                {String(m.address)} <Pill value={m.status} /> <span className="muted small">{num(m.daily_limit)}/day</span>
              </label>
            ))}
          </fieldset>
        )}
        <label className="field">
          <span className="field__label">Sequence</span>
          <select className="input" value={sequence} onChange={(e) => setSequence(e.target.value)}>
            <option value="">Choose…</option>
            {(sequences.data?.items ?? []).map((sq) => <option key={sq.id} value={sq.id}>{String(sq.name)}</option>)}
          </select>
        </label>
        {sequence && <p className="small"><Link className="link" to={`/sequences/${sequence}`}>Edit this sequence's steps</Link></p>}
        <label className="field">
          <span className="field__label">Approval</span>
          <select className="input" value={policy} onChange={(e) => setPolicy(e.target.value)}>
            <option value="manual">Every enrollment is approved by a person</option>
            <option value="auto_after_review">Approved by the person who launches (after reviewing the audience)</option>
          </select>
        </label>
        <div className="form__actions">
          <button className="button button--primary" onClick={() => onSave({ mailbox_ids: boxes, default_sequence_id: sequence || null, approval_policy: policy, template_ids: templates })}>Save</button>
        </div>
      </div>
      <div className="card">
        <h3>Templates</h3>
        <fieldset className="send-checks">
          <legend className="field__label">Templates used by this campaign</legend>
          {(allTemplates.data?.items ?? []).map((t) => (
            <label key={t.id} className="checkbox">
              <input type="checkbox" checked={templates.includes(t.id)} onChange={(e) => setTemplates(e.target.checked ? [...templates, t.id] : templates.filter((id) => id !== t.id))} />
              {String(t.name)} <span className="muted small">{String(t.subject).slice(0, 60)}</span>
            </label>
          ))}
        </fieldset>
        <h3>Sending</h3>
        <p className="muted small">Even with sending enabled, nothing leaves the platform unless the environment allows sending and each enrollment is approved.</p>
        <button className={`button ${campaign.sending_enabled ? "button--ghost" : "button--primary"}`} disabled={!admin} onClick={() => onSave({ sending_enabled: !campaign.sending_enabled }, campaign.sending_enabled ? "Sending disabled" : "Sending enabled")}>
          {campaign.sending_enabled ? "Disable sending" : "Enable sending"}
        </button>
        {!admin && <p className="muted small">Only an admin can change this.</p>}
      </div>
    </div>
  );
}

function CampaignSchedule({ campaign, onSave }: { campaign: Row; onSave: (changes: Json) => void }) {
  const initial = (campaign.schedule ?? {}) as Schedule;
  const [tz, setTz] = useState(initial.timezone ?? "America/New_York");
  const [days, setDays] = useState<number[]>(initial.days ?? [1, 2, 3, 4, 5]);
  const [start, setStart] = useState(initial.start_hour ?? 9);
  const [end, setEnd] = useState(initial.end_hour ?? 17);
  const [cap, setCap] = useState(initial.daily_cap ?? 0);
  const schedule: Schedule = { timezone: tz, days, start_hour: start, end_hour: end, daily_cap: cap || undefined };
  return (
    <div className="card">
      <h3>Sending window</h3>
      <p className="muted small">Current: {scheduleSummary(initial)}. Steps due outside the window wait for the next window start.</p>
      <div className="form-grid">
        <label className="field">
          <span className="field__label">Time zone</span>
          <input className="input" value={tz} onChange={(e) => setTz(e.target.value)} placeholder="America/New_York" />
        </label>
        <label className="field">
          <span className="field__label">From hour</span>
          <input className="input" type="number" min={0} max={23} value={start} onChange={(e) => setStart(Number(e.target.value))} />
        </label>
        <label className="field">
          <span className="field__label">Until hour</span>
          <input className="input" type="number" min={1} max={24} value={end} onChange={(e) => setEnd(Number(e.target.value))} />
        </label>
        <label className="field">
          <span className="field__label">Daily cap (0 = none)</span>
          <input className="input" type="number" min={0} value={cap} onChange={(e) => setCap(Number(e.target.value))} />
        </label>
      </div>
      <fieldset className="send-days">
        <legend className="field__label">Days</legend>
        {DAY_LABELS.map((label, i) => (
          <label key={label} className="checkbox">
            <input type="checkbox" checked={days.includes(i + 1)} onChange={(e) => setDays(e.target.checked ? [...days, i + 1].sort((a, b) => a - b) : days.filter((d) => d !== i + 1))} />
            {label}
          </label>
        ))}
      </fieldset>
      <p className="small">Will be: {scheduleSummary(schedule)}</p>
      <div className="form__actions">
        <button className="button button--primary" disabled={days.length === 0 || start >= end} onClick={() => onSave({ schedule })}>Save schedule</button>
        <button className="button button--ghost" onClick={() => onSave({ schedule: {} })}>Any time</button>
      </div>
    </div>
  );
}

// --- Sequence detail: the step editor -------------------------------------------------------

interface StepDraft extends EditableStep {
  key: string;
  subject_override?: string | null;
  instructions?: string | null;
  only_if_no_reply?: boolean;
}

interface Overview {
  sequence: Row;
  steps: Row[];
  days: number[];
  stop_conditions: Record<string, boolean>;
  locked_stop_conditions: string[];
  enrollments: Record<string, number>;
  events: Record<string, number>;
}

let draftCounter = 0;
function nextKey(): string {
  draftCounter += 1;
  return `s${draftCounter}`;
}

function draftFrom(step: Row): StepDraft {
  return {
    key: nextKey(),
    channel: String(step.channel ?? "email"),
    delay_days: num(step.delay_days),
    step_type: String(step.step_type ?? "initial"),
    template_id: (step.template_id as string | null) ?? null,
    subject_override: (step.subject_override as string | null) ?? null,
    instructions: (step.instructions as string | null) ?? null,
    only_if_no_reply: Boolean((step.condition as Json | undefined)?.only_if_no_reply),
  };
}

const STOP_LABELS: Record<string, string> = {
  reply: "A reply is received",
  unsubscribe: "The contact unsubscribes",
  bounce: "A permanent bounce",
  contact_disabled: "The contact is disabled (do not contact, left company)",
  campaign_stopped: "The campaign is stopped",
  suppressed: "A suppression rule matches",
};

export function SequenceDetail() {
  const { sequenceId = "" } = useParams();
  const client = useWs();
  const [tick, setTick] = useState(0);
  const overview = useLoad((s) => client.get<Overview>(`/sequences/${sequenceId}/overview`, undefined, s), client.base + sequenceId + tick);
  const templates = useLoad((s) => client.list("/templates", { limit: 200 }, s), client.base + "tpl");
  const [steps, setSteps] = useState<StepDraft[] | null>(null);
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  const refresh = () => setTick((n) => n + 1);

  useEffect(() => {
    if (overview.data) setSteps(overview.data.steps.map(draftFrom));
  }, [overview.data]);

  if (overview.error) return <div className="page"><ErrorBanner error={overview.error} /></div>;
  if (!overview.data || !steps) return <div className="page"><Loading /></div>;
  const o = overview.data;
  const problems = validateSteps(steps);
  const days = cadenceDays(steps.map((s) => s.delay_days));
  const tpl = templates.data?.items ?? [];
  const update = (i: number, changes: Partial<StepDraft>) => setSteps(steps.map((s, j) => (j === i ? { ...s, ...changes } : s)));
  const add = (channel: string) => {
    const first = steps.length === 0;
    setSteps([
      ...steps,
      {
        key: nextKey(),
        channel,
        delay_days: first ? 0 : channel === "wait" ? 1 : 2,
        step_type: channel === "email" ? (first ? "initial" : "follow_up") : channel === "wait" ? "wait" : "task",
        template_id: channel === "email" ? tpl[0]?.id ?? null : null,
        only_if_no_reply: channel === "email" && !first,
      },
    ]);
  };
  const save = () =>
    void action.run(async () => {
      await client.put(`/sequences/${sequenceId}/steps`, {
        steps: steps.map((s) => ({
          channel: s.channel,
          delay_days: s.delay_days,
          step_type: s.step_type,
          template_id: s.template_id,
          subject_override: s.subject_override || null,
          instructions: s.instructions || null,
          condition: s.only_if_no_reply ? { only_if_no_reply: true } : {},
        })),
      });
      setDone("Steps saved");
      refresh();
    });
  const cadence = () =>
    void action.run(async () => {
      const chosen = steps.filter((s) => s.channel === "email" && s.template_id).map((s) => s.template_id as string);
      const ids = chosen.length ? chosen : tpl.slice(0, 1).map((t) => t.id);
      await client.post(`/sequences/${sequenceId}/cadence`, { template_ids: ids });
      setDone("Day 1 / 3 / 6 / 10 cadence applied");
      refresh();
    });

  return (
    <div className="page">
      <Link to="/sequences" className="back">← Sequences</Link>
      <PageHeader
        title={String(o.sequence.name)}
        crumbTitle={String(o.sequence.name)}
        subtitle={<><Pill value={o.sequence.status} /> {o.sequence.campaign_id ? <Link className="link" to={`/campaigns/${String(o.sequence.campaign_id)}`}>campaign</Link> : "no campaign"} · enrollments wait for approval; nothing is sent outside production.</>}
        actions={<button className="button button--primary" disabled={action.busy || problems.length > 0} onClick={save}>Save steps</button>}
      />
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />
      <div className="stats">
        <Stat label="Steps" value={steps.length} hint={steps.length ? `Day ${days.join(", ")}` : undefined} />
        <Stat label="Active" value={fmt(o.enrollments.active ?? 0)} />
        <Stat label="Waiting approval" value={fmt(o.enrollments.pending_approval ?? 0)} />
        <Stat label="Replied" value={fmt(o.enrollments.replied ?? 0)} />
        <Stat label="Sent" value={fmt(o.events.sent ?? 0)} />
      </div>

      <div className="card">
        <div className="send-editor__head">
          <h3>Steps</h3>
          <div className="actions">
            <button className="button button--ghost button--small" onClick={() => add("email")}>+ Email</button>
            <button className="button button--ghost button--small" onClick={() => add("wait")}>+ Wait</button>
            <button className="button button--ghost button--small" onClick={() => add("task")}>+ Task</button>
            <button className="button button--ghost button--small" onClick={() => add("call")}>+ Call</button>
            <button className="button button--ghost button--small" disabled={action.busy || tpl.length === 0} onClick={cadence} title="Replaces the steps with emails on Day 1, 3, 6 and 10">Use Day 1/3/6/10</button>
          </div>
        </div>
        {tpl.length === 0 && <Notice tone="warning">Create an email template first: <Link className="link" to="/templates">Templates</Link>.</Notice>}
        {steps.length === 0 ? (
          <EmptyState title="No steps yet" description="Add an initial email, waits and follow-ups, or start from the Day 1 / 3 / 6 / 10 cadence." icon="repeat" />
        ) : (
          <ol className="send-steps">
            {steps.map((s, i) => (
              <li key={s.key} className="send-step">
                <div className="send-step__day">Day {days[i]}</div>
                <div className="send-step__body">
                  <div className="send-step__row">
                    <select className="input input--small" aria-label={`Step ${i + 1} channel`} value={s.channel} onChange={(e) => update(i, { channel: e.target.value, step_type: e.target.value === "email" ? "follow_up" : e.target.value === "wait" ? "wait" : "task" })}>
                      <option value="email">Send email</option>
                      <option value="wait">Wait</option>
                      <option value="task">Task</option>
                      <option value="call">Call task</option>
                      <option value="linkedin_task">LinkedIn task</option>
                    </select>
                    {s.channel === "email" && (
                      <select className="input input--small" aria-label={`Step ${i + 1} type`} value={s.step_type} onChange={(e) => update(i, { step_type: e.target.value })}>
                        <option value="initial">Initial</option>
                        <option value="follow_up">Follow-up</option>
                        <option value="final">Final</option>
                      </select>
                    )}
                    <label className="send-inline small">
                      {i === 0 ? "Start after" : "Then wait"}
                      <input className="input input--small send-num" type="number" min={0} max={365} aria-label={`Step ${i + 1} delay in days`} value={s.delay_days} onChange={(e) => update(i, { delay_days: Math.max(0, Number(e.target.value)) })} />
                      days
                    </label>
                    <span className="send-step__tools">
                      <button className="button button--ghost button--small" aria-label="Move up" disabled={i === 0} onClick={() => setSteps(moveStep(steps, i, -1))}>↑</button>
                      <button className="button button--ghost button--small" aria-label="Move down" disabled={i === steps.length - 1} onClick={() => setSteps(moveStep(steps, i, 1))}>↓</button>
                      <button className="button button--ghost button--small" aria-label="Remove step" onClick={() => setSteps(steps.filter((_, j) => j !== i))}>Remove</button>
                    </span>
                  </div>
                  {s.channel === "email" && (
                    <div className="send-step__row">
                      <select className="input input--small" aria-label={`Step ${i + 1} template`} value={s.template_id ?? ""} onChange={(e) => update(i, { template_id: e.target.value || null })}>
                        <option value="">Choose template…</option>
                        {tpl.map((t) => <option key={t.id} value={t.id}>{String(t.name)}</option>)}
                      </select>
                      <input className="input input--small send-grow" placeholder="Subject override (optional, e.g. Re: {{company.name}})" aria-label={`Step ${i + 1} subject override`} value={s.subject_override ?? ""} onChange={(e) => update(i, { subject_override: e.target.value })} />
                      {i > 0 && (
                        <label className="checkbox small">
                          <input type="checkbox" checked={Boolean(s.only_if_no_reply)} onChange={(e) => update(i, { only_if_no_reply: e.target.checked })} />
                          only if no reply
                        </label>
                      )}
                    </div>
                  )}
                  {s.channel !== "email" && s.channel !== "wait" && (
                    <input className="input input--small send-grow" placeholder="Instructions for the task" aria-label={`Step ${i + 1} instructions`} value={s.instructions ?? ""} onChange={(e) => update(i, { instructions: e.target.value })} />
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
        {problems.length > 0 && steps.length > 0 && <Notice tone="warning">{problems.join(" ")}</Notice>}
        <p className="muted small">
          Variables: {"{{contact.first_name}} {{contact.title}} {{company.name}} {{company.industry}} {{job.title}} {{signal.summary}} {{sender.name}} {{unsubscribe.url}}"}. A missing value blocks that message instead of sending a blank.
        </p>
      </div>

      <div className="grid-2">
        <StopConditions sequenceId={sequenceId} overview={o} onSaved={refresh} />
        <Preview templates={tpl} />
      </div>
      <Enrollments sequenceId={sequenceId} tick={tick} onChange={refresh} />
    </div>
  );
}

function StopConditions({ sequenceId, overview, onSaved }: { sequenceId: string; overview: Overview; onSaved: () => void }) {
  const client = useWs();
  const [values, setValues] = useState(overview.stop_conditions);
  const action = useAction();
  return (
    <div className="card">
      <h3>Stop the sequence when…</h3>
      <fieldset className="send-checks">
        <legend className="sr-only">Stop conditions</legend>
        {Object.keys(STOP_LABELS).map((key) => {
          const locked = overview.locked_stop_conditions.includes(key);
          return (
            <label key={key} className="checkbox">
              <input type="checkbox" disabled={locked} checked={locked || Boolean(values[key])} onChange={(e) => setValues({ ...values, [key]: e.target.checked })} />
              {STOP_LABELS[key]} {locked && <span className="muted small">(always, for compliance)</span>}
            </label>
          );
        })}
      </fieldset>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button className="button button--ghost" disabled={action.busy} onClick={() => void action.run(async () => { await client.post(`/sequences/${sequenceId}/stop-conditions`, values); onSaved(); })}>Save stop conditions</button>
      </div>
    </div>
  );
}

function Preview({ templates }: { templates: Row[] }) {
  const client = useWs();
  const [templateId, setTemplateId] = useState("");
  const [contactId, setContactId] = useState("");
  const [result, setResult] = useState<Json | null>(null);
  const action = useAction();
  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => setResult(await client.post<Json>(`/templates/${templateId}/preview`, { contact_id: contactId })));
      }}
    >
      <h3>Preview for a contact</h3>
      <div className="send-inline">
        <select className="input input--small" aria-label="Template" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
          <option value="">Template…</option>
          {templates.map((t) => <option key={t.id} value={t.id}>{String(t.name)}</option>)}
        </select>
        <input className="input input--small" placeholder="Contact id (ct_…)" aria-label="Contact id" value={contactId} onChange={(e) => setContactId(e.target.value.trim())} />
        <button className="button button--ghost button--small" disabled={action.busy || !templateId || !contactId}>Preview</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {result && (
        <div className="send-preview">
          <p><strong>To:</strong> {fmt(result.to)} {result.block_reason ? <Pill value="blocked" /> : null}</p>
          {result.block_reason ? <p className="small">Would not be sent: {String(result.block_reason)}</p> : null}
          <p><strong>Subject:</strong> {String(result.subject)}</p>
          <pre className="send-preview__body">{String(result.body)}</pre>
        </div>
      )}
    </form>
  );
}

function Enrollments({ sequenceId, tick, onChange }: { sequenceId: string; tick: number; onChange: () => void }) {
  const client = useWs();
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [ids, setIds] = useState("");
  const [listId, setListId] = useState("");
  const rows = useLoad((s) => client.list("/enrollments", { sequence_id: sequenceId, status: status || undefined, limit: 100 }, s), client.base + sequenceId + status + tick);
  const lists = useLoad((s) => client.list("/lists", { entity_type: "contacts", limit: 200 }, s).catch(() => EMPTY_PAGE), client.base + "clists");
  const action = useAction();
  const [done, setDone] = useState<string | null>(null);
  const enroll = () =>
    void action.run(async () => {
      let contactIds = ids.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      if (listId) {
        const members = await client.get<{ items: { entity_id: string }[] }>(`/lists/${listId}/members`, { limit: 500 });
        contactIds = [...contactIds, ...members.items.map((m) => m.entity_id)];
      }
      const r = await client.post<{ results: { status: string }[] }>(`/sequences/${sequenceId}/enroll`, { contact_ids: contactIds });
      const enrolled = r.results.filter((x) => x.status === "enrolled").length;
      setDone(`${enrolled} enrolled (waiting for approval), ${r.results.length - enrolled} skipped`);
      setIds("");
      onChange();
    });
  const approve = () =>
    void action.run(async () => {
      const r = await client.post<{ approved: Row[] }>("/enrollments/approve", { enrollment_ids: selected });
      setDone(`${r.approved.length} approved`);
      setSelected([]);
      onChange();
    });
  const stop = () =>
    void action.run(async () => {
      for (const id of selected) await client.post(`/enrollments/${id}/stop-with-reason`, { reason: "stopped by a user" });
      setDone(`${selected.length} stopped`);
      setSelected([]);
      onChange();
    });
  const pending = (rows.data?.items ?? []).filter((r) => r.status === "pending_approval").map((r) => r.id);
  return (
    <div className="card">
      <div className="send-editor__head">
        <h3>Enrollments</h3>
        <div className="actions">
          <select className="input input--small" aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {["pending_approval", "active", "paused", "completed", "replied", "bounced", "unsubscribed", "suppressed", "stopped"].map((st) => <option key={st} value={st}>{st.replace(/_/g, " ")}</option>)}
          </select>
          <button className="button button--primary button--small" disabled={action.busy || selected.filter((id) => pending.includes(id)).length === 0} onClick={approve}>Approve selected</button>
          <button className="button button--ghost button--small" disabled={action.busy || selected.length === 0} onClick={stop}>Stop selected</button>
        </div>
      </div>
      <div className="send-inline send-enroll">
        <select className="input input--small" aria-label="Enroll a contact list" value={listId} onChange={(e) => setListId(e.target.value)}>
          <option value="">Contact list…</option>
          {(lists.data?.items ?? []).map((l) => <option key={l.id} value={l.id}>{String(l.name)}</option>)}
        </select>
        <input className="input input--small send-grow" placeholder="…or contact ids (ct_…), comma separated" aria-label="Contact ids" value={ids} onChange={(e) => setIds(e.target.value)} />
        <button className="button button--ghost button--small" disabled={action.busy || (!ids.trim() && !listId)} onClick={enroll}>Enroll</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <Done text={done} />
      {!rows.data ? (
        <Loading />
      ) : (
        <DataTable
          rows={rows.data.items}
          empty={{ title: "No enrollments", description: "Enroll contacts above. Suppressed, unsubscribed and invalid contacts are skipped with the reason.", icon: "users" }}
          columns={[
            { key: "select", label: "", render: (r) => <input type="checkbox" aria-label="Select enrollment" checked={selected.includes(r.id)} onChange={(e) => setSelected(e.target.checked ? [...selected, r.id] : selected.filter((id) => id !== r.id))} /> },
            { key: "contact_id", label: "Contact", render: (r) => <Link className="link" to={`/contacts/${String(r.contact_id)}`}>{String(r.contact_id)}</Link> },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "current_step", label: "Step", render: (r) => num(r.current_step) + 1 },
            { key: "next_step_at", label: "Next step", render: (r) => fmt(r.next_step_at) },
            { key: "stop_reason", label: "Stop reason", render: (r) => <span className="muted small">{fmt(r.stop_reason)}</span> },
          ]}
        />
      )}
    </div>
  );
}
