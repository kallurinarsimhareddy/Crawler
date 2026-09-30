// Email Validation: paste addresses or contacts (or upload a CSV/XLSX, or validate a contact
// list). Free layered checks run first; addresses that stay Not Verified can then be sent to
// EmailListVerify after the user confirms the exact credit cost. Users see three statuses only
// — Valid, Invalid, Not verified — and every signal behind them in a details drawer. Every
// input becomes an ordinary job, so all of them go through the same pipeline.

import { useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { Icon } from "../../shell/Icon";
import type { PageOf, Row } from "../api";
import { fileSize } from "../logic/format";
import {
  ACTION_GROUPS,
  COPY_GROUPS,
  FINAL_LABELS,
  MAX_PASTED,
  MAX_UPLOAD_ROWS,
  RESULT_TABS,
  SUMMARY_LABELS,
  collectEmails,
  copyTarget,
  emailsToText,
  exportPath,
  finalStatus,
  initialColumn,
  isActive,
  itemQuery,
  parseContactRows,
  parsePastedEmails,
  pastedRows,
  progressPercent,
  providerState,
  reasonFor,
  signalTone,
  sourceLabel,
  tabCount,
  uploadProblem,
  type Candidate,
  type Counts,
  type EvidenceSummary,
  type FinalStatus,
  type ItemFilters,
} from "../logic/emailValidation";
import { DataTable, KeyValues, PageHeader, Pill, ResourceList, Stat, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
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

type ContactColumns = Partial<Record<"first_name" | "last_name" | "full_name" | "company" | "title" | "website", string>>;

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
  settings: {
    candidates?: Candidate[];
    problems?: string[];
    allow_paid?: boolean;
    max_age_days?: number;
    last_recheck_at?: string;
    contact_mode?: boolean;
    contact_columns?: ContactColumns;
    public_evidence?: boolean;
    smtp_preflight?: boolean;
    /** Large uploads are read into the job by the background worker. */
    ingest?: { state: "pending" | "done"; rows?: number };
  };
  task?: { status: string; progress?: Record<string, unknown>; error?: string | null } | null;
  error?: string | null;
};

type Item = Row & { final_status?: FinalStatus; summary?: EvidenceSummary; checks?: Record<string, unknown> };

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
          <li><Icon name="check" size={14} /> <strong>SPF / DMARC</strong> <span className="muted small">— the domain's email security records (supporting signals)</span></li>
          <li><Icon name="check" size={14} /> <strong>Contact match</strong> <span className="muted small">— is it this person's address at this company, or a shared inbox?</span></li>
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
            ["Credits left", elv.credits?.known ? fmt(elv.credits.remaining) : "unknown — use Test connection (free) to read it"],
            ["Checks run", fmt(elv.usage?.calls ?? 0)],
            ["Last verified", fmt(elv.last_checked_at)],
          ]} />
        ) : (
          <p className="muted small">
            {elv.configured
              ? "A key is stored but has not been verified, so no paid checks run. Test the connection to verify it."
              : `Not connected. Mailbox-level checks (the only way to mark an address Valid) need ${elv.requirement}. Built-in validation works without it.`}
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

/** Copy text to the clipboard; falls back to a hidden textarea where the async API is missing. */
async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  if (!ok) throw new Error("The browser blocked clipboard access.");
}

const PIPELINE_NOTE = "Free checks run first: format, domain and MX, SPF/DMARC, risk signals and (for contacts) name and company match. Addresses that stay Not Verified can then be checked by EmailListVerify — only after you confirm the exact credit cost.";

const plural = (n: number, what = "email") => `${fmt(n)} ${what}${n === 1 ? "" : what.endsWith("s") ? "es" : "s"}`;

// --- options shared by paste and upload ---------------------------------------------------

interface EvidenceOptions {
  public_evidence: boolean;
  smtp_preflight: boolean;
}

function OptionsBox({ value, onChange, contact }: { value: EvidenceOptions; onChange: (v: EvidenceOptions) => void; contact: boolean }) {
  return (
    <fieldset className="ev-options">
      <legend className="small muted">Optional, slower checks</legend>
      <label className="check">
        <input type="checkbox" checked={value.public_evidence} onChange={(e) => onChange({ ...value, public_evidence: e.target.checked })} />
        <span>Look for public evidence <span className="muted small">— the exact address on the company's own website (team, leadership, contact pages; robots.txt respected). Supporting evidence only{contact ? "" : "; most useful for contacts"}.</span></span>
      </label>
      <label className="check">
        <input type="checkbox" checked={value.smtp_preflight} onChange={(e) => onChange({ ...value, smtp_preflight: e.target.checked })} />
        <span>Mail server check <span className="muted small">— connect and greet each mail server once (no mailbox probing, nothing sent). Often blocked by internet providers; then reported as unknown.</span></span>
      </label>
    </fieldset>
  );
}

