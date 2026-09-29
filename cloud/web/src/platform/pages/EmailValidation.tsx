// Email Validation: upload a CSV/XLSX (or validate a contact list), pick the email
// column, run the built-in checks (plus EmailListVerify only when it is configured
// AND verified), watch progress, filter results, export, and — only on request —
// add the results to a list, a draft campaign or a sequence (pending approval).

import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { Icon } from "../../shell/Icon";
import type { PageOf, Row } from "../api";
import { fileSize } from "../logic/format";
import {
  RESULT_TABS,
  exportPath,
  initialColumn,
  isActive,
  itemQuery,
  progressPercent,
  providerState,
  reasonFor,
  tabCount,
  uploadProblem,
  type Candidate,
  type Counts,
  type ItemFilters,
} from "../logic/emailValidation";
import { DataTable, KeyValues, PageHeader, Pill, ResourceList, Score, Stat, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import "../styles/emailValidation.css";

interface ProviderStatus {
  local: { status: string; checks: { key: string; label: string; detail: string }[]; note: string };
  emaillistverify: {
    status: string;
    configured: boolean;
    verified?: boolean;
    secret_hint?: string | null;
    last_checked_at?: string | null;
    last_error?: string | null;
    cost_per_check: number;
    credits?: { known?: boolean; remaining?: number; consumed?: number };
    usage?: { calls: number; units: number; failures: number };
    requirement: string;
  };
}

type Job = Row & {
  name: string;
  status: string;
  columns: string[];
  email_column: string | null;
  preview: Record<string, unknown>[];
  row_count: number;
  size_bytes?: number | null;
  filename?: string | null;
  source_type: string;
  counts: Counts;
  settings: { candidates?: Candidate[]; problems?: string[]; allow_paid?: boolean; max_age_days?: number };
  task?: { status: string; progress?: Record<string, unknown>; error?: string | null } | null;
  error?: string | null;
};

// --- provider panel ------------------------------------------------------------------

function ProviderPanel({ compact = false }: { compact?: boolean }) {
  const client = useWs();
  const { data, error, refresh } = useLoad((signal) => client.get<ProviderStatus>("/email/provider", undefined, signal), client.base + "/email/provider");
  const test = useAction();
  const [result, setResult] = useState<string | null>(null);
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (!data) return <Loading label="Checking validation providers…" />;
  const elv = data.emaillistverify;
  const state = providerState(elv);
  return (
    <div className={`ev-providers${compact ? " ev-providers--compact" : ""}`}>
      <div className="card pad ev-provider">
        <div className="ev-provider__head">
          <h3>Built-in validation</h3>
          <span className="badge badge--completed"><span className="badge__dot" aria-hidden="true" />Active</span>
        </div>
        <ul className="ev-checks">
          {data.local.checks.map((c) => (
            <li key={c.key}><Icon name="check" size={14} /> <strong>{c.label}</strong> <span className="muted small">— {c.detail}</span></li>
          ))}
        </ul>
        <p className="muted small">{data.local.note}</p>
      </div>
      <div className="card pad ev-provider">
        <div className="ev-provider__head">
          <h3>EmailListVerify (external)</h3>
          <span className={`badge badge--${state.tone === "active" ? "completed" : state.tone === "warn" ? "cancelled" : "queued"}`}>
            <span className="badge__dot" aria-hidden="true" />{state.label}
          </span>
        </div>
        {state.active ? (
          <KeyValues items={[
            ["Key", elv.secret_hint ? `…${elv.secret_hint}` : "stored"],
            ["Cost", `${elv.cost_per_check} credit per checked address`],
            ["Credits left", elv.credits?.known ? fmt(elv.credits.remaining) : "unknown — sync the balance under Settings → Credits"],
            ["Checks run", fmt(elv.usage?.calls ?? 0)],
            ["Last verified", fmt(elv.last_checked_at)],
          ]} />
        ) : (
          <p className="muted small">
            {elv.configured
              ? "A key is stored but has not been verified, so no paid checks run. Test the connection to verify it."
              : `Not connected. Mailbox-level checks (the only way to mark an address VALID) need ${elv.requirement}. Built-in validation works without it.`}
          </p>
        )}
        {elv.last_error && !state.active && <p className="error-text small">{elv.last_error}</p>}
        <div className="actions">
          {elv.configured && (
            <button type="button" className="button button--ghost button--small" disabled={test.busy}
              onClick={() => test.run(async () => {
                const r = await client.post<{ status: string; detail?: string }>("/email/provider/test");
                setResult(`${r.status}${r.detail ? ` — ${r.detail}` : ""}`);
                refresh();
              })}>
              Test connection
            </button>
          )}
          <Link className="button button--ghost button--small" to="/sources">Manage providers</Link>
        </div>
        {result && <p className="muted small">Test result: {result}</p>}
        {test.error && <ErrorBanner error={test.error} />}
      </div>
    </div>
  );
}

// --- landing: upload, list validation, history -----------------------------------------

function Dropzone({ onFile, busy }: { onFile: (file: File) => void; busy: boolean }) {
  const [over, setOver] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const take = (file: File | undefined) => {
    if (!file) return;
    const issue = uploadProblem(file.name, file.size);
    setProblem(issue);
    if (!issue) onFile(file);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    take(e.dataTransfer.files?.[0]);
  };
  return (
    <div
      className={`ev-drop${over ? " ev-drop--over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
      onClick={() => input.current?.click()}
      aria-label="Upload a CSV or XLSX file"
    >
      <Icon name="upload" size={24} />
      <p><strong>{busy ? "Uploading…" : "Drop a CSV or XLSX file here"}</strong></p>
      <p className="muted small">or <span className="link">browse</span> · up to 25 MB and 50,000 rows</p>
      <input ref={input} type="file" accept=".csv,.xlsx" hidden onChange={(e) => { take(e.target.files?.[0]); e.target.value = ""; }} />
      {problem && <p className="error-text small">{problem}</p>}
    </div>
  );
}

function ListValidation() {
  const client = useWs();
  const navigate = useNavigate();
  const lists = useLoad((signal) => client.list("/lists", { entity_type: "contacts", limit: 200, order: "name" }, signal), client.base + "/lists/contacts");
  const [listId, setListId] = useState("");
  const action = useAction();
  return (
    <div className="card pad">
      <h3>Validate a contact list</h3>
      <p className="muted small">Checks the email address of every contact in a list. Contacts' email status is updated; nothing else in the CRM changes.</p>
      <div className="form--inline ev-inline">
        <select className="input" value={listId} onChange={(e) => setListId(e.target.value)} aria-label="Contact list">
          <option value="">Choose a contact list…</option>
          {(lists.data?.items ?? []).map((l) => <option key={l.id} value={l.id}>{String(l.name)} ({fmt(l.member_count)})</option>)}
        </select>
        <button type="button" className="button button--primary" disabled={!listId || action.busy}
          onClick={() => action.run(async () => {
            const name = (lists.data?.items ?? []).find((l) => l.id === listId)?.name;
            const job = await client.post<Job>("/email/jobs", { source: "list", list_id: listId, name: `List: ${String(name ?? listId)}` });
            navigate(`/email-validation/${job.id}`);
          })}>
          Review and validate
        </button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

export function EmailValidation() {
  const client = useWs();
  const navigate = useNavigate();
  const upload = useAction();
  return (
    <div className="page">
      <PageHeader title="Email Validation" subtitle="Check addresses before they reach a campaign: syntax, domain/MX, disposable, role and free-provider checks, plus EmailListVerify when it is connected and verified. Nothing is ever sent." />
      <div className="grid-2">
        <div className="card pad">
          <h3>Upload a file</h3>
          <Dropzone busy={upload.busy} onFile={(file) => upload.run(async () => {
            const job = await client.upload<Job>("/email/jobs/upload", [file], { name: file.name });
            navigate(`/email-validation/${job.id}`);
          })} />
          {upload.error && <ErrorBanner error={upload.error} />}
        </div>
        <ListValidation />
      </div>
      <h2 className="section-title">Validation providers</h2>
      <ProviderPanel />
      <h2 className="section-title">History</h2>
      <ResourceList
        load={(q, s) => client.list("/email/jobs", q, s)}
        link={(r) => `/email-validation/${r.id}`}
        filters={[{ key: "status", label: "Status", options: ["uploaded", "ready", "queued", "running", "paused", "completed", "cancelled", "failed"] }, { key: "source_type", label: "Source", options: ["upload", "list", "contacts", "scrape", "manual"] }]}
        columns={[
          { key: "name", label: "Job" },
          { key: "source_type", label: "Source", render: (r) => <Pill value={r.source_type} /> },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "row_count", label: "Rows", className: "tabular" },
          { key: "counts", label: "Valid / Invalid / Unknown", render: (r) => { const c = (r.counts ?? {}) as Counts; return <span className="tabular">{fmt(c.VALID ?? 0)} / {fmt(c.INVALID ?? 0)} / {fmt(c.UNKNOWN ?? 0)}</span>; } },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
        ]}
        empty={{ title: "No validation jobs yet", description: "Upload a CSV or XLSX file, or validate a contact list, to see results here.", icon: "mailcheck" }}
      />
    </div>
  );
}

// --- a job ----------------------------------------------------------------------------

function ColumnStep({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const client = useWs();
  const [column, setColumn] = useState(() => initialColumn(job.email_column, job.settings.candidates, job.columns));
  const [allowPaid, setAllowPaid] = useState(false);
  const [maxAge, setMaxAge] = useState(30);
  const provider = useLoad((signal) => client.get<ProviderStatus>("/email/provider", undefined, signal), client.base + "/email/provider/step");
  const action = useAction();
  const candidates = job.settings.candidates ?? [];
  const paidActive = providerState(provider.data?.emaillistverify).active;
  return (
    <>
      <div className="card pad">
        <h3>1 · Email column</h3>
        {candidates.length === 0 && job.source_type === "upload" && <p className="alert alert--warning">No column looks like it holds email addresses. Choose one below.</p>}
        {candidates.length > 1 && !job.email_column && <p className="alert alert--info">More than one column could hold emails. Choose the one to validate.</p>}
        <div className="ev-columns" role="radiogroup" aria-label="Email column">
          {job.columns.map((c) => {
            const cand = candidates.find((x) => x.column === c);
            return (
              <label key={c} className={`choice${column === c ? " choice--selected" : ""}`}>
                <input type="radio" name="email-column" value={c} checked={column === c} onChange={() => setColumn(c)} />
                <span className="choice__body">
                  <span className="choice__title">{c}</span>
                  {cand && <span className="muted small">Likely email column · {cand.reason}</span>}
                </span>
              </label>
            );
          })}
        </div>
        <h4 className="ev-subhead">Preview</h4>
        <div className="table-wrap">
          <table className="table">
            <thead><tr>{job.columns.map((c) => <th key={c} className={c === column ? "ev-col--chosen" : undefined}>{c}</th>)}</tr></thead>
            <tbody>
              {job.preview.map((row, i) => (
                <tr key={i}>{job.columns.map((c) => <td key={c} className={c === column ? "ev-col--chosen" : undefined}>{fmt(row[c])}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
        {(job.settings.problems ?? []).length > 0 && <p className="muted small">File notes: {(job.settings.problems ?? []).join("; ")}</p>}
      </div>
      <div className="card pad">
        <h3>2 · Checks</h3>
        <ProviderPanel compact />
        <label className="check ev-paid">
          <input type="checkbox" checked={allowPaid && paidActive} disabled={!paidActive} onChange={(e) => setAllowPaid(e.target.checked)} />
          <span>
            Use EmailListVerify for addresses the built-in checks cannot decide
            {!paidActive && <span className="muted small"> — unavailable: the provider is not configured and verified</span>}
            {paidActive && <span className="muted small"> — spends about {provider.data?.emaillistverify.cost_per_check ?? 1} credit per undecided address</span>}
          </span>
        </label>
        <label className="field ev-age">
          <span className="field__label">Reuse results checked in the last (days)</span>
          <input className="input input--small" type="number" min={0} max={365} value={maxAge} onChange={(e) => setMaxAge(Number(e.target.value) || 0)} />
          <span className="field__hint">Cached results are free and are never paid for twice.</span>
        </label>
        <div className="form__actions">
          <button type="button" className="button button--primary" disabled={!column || action.busy}
            onClick={() => action.run(async () => {
              if (column !== job.email_column) await client.post(`/email/jobs/${job.id}/column`, { column });
              await client.post(`/email/jobs/${job.id}/start`, { settings: { allow_paid: allowPaid && paidActive, max_age_days: maxAge } });
              onChanged();
            })}>
            Validate {fmt(job.row_count)} row{job.row_count === 1 ? "" : "s"}
          </button>
        </div>
        {action.error && <ErrorBanner error={action.error} />}
      </div>
    </>
  );
}

function RunPanel({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const client = useWs();
  const action = useAction();
  const c = job.counts;
  const pct = progressPercent(c);
  const act = (verb: string) => action.run(async () => { await client.post(`/email/jobs/${job.id}/${verb}`); onChanged(); });
  const fill = job.status === "completed" ? "completed" : job.status === "cancelled" || job.status === "failed" ? "cancelled" : "running";
  return (
    <div className="card pad">
      <div className="ev-run__head">
        <h3>Validation run</h3>
        <Pill value={job.status} />
        <span className="filterbar__spacer" />
        {isActive(job.status) && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act("pause")}>Pause</button>}
        {job.status === "paused" && <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => act("resume")}>Resume</button>}
        {(isActive(job.status) || job.status === "paused") && <button type="button" className="button button--danger button--small" disabled={action.busy} onClick={() => act("cancel")}>Cancel</button>}
      </div>
      <div className="progress">
        <div className="progress__track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
          <div className={`progress__fill progress__fill--${fill}`} style={{ width: `${pct}%` }} />
        </div>
        <div className="progress__meta">
          <span>{String(job.task?.progress?.message ?? (job.status === "queued" ? "Waiting for the background worker…" : job.status))}</span>
          <span className="tabular">{fmt(c.processed ?? 0)} / {fmt(c.total ?? job.row_count)} · {pct}%</span>
        </div>
      </div>
      <div className="stats stats--wrap ev-stats">
        <Stat label="Total" value={fmt(c.total ?? job.row_count)} />
        <Stat label="Processed" value={fmt(c.processed ?? 0)} />
        <Stat label="Valid" value={fmt(c.VALID ?? 0)} hint="mailbox verified" />
        <Stat label="Invalid" value={fmt(c.INVALID ?? 0)} />
        <Stat label="Risky" value={fmt(c.RISKY ?? 0)} />
        <Stat label="Role" value={fmt(c.ROLE ?? 0)} />
        <Stat label="Disposable" value={fmt(c.DISPOSABLE ?? 0)} />
        <Stat label="Free provider" value={fmt(c.FREE_PROVIDER ?? 0)} />
        <Stat label="Unknown" value={fmt(c.UNKNOWN ?? 0)} hint="mail server OK, mailbox unchecked" />
      </div>
      {job.error && <p className="error-text">{job.error}</p>}
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

function Results({ job, reloadKey }: { job: Job; reloadKey: string }) {
  const client = useWs();
  const [tabKey, setTabKey] = useState("all");
  const [draft, setDraft] = useState<ItemFilters>({});
  const [filters, setFilters] = useState<ItemFilters>({});
  const [offset, setOffset] = useState(0);
  const tab = RESULT_TABS.find((t) => t.key === tabKey) ?? RESULT_TABS[0];
  const pageSize = 50;
  const query = itemQuery(tab, filters, pageSize, offset);
  const items = useLoad((signal) => client.list<Row>(`/email/jobs/${job.id}/items`, query, signal), JSON.stringify(query) + job.id + reloadKey);
  const download = useAction();
  useEffect(() => setOffset(0), [tabKey, JSON.stringify(filters)]);
  const page = items.data as PageOf<Row> | null;
  return (
    <div className="card">
      <div className="ev-results__head">
        <Tabs tabs={RESULT_TABS.map((t) => ({ key: t.key, label: t.label, count: tabCount(t, job.counts) }))} active={tabKey} onChange={setTabKey} />
        <div className="actions">
          <button type="button" className="button button--ghost button--small" disabled={download.busy} onClick={() => download.run(() => client.download(exportPath(job.id, "csv", tab), `${job.name}-${tab.key}.csv`))}>
            <Icon name="download" size={14} /> CSV
          </button>
          <button type="button" className="button button--ghost button--small" disabled={download.busy} onClick={() => download.run(() => client.download(exportPath(job.id, "xlsx", tab), `${job.name}-${tab.key}.xlsx`))}>
            <Icon name="download" size={14} /> XLSX
          </button>
        </div>
      </div>
      <form className="filterbar" onSubmit={(e) => { e.preventDefault(); setFilters(draft); }}>
        <div className="filterbar__row ev-filters">
          <input className="input input--small" placeholder="Email contains…" value={draft.email ?? ""} onChange={(e) => setDraft({ ...draft, email: e.target.value })} aria-label="Email contains" />
          <input className="input input--small" placeholder="Domain (exact)" value={draft.domain ?? ""} onChange={(e) => setDraft({ ...draft, domain: e.target.value })} aria-label="Domain" />
          <select className="input input--small" value={draft.provider ?? ""} onChange={(e) => setDraft({ ...draft, provider: e.target.value })} aria-label="Provider">
            <option value="">Any provider</option>
            <option value="local">Built-in</option>
            <option value="emaillistverify">EmailListVerify</option>
          </select>
          <label className="small muted ev-date">From <input className="input input--small" type="date" value={draft.from ?? ""} onChange={(e) => setDraft({ ...draft, from: e.target.value })} /></label>
          <label className="small muted ev-date">To <input className="input input--small" type="date" value={draft.to ?? ""} onChange={(e) => setDraft({ ...draft, to: e.target.value })} /></label>
          <button className="button button--ghost button--small" type="submit">Apply</button>
          {Object.values(filters).some(Boolean) && <button type="button" className="button button--ghost button--small" onClick={() => { setDraft({}); setFilters({}); }}>Clear</button>}
        </div>
      </form>
      {items.error && <ErrorBanner error={items.error} onRetry={items.refresh} />}
      {download.error && <ErrorBanner error={download.error} />}
      {!page ? <Loading /> : (
        <>
          <DataTable
            rows={page.items}
            columns={[
              { key: "row_number", label: "#", className: "tabular" },
              { key: "email", label: "Email", render: (r) => <span className="mono small">{fmt(r.email ?? (r.row as Record<string, unknown>)?.[job.email_column ?? "email"])}</span> },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "score", label: "Score", render: (r) => <Score value={r.score} /> },
              { key: "reason", label: "Why", render: (r) => <span className="small">{reasonFor(String(r.status), r.checks as Record<string, unknown>)}</span> },
              { key: "provider", label: "Checked by", render: (r) => (r.provider === "emaillistverify" ? "EmailListVerify" : r.provider === "local" ? "Built-in" : fmt(r.provider)) },
              { key: "cached", label: "Cached", render: (r) => (r.cached ? "Yes" : "") },
              { key: "contact_id", label: "CRM", render: (r) => (r.contact_id ? <Link className="link" to={`/contacts/${r.contact_id}`}>contact</Link> : <span className="muted small">not in CRM</span>) },
            ]}
            empty={{ title: "No rows here", description: tab.statuses.length ? "No results have this status yet." : "Results appear as rows are checked.", icon: "mailcheck" }}
          />
          <div className="pager">
            <span className="muted small tabular">{page.total === 0 ? "0 results" : `${page.offset + 1}–${page.offset + page.items.length} of ${page.total.toLocaleString()}`}</span>
            <div className="actions">
              <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</button>
              <button className="button button--ghost button--small" disabled={!page.has_more} onClick={() => setOffset(offset + pageSize)}>Next</button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

const ACTION_STATUSES = ["VALID", "UNKNOWN", "RISKY", "ROLE", "FREE_PROVIDER"];

function GtmActions({ job }: { job: Job }) {
  const client = useWs();
  const [statuses, setStatuses] = useState<string[]>(() => ((job.counts.VALID ?? 0) > 0 ? ["VALID"] : ["VALID", "UNKNOWN"]));
  const [createContacts, setCreateContacts] = useState(false);
  const [listName, setListName] = useState(`${job.name} — validated`);
  const [listId, setListId] = useState("");
  const [campaignName, setCampaignName] = useState("");
  const [sequenceId, setSequenceId] = useState("");
  const [message, setMessage] = useState<ReactNode>(null);
  const lists = useLoad((signal) => client.list("/lists", { entity_type: "contacts", limit: 200, order: "name" }, signal), client.base + "/lists/c" + job.id);
  const sequences = useLoad((signal) => client.list("/sequences", { limit: 200 }, signal), client.base + "/sequences" + job.id);
  const action = useAction();
  const toggle = (s: string) => setStatuses((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));
  const selected = statuses.reduce((sum, s) => sum + (job.counts[s] ?? 0), 0);
  const common = { statuses, create_missing_contacts: createContacts };
  const summary = (r: { added?: number; not_in_crm?: number; created_contacts?: number }) =>
    `${fmt(r.added ?? 0)} added · ${fmt(r.not_in_crm ?? 0)} not in the CRM${r.created_contacts ? ` · ${fmt(r.created_contacts)} contacts created` : ""}`;
  return (
    <div className="card pad">
      <h3>Use the results</h3>
      <p className="muted small">These are explicit actions. Nothing is added to the CRM, enrolled or sent automatically; sequence enrollments wait for approval and campaigns start as drafts with sending off.</p>
      <div className="ev-statuses" role="group" aria-label="Results to use">
        {ACTION_STATUSES.map((s) => (
          <label key={s} className="check"><input type="checkbox" checked={statuses.includes(s)} onChange={() => toggle(s)} /> <Pill value={s} /> <span className="muted small tabular">{fmt(job.counts[s] ?? 0)}</span></label>
        ))}
      </div>
      <label className="check ev-create">
        <input type="checkbox" checked={createContacts} onChange={(e) => setCreateContacts(e.target.checked)} />
        <span>Create CRM contacts for rows that are not in the CRM yet <span className="muted small">(off by default)</span></span>
      </label>
      <p className="small muted">{fmt(selected)} result{selected === 1 ? "" : "s"} selected.</p>
      <div className="ev-actions">
        <div>
          <h4 className="ev-subhead">Add to list</h4>
          <select className="input input--small" value={listId} onChange={(e) => setListId(e.target.value)} aria-label="Existing list">
            <option value="">New list…</option>
            {(lists.data?.items ?? []).map((l) => <option key={l.id} value={l.id}>{String(l.name)}</option>)}
          </select>
          {!listId && <input className="input input--small" value={listName} onChange={(e) => setListName(e.target.value)} aria-label="New list name" />}
          <button type="button" className="button button--ghost button--small" disabled={action.busy || !statuses.length || (!listId && !listName.trim())}
            onClick={() => action.run(async () => {
              const r = await client.post<{ list: Row; added: number; not_in_crm: number; created_contacts: number }>(`/email/jobs/${job.id}/add-to-list`, { ...common, list_id: listId || undefined, list_name: listId ? undefined : listName });
              setMessage(<>List <Link className="link" to={`/lists/${r.list.id}`}>{String(r.list.name)}</Link>: {summary(r)}.</>);
              lists.refresh();
            })}>Add to list</button>
        </div>
        <div>
          <h4 className="ev-subhead">Create campaign</h4>
          <input className="input input--small" placeholder="Campaign name" value={campaignName} onChange={(e) => setCampaignName(e.target.value)} aria-label="Campaign name" />
          <button type="button" className="button button--ghost button--small" disabled={action.busy || !statuses.length || !campaignName.trim()}
            onClick={() => action.run(async () => {
              const r = await client.post<{ campaign: Row; list: Row; added: number; not_in_crm: number; created_contacts: number }>(`/email/jobs/${job.id}/campaign`, { ...common, name: campaignName.trim() });
              setMessage(<>Draft campaign <Link className="link" to={`/campaigns/${r.campaign.id}`}>{String(r.campaign.name)}</Link> created with audience list <Link className="link" to={`/lists/${r.list.id}`}>{String(r.list.name)}</Link> ({summary(r)}). Sending is off.</>);
            })}>Create draft campaign</button>
        </div>
        <div>
          <h4 className="ev-subhead">Add to sequence</h4>
          <select className="input input--small" value={sequenceId} onChange={(e) => setSequenceId(e.target.value)} aria-label="Sequence">
            <option value="">Choose a sequence…</option>
            {(sequences.data?.items ?? []).filter((s) => s.status !== "archived").map((s) => <option key={s.id} value={s.id}>{String(s.name)}</option>)}
          </select>
          <button type="button" className="button button--ghost button--small" disabled={action.busy || !statuses.length || !sequenceId}
            onClick={() => action.run(async () => {
              const r = await client.post<{ enrolled: number; skipped: unknown[]; not_in_crm: number }>(`/email/jobs/${job.id}/enroll`, { statuses, sequence_id: sequenceId });
              setMessage(<>{fmt(r.enrolled)} enrolled (pending approval) · {fmt(r.skipped.length)} skipped by suppression/deliverability rules · {fmt(r.not_in_crm)} not in the CRM. <Link className="link" to={`/sequences/${sequenceId}`}>Review enrollments</Link></>);
            })}>Enroll (pending approval)</button>
          <p className="muted small">Only rows already in the CRM can be enrolled.</p>
        </div>
      </div>
      {message && <p className="alert alert--info">{message}</p>}
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

export function EmailValidationJob() {
  const { jobId = "" } = useParams();
  const client = useWs();
  const navigate = useNavigate();
  const [tick, setTick] = useState(0);
  const [poll, setPoll] = useState<number | undefined>(undefined);
  const { data: job, error, refresh } = useLoad((signal) => client.get<Job>(`/email/jobs/${jobId}`, undefined, signal), client.base + jobId + tick, poll);
  const remove = useAction();
  useEffect(() => setPoll(job && isActive(job.status) ? 2000 : undefined), [job?.status]);
  if (error) return <div className="page"><Link to="/email-validation" className="back">← Email Validation</Link><ErrorBanner error={error} onRetry={refresh} /></div>;
  if (!job) return <div className="page"><Loading /></div>;
  const changed = () => setTick((n) => n + 1);
  const setup = job.status === "uploaded" || job.status === "ready";
  const done = ["completed", "cancelled", "failed"].includes(job.status);
  const processed = job.counts.processed ?? 0;
  return (
    <div className="page">
      <Link to="/email-validation" className="back">← Email Validation</Link>
      <PageHeader
        title={job.name}
        subtitle={<>{job.filename ? `${job.filename} · ` : ""}{job.size_bytes ? `${fileSize(job.size_bytes)} · ` : ""}{fmt(job.row_count)} rows · source {job.source_type.replace(/_/g, " ")}</>}
        actions={<>
          <Pill value={job.status} />
          {(setup || done) && <button type="button" className="button button--ghost button--small" disabled={remove.busy} onClick={() => remove.run(async () => { await client.del(`/email/jobs/${job.id}`); navigate("/email-validation"); })}>Delete</button>}
        </>}
      />
      {remove.error && <ErrorBanner error={remove.error} />}
      {setup ? <ColumnStep job={job} onChanged={changed} /> : <RunPanel job={job} onChanged={changed} />}
      {!setup && processed > 0 && <Results job={job} reloadKey={isActive(job.status) ? String(job.counts.processed) : String(tick)} />}
      {!setup && processed === 0 && !done && <EmptyState title="Waiting to start" description="The background worker picks the job up shortly. Results appear here as rows are checked." icon="clock" />}
      {done && processed > 0 && <GtmActions job={job} />}
    </div>
  );
}