// --- landing: paste, upload, list validation, history ----------------------------------

function PastePanel() {
  const client = useWs();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"emails" | "contacts">("emails");
  const [text, setText] = useState("");
  const [options, setOptions] = useState<EvidenceOptions>({ public_evidence: false, smtp_preflight: false });
  const action = useAction();
  const emails = useMemo(() => (mode === "emails" ? parsePastedEmails(text) : null), [text, mode]);
  const contacts = useMemo(() => (mode === "contacts" ? parseContactRows(text) : null), [text, mode]);
  const toValidate = emails ? emails.emails.length : contacts?.rows.length ?? 0;
  const rows: Record<string, string>[] = emails ? pastedRows(emails) : (contacts?.rows ?? []).map((r) => ({ ...r }));
  const hasInput = emails ? emails.total > 0 : (contacts?.total ?? 0) > 0;
  const malformed = emails ? emails.malformed : contacts?.malformed ?? [];
  const overLimit = emails ? emails.overLimit : contacts?.overLimit ?? 0;
  return (
    <div className="card pad ev-paste">
      <div className="ev-paste__head">
        <h3>Paste {mode === "emails" ? "emails" : "contacts"}</h3>
        <div className="ev-mode" role="radiogroup" aria-label="What to paste">
          <label className={`ev-mode__option${mode === "emails" ? " ev-mode__option--on" : ""}`}>
            <input type="radio" name="paste-mode" checked={mode === "emails"} onChange={() => setMode("emails")} /> Emails
          </label>
          <label className={`ev-mode__option${mode === "contacts" ? " ev-mode__option--on" : ""}`}>
            <input type="radio" name="paste-mode" checked={mode === "contacts"} onChange={() => setMode("contacts")} /> Validate Contact Emails
          </label>
        </div>
      </div>
      <p className="muted small">
        {mode === "emails"
          ? "One per line, or separated by commas, semicolons, spaces or tabs. Duplicates are removed and addresses are lower-cased before checking."
          : "One contact per line: First Name, Last Name, Company, Title, Email (copy rows straight from a spreadsheet, or comma-separated). A header row is optional. SANA GTM then tells a person's own address (john.smith@company.com) from a shared inbox (info@company.com)."}
      </p>
      <textarea
        className="input ev-paste__text"
        rows={8}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={mode === "emails" ? "Paste email addresses here, one per line..." : "First Name\tLast Name\tCompany\tTitle\tEmail"}
        aria-label={mode === "emails" ? "Email addresses to validate" : "Contacts to validate"}
        spellCheck={false}
        autoComplete="off"
      />
      {hasInput && (
        <>
          <div className="stats stats--wrap ev-paste__stats" aria-live="polite">
            {emails ? (
              <>
                <Stat label="Pasted" value={fmt(emails.total)} />
                <Stat label="Unique" value={fmt(emails.unique)} />
                <Stat label="Duplicates" value={fmt(emails.duplicates)} hint="removed" />
              </>
            ) : (
              <>
                <Stat label="Contacts" value={fmt(contacts!.total)} hint={contacts!.hasHeader ? "header row detected" : "no header row"} />
                <Stat label="Duplicates" value={fmt(contacts!.duplicates)} hint="same email, removed" />
                <Stat label="No email" value={fmt(contacts!.missingEmail)} hint="skipped" />
              </>
            )}
            <Stat label="Invalid format" value={fmt(malformed.length)} hint="not sent" />
            <Stat label="To validate" value={fmt(toValidate)} />
            <Stat label="Credits now" value="0" hint="free checks only" />
          </div>
          {malformed.length > 0 && (
            <p className="muted small ev-paste__bad">
              Invalid format: <span className="mono">{malformed.slice(0, 8).join(", ")}</span>
              {malformed.length > 8 && ` and ${fmt(malformed.length - 8)} more`}
            </p>
          )}
          {overLimit > 0 && (
            <p className="alert alert--warning">Only the first {fmt(MAX_PASTED)} are validated here; {fmt(overLimit)} left out. Upload a file for larger lists — it is split into batches automatically.</p>
          )}
        </>
      )}
      <OptionsBox value={options} onChange={setOptions} contact={mode === "contacts"} />
      <p className="muted small">{PIPELINE_NOTE}</p>
      <div className="actions">
        <button type="button" className="button button--primary" disabled={toValidate === 0 || action.busy}
          onClick={() => action.run(async () => {
            const job = await client.post<Job>("/email/jobs", {
              source: "rows",
              source_type: "manual",
              email_field: "email",
              name: `${mode === "emails" ? "Pasted emails" : "Pasted contacts"} (${fmt(toValidate)})`,
              rows,
              start: true,
              settings: { allow_paid: false, max_age_days: 30, ...options },
            });
            navigate(`/email-validation/${job.id}`);
          })}>
          {action.busy ? "Starting…" : mode === "emails" ? "Validate Emails" : "Validate Contact Emails"}
        </button>
        {text && <button type="button" className="button button--ghost" disabled={action.busy} onClick={() => setText("")}>Clear</button>}
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

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
      <p className="muted small">or <span className="link">browse</span> · up to 100 MB and {fmt(MAX_UPLOAD_ROWS)} rows, processed in batches</p>
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

/** Valid / Invalid / Not verified for a job row (older jobs have only internal counts). */
function finalCounts(c: Counts): [number, number, number] {
  if (c.final_valid !== undefined) return [c.final_valid ?? 0, c.final_invalid ?? 0, c.final_not_verified ?? 0];
  const valid = c.VALID ?? 0, invalid = c.INVALID ?? 0;
  return [valid, invalid, Math.max(0, (c.processed ?? 0) - valid - invalid)];
}

export function EmailValidation() {
  const client = useWs();
  const navigate = useNavigate();
  const upload = useAction();
  return (
    <div className="page">
      <PageHeader title="Email Validation" subtitle="Check addresses before they reach a campaign. Every result is Valid, Invalid or Not verified; the evidence behind it (format, domain, MX, SPF/DMARC, risk, contact match, EmailListVerify) is one click away. Nothing is ever sent." />
      <PastePanel />
      <div className="grid-2">
        <div className="card pad">
          <h3>Upload a file</h3>
          <p className="muted small">For large lists, or spreadsheets with more columns to keep in the export. Columns such as First Name, Last Name, Company and Title turn on contact matching automatically.</p>
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
          { key: "counts", label: "Valid / Invalid / Not verified", render: (r) => { const [v, i, n] = finalCounts((r.counts ?? {}) as Counts); return <span className="tabular">{fmt(v)} / {fmt(i)} / {fmt(n)}</span>; } },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
        ]}
        empty={{ title: "No validation jobs yet", description: "Paste addresses or contacts, upload a CSV or XLSX file, or validate a contact list, to see results here.", icon: "mailcheck" }}
      />
    </div>
  );
}

// --- a job ----------------------------------------------------------------------------

const CONTACT_FIELD_LABELS: Record<string, string> = { first_name: "First name", last_name: "Last name", full_name: "Name", company: "Company", title: "Title", website: "Website" };

function ColumnStep({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const client = useWs();
  const [column, setColumn] = useState(() => initialColumn(job.email_column, job.settings.candidates, job.columns));
  const [maxAge, setMaxAge] = useState(30);
  const [options, setOptions] = useState<EvidenceOptions>({ public_evidence: false, smtp_preflight: false });
  const action = useAction();
  const candidates = job.settings.candidates ?? [];
  const contactColumns = Object.entries(job.settings.contact_columns ?? {});
  const reading = job.settings.ingest?.state === "pending";
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
        {contactColumns.length > 0 && (
          <p className="alert alert--info">
            Contact matching {job.settings.contact_mode ? "is on" : "has partial data"}: {contactColumns.map(([field, col]) => `${CONTACT_FIELD_LABELS[field] ?? field} = “${col}”`).join(" · ")}
          </p>
        )}
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
        {reading && (
          <p className="alert alert--info" role="status">
            Reading the file in the background… {fmt(job.settings.ingest?.rows ?? job.row_count)} rows so far. You can choose the email column and options now; validation can start when reading has finished.
          </p>
        )}
        <ProviderPanel compact />
        <p className="muted small">{PIPELINE_NOTE}</p>
        <OptionsBox value={options} onChange={setOptions} contact={Boolean(job.settings.contact_mode)} />
        <label className="field ev-age">
          <span className="field__label">Reuse results checked in the last (days)</span>
          <input className="input input--small" type="number" min={0} max={365} value={maxAge} onChange={(e) => setMaxAge(Number(e.target.value) || 0)} />
          <span className="field__hint">Cached results are free and are never paid for twice.</span>
        </label>
        <div className="form__actions">
          <button type="button" className="button button--primary" disabled={!column || action.busy || reading}
            onClick={() => action.run(async () => {
              if (column !== job.email_column) await client.post(`/email/jobs/${job.id}/column`, { column });
              await client.post(`/email/jobs/${job.id}/start`, { settings: { allow_paid: false, max_age_days: maxAge, ...options } });
              onChanged();
            })}>
            {reading ? "Reading the file…" : `Validate ${fmt(job.row_count)} row${job.row_count === 1 ? "" : "s"}`}
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
  const [valid, invalid, notVerified] = finalCounts(c);
  const act = (verb: string) => action.run(async () => { await client.post(`/email/jobs/${job.id}/${verb}`); onChanged(); });
  const fill = job.status === "completed" ? "completed" : job.status === "cancelled" || job.status === "failed" ? "cancelled" : "running";
  const signals = [["Role / shared inbox", c.ROLE], ["Free provider", c.FREE_PROVIDER], ["Disposable", c.DISPOSABLE], ["Catch-all or risky", c.RISKY]]
    .filter(([, n]) => (n as number) > 0) as [string, number][];
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
        <Stat label="Valid" value={fmt(valid)} hint="mailbox verified" />
        <Stat label="Invalid" value={fmt(invalid)} hint="confirmed undeliverable" />
        <Stat label="Not verified" value={fmt(notVerified)} hint="mailbox not confirmed" />
      </div>
      {signals.length > 0 && (
        <p className="muted small ev-signals">Signals inside Not verified: {signals.map(([label, n]) => `${label} ${fmt(n)}`).join(" · ")}</p>
      )}
      {job.error && <p className="error-text">{job.error}</p>}
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

interface UnknownsEstimate {
  unresolved: number;
  reused_from_cache: number;
  to_check: number;
  skipped_catch_all: number;
  skipped_disposable: number;
  cost_per_check: number;
  credits: number;
  provider_active: boolean;
  credits_known: boolean;
  credits_remaining?: number | null;
  can_verify: boolean;
  blocker: string | null;
}

function VerifyUnknowns({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const client = useWs();
  const estimate = useLoad((signal) => client.get<UnknownsEstimate>(`/email/jobs/${job.id}/unknowns`, undefined, signal),
    `${client.base}/unknowns/${job.id}/${job.counts.final_not_verified ?? 0}/${job.counts.provider_emaillistverify ?? 0}/${job.status}`);
  const action = useAction();
  const [confirming, setConfirming] = useState(false);
  const e = estimate.data;
  const recheck = Boolean(job.settings.last_recheck_at);
  const title = recheck ? "Recheck Unknowns" : "Verify Not Verified emails with EmailListVerify";
  if (!e) return estimate.error ? <ErrorBanner error={estimate.error} onRetry={estimate.refresh} /> : null;
  if (e.unresolved === 0) {
    return (job.counts.final_not_verified ?? 0) > 0 ? (
      <div className="card pad ev-verify">
        <h3>{title}</h3>
        <p className="muted small">Nothing left to send: the remaining Not verified addresses were already checked by EmailListVerify (inconclusive, e.g. catch-all), are disposable, or sit on a known catch-all domain. They are not sent again.</p>
      </div>
    ) : null;
  }
  const balanceUnknown = e.provider_active && !e.credits_known && e.credits > 0;
  return (
    <div className="card pad ev-verify">
      <h3>{title}</h3>
      <p className="muted small">Only Not verified addresses the free checks could not settle are sent. Valid and Invalid results, disposable addresses, known catch-all domains and addresses EmailListVerify already answered are never sent again. Nothing is emailed.</p>
      <div className="stats stats--wrap ev-verify__stats">
        <Stat label="Unknown candidates" value={fmt(e.unresolved)} />
        <Stat label="Previously verified and reusable" value={fmt(e.reused_from_cache)} hint="free" />
        <Stat label="New ELV checks" value={fmt(e.to_check)} />
        <Stat label="Estimated credits" value={fmt(e.credits)} hint={`${fmt(e.cost_per_check)} per address`} />
        <Stat label="Credits available" value={e.credits_known ? fmt(e.credits_remaining ?? 0) : "unknown"} />
      </div>
      {(e.skipped_catch_all > 0 || e.skipped_disposable > 0) && (
        <p className="muted small">Not sent: {[e.skipped_catch_all ? `${fmt(e.skipped_catch_all)} on known catch-all domains` : "", e.skipped_disposable ? `${fmt(e.skipped_disposable)} disposable` : ""].filter(Boolean).join(" · ")}.</p>
      )}
      {e.blocker && <p className="alert alert--warning">{e.blocker}</p>}
      <div className="actions">
        {balanceUnknown && (
          <button type="button" className="button button--ghost" disabled={action.busy}
            onClick={() => action.run(async () => { await client.post("/email/provider/test"); estimate.refresh(); })}>
            Refresh balance (free)
          </button>
        )}
        {!confirming ? (
          <button type="button" className="button button--primary" disabled={!e.can_verify || action.busy} onClick={() => setConfirming(true)}>
            {recheck ? "Recheck Unknowns" : "Verify with EmailListVerify"}
          </button>
        ) : (
          <div className="ev-confirm" role="alertdialog" aria-label="Confirm paid verification">
            <p><strong>Spend {fmt(e.credits)} EmailListVerify credit{e.credits === 1 ? "" : "s"} to verify {plural(e.to_check)}?</strong></p>
            <div className="actions">
              <button type="button" className="button button--primary" disabled={action.busy}
                onClick={() => action.run(async () => {
                  try {
                    await client.post(`/email/jobs/${job.id}/verify-unknowns`, { confirm: true, expected_credits: e.credits });
                    onChanged();
                  } finally {
                    setConfirming(false);
                    estimate.refresh();
                  }
                })}>
                Confirm and verify
              </button>
              <button type="button" className="button button--ghost" disabled={action.busy} onClick={() => setConfirming(false)}>Cancel</button>
            </div>
          </div>
        )}
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

// --- results ----------------------------------------------------------------------------

function FinalPill({ item }: { item: Item }) {
  if (item.status === "PENDING") return <Pill value="PENDING" />;
  const status = item.final_status ?? finalStatus(item.status, item.provider);
  return <Pill value={status === "NOT_VERIFIED" ? "NOT_VERIFIED" : status} />;
}

function contactHint(item: Item): string {
  const s = item.summary;
  if (!s) return "";
  if (s.role === "YES") return "Role / shared inbox";
  if (s.disposable === "YES") return "Disposable";
  if (s.person_match === "YES") return "Person's own address";
  if (s.free_provider === "YES") return "Free mailbox";
  return "";
}

function contactName(item: Item, cols: ContactColumns): string {
  const row = (item.row ?? {}) as Record<string, unknown>;
  const get = (k: keyof ContactColumns) => (cols[k] ? String(row[cols[k] as string] ?? "").trim() : "");
  return [get("first_name"), get("last_name")].filter(Boolean).join(" ") || get("full_name");
}

function DetailsDrawer({ item, job, onClose }: { item: Item; job: Job; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const checks = (item.checks ?? {}) as Record<string, unknown>;
  const ev = (checks.evidence ?? {}) as Record<string, Record<string, unknown>>;
  const fmtEv = ev.format ?? {};
  const dom = ev.domain ?? {};
  const smtp = ev.smtp ?? {};
  const pub = ev.public ?? {};
  const contact = ev.contact ?? {};
  const summary = item.summary;
  const name = contactName(item, job.settings.contact_columns ?? {});
  return (
    <div className="ev-drawer" role="dialog" aria-modal="true" aria-label={`Evidence for ${String(item.email ?? "")}`}>
      <button type="button" className="ev-drawer__scrim" aria-label="Close details" onClick={onClose} />
      <aside className="ev-drawer__panel">
        <div className="ev-drawer__head">
          <div>
            <p className="mono ev-drawer__email">{String(item.email ?? "—")}</p>
            {name && <p className="muted small">{name}</p>}
          </div>
          <button type="button" className="button button--ghost button--small" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p><FinalPill item={item} /> <span className="small">{reasonFor(String(item.status), checks)}</span></p>
        {summary && (
          <dl className="ev-evidence">
            {SUMMARY_LABELS.map(([key, label]) => (
              <div key={key} className="ev-evidence__row">
                <dt>{label}</dt>
                <dd className={`ev-signal ev-signal--${signalTone(key, summary[key])}`}>{summary[key]}</dd>
              </div>
            ))}
          </dl>
        )}
        <h4 className="ev-subhead">Details</h4>
        <ul className="ev-details small">
          {Array.isArray(fmtEv.issues) && fmtEv.issues.length > 0 && <li>Format: {(fmtEv.issues as string[]).join(", ")}</li>}
          {fmtEv.did_you_mean ? <li>Possible typo — did you mean <span className="mono">{String(fmtEv.did_you_mean)}</span>?</li> : null}
          {fmtEv.idn ? <li>Internationalized domain (checked as punycode)</li> : null}
          {dom.dns_error ? <li>DNS: {String(dom.dns_error)}</li> : null}
          {dom.null_mx ? <li>The domain publishes a null MX: it accepts no email</li> : null}
          {dom.a_fallback ? <li>No MX record; mail goes to the domain's own address (A/AAAA fallback)</li> : null}
          {dom.mx_hosts ? <li>{fmt(dom.mx_hosts)} mail server{Number(dom.mx_hosts) === 1 ? "" : "s"} (MX)</li> : null}
          {dom.spf_all ? <li>SPF policy {String(dom.spf_all)}</li> : null}
          {dom.dmarc_policy ? <li>DMARC policy p={String(dom.dmarc_policy)}</li> : null}
          {smtp.smtp ? <li>Mail server check: {String(smtp.smtp)}{smtp.starttls ? " · STARTTLS offered" : ""}{smtp.detail ? ` · ${String(smtp.detail)}` : ""}</li> : null}
          {contact.person_name_match ? <li>Person match: {String(contact.person_name_match)}{contact.name_pattern ? ` (${String(contact.name_pattern)})` : ""} · company match: {String(contact.company_match)} · contact confidence {String(contact.contact_evidence_confidence)} ({String(contact.contact_confidence_label)})</li> : null}
          {pub.public_email_evidence === true && (
            <li>Published at <a className="link" href={String(pub.source_url)} target="_blank" rel="noopener noreferrer">{String(pub.source_type)}</a> ({String(pub.evidence_confidence)} confidence{pub.checked_at ? `, ${fmtDate(pub.checked_at)}` : ""}). Publication supports the address but does not prove the mailbox exists.</li>
          )}
          {pub.public_email_evidence === false && <li>Not found on the company's public pages ({fmt(pub.pages_read)} read)</li>}
          {pub.reason && pub.public_email_evidence == null ? <li>Public evidence: {String(pub.reason)}</li> : null}
          {checks.result_code ? <li>EmailListVerify answer: <span className="mono">{String(checks.result_code)}</span></li> : null}
          {checks.paid_error ? <li>Paid check failed: {String(checks.paid_error)}</li> : null}
          {item.cached ? <li>Reused from a recent check (no new cost)</li> : null}
        </ul>
        {item.contact_id ? <p><Link className="link" to={`/contacts/${String(item.contact_id)}`}>Open the CRM contact</Link></p> : null}
      </aside>
    </div>
  );
}

function Results({ job, reloadKey }: { job: Job; reloadKey: string }) {
  const client = useWs();
  const [tabKey, setTabKey] = useState("all");
  const [draft, setDraft] = useState<ItemFilters>({});
  const [filters, setFilters] = useState<ItemFilters>({});
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<Item | null>(null);
  const tab = RESULT_TABS.find((t) => t.key === tabKey) ?? RESULT_TABS[0];
  const pageSize = 50;
  const query = itemQuery(tab, filters, pageSize, offset);
  const items = useLoad((signal) => client.list<Item>(`/email/jobs/${job.id}/items`, query, signal), JSON.stringify(query) + job.id + reloadKey);
  const download = useAction();
  const copy = useAction();
  const [copied, setCopied] = useState<string | null>(null);
  const [selected, setSelected] = useState<Map<string, string>>(() => new Map());
  useEffect(() => setOffset(0), [tabKey, JSON.stringify(filters)]);
  const page = items.data as PageOf<Item> | null;
  const emailOf = (r: Row) => String(r.email ?? (r.row as Record<string, unknown>)?.[job.email_column ?? "email"] ?? "").trim();
  const pageRows = (page?.items ?? []).filter((r) => emailOf(r));
  const allOnPage = pageRows.length > 0 && pageRows.every((r) => selected.has(String(r.id)));
  const contactMode = Boolean(job.settings.contact_mode);
  const cols = job.settings.contact_columns ?? {};
  const toggle = (r: Row) => setSelected((cur) => {
    const next = new Map(cur);
    if (next.has(String(r.id))) next.delete(String(r.id));
    else next.set(String(r.id), emailOf(r));
    return next;
  });
  const togglePage = () => setSelected((cur) => {
    const next = new Map(cur);
    for (const r of pageRows) {
      if (allOnPage) next.delete(String(r.id));
      else next.set(String(r.id), emailOf(r));
    }
    return next;
  });
  const copyOut = (label: string, make: () => Promise<string>) => copy.run(async () => {
    setCopied(null);
    const text = await make();
    const count = text ? text.split("\n").length : 0;
    if (count) await copyText(text);
    setCopied(count ? `${label}: ${plural(count)} copied to the clipboard.` : `${label}: nothing to copy.`);
  });
  const select = { key: "select", label: "", className: "ev-select", render: (r: Item) => (emailOf(r) ? (
    <input type="checkbox" checked={selected.has(String(r.id))} onChange={() => toggle(r)} aria-label={`Select ${emailOf(r)}`} />
  ) : null) };
  const details = { key: "details", label: "Details", render: (r: Item) => (r.status === "PENDING" ? "" : (
    <button type="button" className="button button--ghost button--small" onClick={() => setOpen(r)}>Details</button>
  )) };
  const source = { key: "source", label: "Source", render: (r: Item) => (r.status === "PENDING" ? "" : r.summary?.source ?? sourceLabel(r.provider, r.checks)) };
  const email = { key: "email", label: "Email", render: (r: Item) => <span className="mono small">{emailOf(r) || "—"}</span> };
  const status = { key: "status", label: "Status", render: (r: Item) => <FinalPill item={r} /> };
  const columns = contactMode ? [
    select,
    { key: "contact", label: "Contact", render: (r: Item) => contactName(r, cols) || <span className="muted">—</span> },
    email,
    { key: "company", label: "Company", render: (r: Item) => (cols.company ? fmt((r.row as Record<string, unknown>)?.[cols.company]) : "—") },
    status,
    { key: "person", label: "Person Match", render: (r: Item) => r.summary?.person_match ?? "" },
    { key: "public", label: "Public Evidence", render: (r: Item) => r.summary?.public_evidence ?? "" },
    { key: "mailbox", label: "Mailbox Verification", render: (r: Item) => r.summary?.mailbox_verification ?? "" },
    source,
    details,
  ] : [
    select,
    email,
    status,
    { key: "person", label: "Person/Contact", render: (r: Item) => <span className="small">{contactHint(r)}</span> },
    source,
    details,
  ];
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
      <div className="ev-copy" role="group" aria-label="Copy email addresses">
        {COPY_GROUPS.map((g) => (
          <button key={g.key} type="button" className={`button button--small ${g.key === "valid" ? "button--primary" : "button--ghost"}`}
            disabled={copy.busy || (job.counts[g.countKey] ?? 0) === 0}
            onClick={() => copyOut(g.label.replace(/^Copy /, ""), () => collectEmails((q) => client.list<Row>(`/email/jobs/${job.id}/items`, q), copyTarget(g)))}>
            <Icon name="copy" size={14} /> {g.label} <span className="tabular">({fmt(job.counts[g.countKey] ?? 0)})</span>
          </button>
        ))}
        <button type="button" className="button button--ghost button--small" disabled={copy.busy || selected.size === 0}
          onClick={() => copyOut("Selected emails", async () => emailsToText(selected.values()))}>
          <Icon name="copy" size={14} /> Copy selected ({fmt(selected.size)})
        </button>
        {selected.size > 0 && <button type="button" className="button button--ghost button--small" onClick={() => setSelected(new Map())}>Clear selection</button>}
      </div>
      {copied && <p className="muted small ev-copy__note" role="status">{copied}</p>}
      {copy.error && <ErrorBanner error={copy.error} />}
      <form className="filterbar" onSubmit={(e) => { e.preventDefault(); setFilters(draft); }}>
        <div className="filterbar__row ev-filters">
          <input className="input input--small" placeholder="Search email…" value={draft.email ?? ""} onChange={(e) => setDraft({ ...draft, email: e.target.value })} aria-label="Email contains" />
          <input className="input input--small" placeholder="Domain (exact)" value={draft.domain ?? ""} onChange={(e) => setDraft({ ...draft, domain: e.target.value })} aria-label="Domain" />
          <select className="input input--small" value={draft.provider ?? ""} onChange={(e) => setDraft({ ...draft, provider: e.target.value })} aria-label="Checked by">
            <option value="">Checked by: any</option>
            <option value="local">Built-in only</option>
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
            columns={columns}
            empty={{ title: "No rows here", description: tab.statuses.length ? `No results are ${FINAL_LABELS[(tab.key === "valid" ? "VALID" : tab.key === "invalid" ? "INVALID" : "NOT_VERIFIED") as FinalStatus].toLowerCase()} yet.` : "Results appear as rows are checked.", icon: "mailcheck" }}
          />
          <div className="pager">
            <span className="muted small tabular">{page.total === 0 ? "0 results" : `${page.offset + 1}–${page.offset + page.items.length} of ${page.total.toLocaleString()}`}</span>
            <div className="actions">
              {pageRows.length > 0 && <button type="button" className="button button--ghost button--small" onClick={togglePage}>{allOnPage ? "Unselect page" : "Select page"}</button>}
              <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</button>
              <button className="button button--ghost button--small" disabled={!page.has_more} onClick={() => setOffset(offset + pageSize)}>Next</button>
            </div>
          </div>
        </>
      )}
      {open && <DetailsDrawer item={open} job={job} onClose={() => setOpen(null)} />}
    </div>
  );
}

function GtmActions({ job }: { job: Job }) {
  const client = useWs();
  const [groups, setGroups] = useState<FinalStatus[]>(["VALID"]);
  const [createContacts, setCreateContacts] = useState(false);
  const [listName, setListName] = useState(`${job.name} — validated`);
  const [listId, setListId] = useState("");
  const [campaignName, setCampaignName] = useState("");
  const [sequenceId, setSequenceId] = useState("");
  const [message, setMessage] = useState<ReactNode>(null);
  const lists = useLoad((signal) => client.list("/lists", { entity_type: "contacts", limit: 200, order: "name" }, signal), client.base + "/lists/c" + job.id);
  const sequences = useLoad((signal) => client.list("/sequences", { limit: 200 }, signal), client.base + "/sequences" + job.id);
  const action = useAction();
  const [valid, , notVerified] = finalCounts(job.counts);
  const countOf = (g: FinalStatus) => (g === "VALID" ? valid : notVerified);
  const statuses = ACTION_GROUPS.filter((g) => groups.includes(g.key)).flatMap((g) => g.statuses);
  const toggle = (g: FinalStatus) => setGroups((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]));
  const selected = groups.reduce((sum, g) => sum + countOf(g), 0);
  const common = { statuses, create_missing_contacts: createContacts };
  const summary = (r: { added?: number; not_in_crm?: number; created_contacts?: number }) =>
    `${fmt(r.added ?? 0)} added · ${fmt(r.not_in_crm ?? 0)} not in the CRM${r.created_contacts ? ` · ${fmt(r.created_contacts)} contacts created` : ""}`;
  return (
    <div className="card pad">
      <h3>Use the results</h3>
      <p className="muted small">These are explicit actions. Nothing is added to the CRM, enrolled or sent automatically; sequence enrollments wait for approval and campaigns start as drafts with sending off. Invalid addresses are never offered.</p>
      <div className="ev-statuses" role="group" aria-label="Results to use">
        {ACTION_GROUPS.map((g) => (
          <label key={g.key} className="check"><input type="checkbox" checked={groups.includes(g.key)} onChange={() => toggle(g.key)} /> <Pill value={g.key} /> <span className="muted small tabular">{fmt(countOf(g.key))}</span></label>
        ))}
      </div>
      {groups.includes("NOT_VERIFIED") && <p className="alert alert--warning">Not verified addresses have no mailbox confirmation; sending to them risks bounces.</p>}
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
  const reading = job?.settings?.ingest?.state === "pending";
  useEffect(() => setPoll(job && (isActive(job.status) || reading) ? 2000 : undefined), [job?.status, reading]);
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
        subtitle={<>{job.filename ? `${job.filename} · ` : ""}{job.size_bytes ? `${fileSize(job.size_bytes)} · ` : ""}{fmt(job.row_count)} rows · source {job.source_type.replace(/_/g, " ")}{job.settings.contact_mode ? " · contact matching" : ""}</>}
        actions={<>
          <Pill value={job.status} />
          {(setup || done) && <button type="button" className="button button--ghost button--small" disabled={remove.busy} onClick={() => remove.run(async () => { await client.del(`/email/jobs/${job.id}`); navigate("/email-validation"); })}>Delete</button>}
        </>}
      />
      {remove.error && <ErrorBanner error={remove.error} />}
      {setup ? <ColumnStep job={job} onChanged={changed} /> : <RunPanel job={job} onChanged={changed} />}
      {job.status === "completed" && processed > 0 && <VerifyUnknowns job={job} onChanged={changed} />}
      {!setup && processed > 0 && <Results job={job} reloadKey={isActive(job.status) ? String(job.counts.processed) : String(tick)} />}
      {!setup && processed === 0 && !done && <EmptyState title="Waiting to start" description="The background worker picks the job up shortly. Results appear here as rows are checked." icon="clock" />}
      {done && processed > 0 && <GtmActions job={job} />}
    </div>
  );
}
