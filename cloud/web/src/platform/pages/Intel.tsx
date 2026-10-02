// Contacts, job postings, hiring intelligence and company discovery.

import { useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import "../styles/jobs.css";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { CreateForm } from "../ResourcePage";
import { KeyValues, PageHeader, Pill, ResourceList, Score, Stat, Tabs, Tags, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import { ScorePanel } from "./Scoring";

export function Contacts() {
  const client = useWs();
  const [params, setParams] = useSearchParams();
  const creating = params.get("create") === "1";
  const setCreating = (open: boolean) => {
    const next = new URLSearchParams(params);
    if (open) next.set("create", "1");
    else next.delete("create");
    setParams(next, { replace: true });
  };
  const [reload, setReload] = useState(0);
  return (
    <div className="page">
      <PageHeader
        title="Contacts"
        subtitle="People at target companies, with the source and validation status of every email."
        actions={creating ? <button className="button button--ghost button--small" onClick={() => setCreating(false)}>Close form</button> : undefined}
      />
      {creating && (
        <CreateForm
          path="/contacts"
          fields={[
            { key: "full_name", label: "Full name", required: true },
            { key: "title", label: "Title" },
            { key: "email", label: "Email" },
            { key: "phone", label: "Phone" },
            { key: "company_id", label: "Company id", placeholder: "co_…" },
            { key: "linkedin_url", label: "LinkedIn URL", hint: "Only if lawfully obtained." },
          ]}
          onCreated={() => { setCreating(false); setReload((n) => n + 1); }}
        />
      )}
      <ResourceList
        reloadKey={String(reload)}
        load={(query, signal) => client.list("/contacts", query, signal)}
        link={(r) => `/contacts/${r.id}`}
        empty={{
          title: "No contacts yet",
          description: "Add people at your target companies, import a contact file, or find contacts for a company from its page.",
          icon: "users",
          action: (
            <span className="actions">
              <button className="button button--primary" onClick={() => setCreating(true)}>+ Add Contact</button>
              <Link className="button button--ghost" to="/imports">Import</Link>
            </span>
          ),
        }}
        columns={[
          { key: "full_name", label: "Name" },
          { key: "title", label: "Title" },
          { key: "function", label: "Function", render: (r) => <Pill value={r.function} /> },
          { key: "seniority", label: "Seniority" },
          { key: "email", label: "Email", className: "mono small" },
          { key: "email_status", label: "Email", render: (r) => <Pill value={r.email_status} /> },
          { key: "contact_score", label: "Score", render: (r) => <Score value={r.contact_score} /> },
          { key: "source", label: "Source" },
        ]}
        filters={[
          { key: "email_status", label: "Email status", options: ["UNVERIFIED", "VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER"] },
          { key: "function", label: "Function", options: ["hr", "recruiting", "it", "executive", "finance", "operations", "other"] },
          { key: "seniority", label: "Seniority", placeholder: "seniority" },
        ]}
      />
    </div>
  );
}

export function ContactDetail() {
  const { contactId = "" } = useParams();
  const client = useWs();
  const [tab, setTab] = useState("profile");
  const { data, error, loading, refresh } = useLoad((signal) => client.get<Row>(`/contacts/${contactId}`, undefined, signal), client.base + contactId);
  const action = useAction();
  if (error) return <div className="page"><ErrorBanner error={error} onRetry={refresh} /></div>;
  if (loading || !data) return <div className="page"><Loading /></div>;
  const c = data;
  return (
    <div className="page">
      <Link to="/contacts" className="back">← Contacts</Link>
      <PageHeader
        title={String(c.full_name)}
        subtitle={[c.title, c.department].filter(Boolean).join(" · ") || "No title recorded"}
        actions={
          c.email ? (
            <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(async () => { await client.post("/email/validate", { emails: [c.email], allow_paid: false }); refresh(); })}>
              Validate email (free checks)
            </button>
          ) : undefined
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      <Tabs active={tab} onChange={setTab} tabs={["profile", "company", "role", "score", "email", "validation", "source", "activities", "sequences"].map((k) => ({ key: k, label: k[0].toUpperCase() + k.slice(1) }))} />
      <div className="card pad">
        {tab === "profile" && (
          <KeyValues items={[["Name", fmt(c.full_name)], ["Location", fmt(c.location)], ["Phone", fmt(c.phone)], ["LinkedIn", c.linkedin_url ? <a className="link" href={String(c.linkedin_url)} target="_blank" rel="noreferrer noopener">profile</a> : null], ["Tags", <Tags values={c.tags} />], ["Status", <Pill value={c.status} />], ["Owner", fmt(c.owner_id)]]} />
        )}
        {tab === "company" && (c.company_id ? <Link className="link" to={`/companies/${c.company_id}`}>Open company →</Link> : <p className="muted">Not linked to a company.</p>)}
        {tab === "score" && <ScorePanel entityType="contact" entityId={contactId} onRecomputed={refresh} />}
        {tab === "role" && <KeyValues items={[["Title", fmt(c.title)], ["Department", fmt(c.department)], ["Function", <Pill value={c.function} />], ["Seniority", fmt(c.seniority)], ["Contact score", <Score value={c.contact_score} />]]} />}
        {tab === "email" && <KeyValues items={[["Email", fmt(c.email)], ["Status", <Pill value={c.email_status} />], ["Score", fmt(c.email_score)], ["Validated", fmtDate(c.email_validated_at)], ["Unsubscribed", fmt(c.unsubscribed)]]} />}
        {tab === "validation" && <KeyValues items={[["Validation status", <Pill value={c.validation_status} />], ["Confidence", fmt(c.confidence)]]} />}
        {tab === "source" && <KeyValues items={[["Source", fmt(c.source)], ["Source date", fmtDate(c.source_date)], ["Provenance", <Link className="link" to={`/provenance/contacts/${c.id}`}>All source records →</Link>]]} />}
        {tab === "activities" && <Embedded path={`/activities?contact_id=${c.id}`} cols={["kind", "summary", "occurred_at"]} />}
        {tab === "sequences" && <Embedded path={`/enrollments?contact_id=${c.id}`} cols={["sequence_id", "status", "current_step", "next_step_at"]} />}
      </div>
    </div>
  );
}

function Embedded({ path, cols }: { path: string; cols: string[] }) {
  const client = useWs();
  return (
    <ResourceList
      load={(query, signal) => client.list(path, query, signal)}
      columns={cols.map((k) => ({ key: k, label: k.replace(/_/g, " "), render: (r: Row) => (k === "status" || k === "kind" ? <Pill value={r[k]} /> : fmt(r[k])) }))}
    />
  );
}

export function Provenance() {
  const { entity = "", entityId = "" } = useParams();
  return (
    <div className="page">
      <PageHeader title="Source records" subtitle={`Where every value of ${entity} ${entityId} came from, with the original values.`} />
      <Embedded path={`/provenance/${entity}/${entityId}`} cols={["source_kind", "source_name", "import_batch_id", "row_number", "match_rule", "observed_at"]} />
    </div>
  );
}

export function Postings() {
  const client = useWs();
  const action = useAction();
  return (
    <div className="page">
      <PageHeader
        title="Jobs"
        subtitle="Every posting from the careers crawler and permitted sources, classified and deduplicated."
        actions={<button className="button button--ghost" disabled={action.busy} onClick={() => action.run(() => client.post("/crawl", { filters: { status: "active" } }))}>Crawl all companies</button>}
      />
      {action.error && <ErrorBanner error={action.error} />}
      <ResourceList
        load={(query, signal) => client.list("/jobs", query, signal)}
        columns={[
          { key: "title", label: "Title", render: (r) => <a className="link" href={String(r.job_url)} target="_blank" rel="noreferrer noopener">{String(r.title)}</a> },
          { key: "company_name", label: "Company", render: (r) => (r.company_id ? <Link className="link" to={`/companies/${r.company_id}`}>{String(r.company_name)}</Link> : String(r.company_name)) },
          { key: "location", label: "Location" },
          { key: "workplace_type", label: "Mode", render: (r) => <Pill value={r.workplace_type} /> },
          { key: "seniority", label: "Seniority" },
          { key: "technologies", label: "Tech", render: (r) => <Tags values={r.technologies} /> },
          { key: "ats", label: "ATS" },
          { key: "first_seen_at", label: "First seen", render: (r) => fmtDate(r.first_seen_at) },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
        ]}
        filters={[
          { key: "status", label: "Status", options: ["open", "closed"] },
          { key: "workplace_type", label: "Mode", options: ["remote", "hybrid", "onsite", "unknown"] },
          { key: "is_relevant", label: "Relevant", options: ["true", "false"] },
          { key: "technologies", label: "Technology", placeholder: "technology (exact)" },
        ]}
      />
    </div>
  );
}

const SIGNAL_TYPES = [
  "NEW_ROLE", "MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE", "HIRING_VELOCITY", "LONG_OPEN_ROLE", "HARD_TO_FILL",
  "SPECIALIZED_TECHNOLOGY", "PROJECT_IMPLEMENTATION", "EXPANSION_HIRING", "BACKFILL_REPLACEMENT", "LEADERSHIP_HIRING",
  "HIRING_CLUSTER", "STACK_MIGRATION", "DEPARTURE",
];
const SIGNAL_OUTCOMES = ["contacted", "replied", "meeting", "opportunity", "no_response", "disqualified"];

/** "replied 2 · meeting 1" from a signal's outcome_counts. */
function outcomeCounts(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  return SIGNAL_OUTCOMES.filter((o) => Number((value as Record<string, unknown>)[o]) > 0)
    .map((o) => `${o.replace(/_/g, " ")} ${Number((value as Record<string, unknown>)[o])}`)
    .join(" · ");
}

/** The closed loop for one signal: what happened after it produced a prospect / campaign. */
function SignalOutcomes({ signal, onClose, onRecorded }: { signal: Row; onClose: () => void; onRecorded: () => void }) {
  const client = useWs();
  const action = useAction();
  const [reload, setReload] = useState(0);
  const [outcome, setOutcome] = useState("meeting");
  const [note, setNote] = useState("");
  const list = useLoad((s) => client.get<{ rows: Row[] }>(`/hiring-signals/${encodeURIComponent(signal.id)}/outcomes`, undefined, s), `${client.base}|outcomes|${signal.id}|${reload}`);
  const rows = list.data?.rows ?? [];
  const record = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      await client.post(`/hiring-signals/${encodeURIComponent(signal.id)}/outcomes`, { outcome, note: note.trim() || undefined });
      setNote("");
      setReload((n) => n + 1);
      onRecorded();
    });
  };
  return (
    <div className="card pad jm-outcomes">
      <div className="title-row">
        <h3>Outcomes · <Pill value={signal.signal_type} /> {String(signal.company_name ?? "") || null}</h3>
        <button type="button" className="button button--ghost button--small" onClick={onClose}>Close</button>
      </div>
      {signal.summary ? <p className="small">{String(signal.summary)}</p> : null}
      {(list.error || action.error) && <ErrorBanner error={(list.error ?? action.error)!} onRetry={list.refresh} />}
      {list.loading && !list.data ? <Loading /> : rows.length === 0 ? (
        <p className="muted small">No outcome yet. Outcomes are written back automatically when a prospect from this signal is contacted, replies, finishes a sequence without a reply, or becomes an opportunity.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Outcome</th><th>When</th><th>Source</th><th>Prospect</th><th>Campaign</th><th>Note</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="table__row">
                  <td data-label="Outcome"><Pill value={r.outcome} /></td>
                  <td data-label="When">{fmt(r.occurred_at)}</td>
                  <td data-label="Source">{String(r.source ?? "")}</td>
                  <td data-label="Prospect">{r.contact_id ? <Link className="link mono small" to={`/contacts/${r.contact_id}`}>{String(r.contact_id)}</Link> : <span className="muted">—</span>}</td>
                  <td data-label="Campaign">{r.campaign_id ? <Link className="link mono small" to={`/campaigns/${r.campaign_id}`}>{String(r.campaign_id)}</Link> : <span className="muted">—</span>}</td>
                  <td data-label="Note" className="small">{String(r.note ?? "") || <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="jm-outcomes__form" onSubmit={record}>
        <label className="field">
          <span className="field__label">Record an outcome</span>
          <select className="input input--small" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            {SIGNAL_OUTCOMES.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
          </select>
        </label>
        <label className="field field--wide">
          <span className="field__label">Note (optional)</span>
          <input className="input input--small" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button type="submit" className="button button--primary button--small" disabled={action.busy}>{action.busy ? "Saving…" : "Record"}</button>
      </form>
    </div>
  );
}

export function HiringIntel({ title = "Hiring intelligence" }: { title?: string }) {
  const client = useWs();
  const action = useAction();
  const [selected, setSelected] = useState<Row | null>(null);
  const [reload, setReload] = useState(0);
  const [jobRun, setJobRun] = useState<Record<string, unknown> | null>(null);
  const counts = useLoad((signal) => client.get<Record<string, unknown>>("/analytics/dashboard", undefined, signal).catch(() => null), client.base + "dash" + reload);
  // The dashboard reports active signals under "signals" (older payloads used "hiring_signals").
  const bySignal = (((counts.data?.signals ?? counts.data?.hiring_signals) as Record<string, unknown> | undefined)?.by_type ?? {}) as Record<string, number>;
  return (
    <div className="page">
      <PageHeader
        title={title}
        subtitle="Evidence-backed signals from jobs, hiring patterns and authorized provider refreshes. Backfill is only claimed when the posting says so; a departure only when the provider reports it."
        actions={
          <>
            <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(async () => { setJobRun(await client.post<Record<string, unknown>>("/signals/jobs/run", {})); setReload((n) => n + 1); })}>Detect job signals now</button>
            <button className="button button--primary" disabled={action.busy} onClick={() => action.run(() => client.post("/signals/run", {}))}>Detect signals now</button>
          </>
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      {jobRun && (
        <p className="alert alert--info small" role="status">
          Job signals: {["HIRING_CLUSTER", "STACK_MIGRATION", "DEPARTURE"].map((t) => `${Number(jobRun[t] ?? 0).toLocaleString()} ${t.replace(/_/g, " ").toLowerCase()}`).join(" · ")}
          {" "}({Number(jobRun.inserted ?? 0).toLocaleString()} new, {Number(jobRun.expired ?? 0).toLocaleString()} expired)
        </p>
      )}
      <div className="stats stats--wrap">
        {SIGNAL_TYPES.map((t) => (
          <Stat key={t} label={t.replace(/_/g, " ").toLowerCase()} value={bySignal[t] ?? 0} />
        ))}
      </div>
      {selected && <SignalOutcomes key={selected.id} signal={selected} onClose={() => setSelected(null)} onRecorded={() => setReload((n) => n + 1)} />}
      <ResourceList
        reloadKey={String(reload)}
        load={(query, signal) => client.list("/hiring-signals", query, signal)}
        columns={[
          { key: "signal_type", label: "Signal", render: (r) => <Pill value={r.signal_type} /> },
          {
            key: "company_id", label: "Company",
            render: (r) => (r.company_id
              ? <Link className="link" to={`/companies/${r.company_id}`}>{String(r.company_name ?? "") || "open"}</Link>
              : r.company_name ? <span title="Not a CRM company yet">{String(r.company_name)}</span> : <span className="muted">—</span>),
          },
          { key: "summary", label: "Evidence summary" },
          { key: "technologies", label: "Technologies", render: (r) => <Tags values={r.technologies} /> },
          { key: "confidence", label: "Confidence" },
          { key: "reason_codes", label: "Reason codes", render: (r) => <Tags values={r.reason_codes} /> },
          { key: "detected_at", label: "Detected", render: (r) => fmtDate(r.detected_at) },
          {
            key: "outcome", label: "Outcome",
            render: (r) => (
              <span>
                {r.outcome ? <Pill value={r.outcome} /> : <span className="muted">—</span>}
                {outcomeCounts(r.outcome_counts) && <span className="muted small jm-block">{outcomeCounts(r.outcome_counts)}</span>}
                <button type="button" className="link-button small" onClick={(e) => { e.stopPropagation(); setSelected(r); }}>Outcomes</button>
              </span>
            ),
          },
        ]}
        filters={[{ key: "signal_type", label: "Signal", options: SIGNAL_TYPES }, { key: "status", label: "Status", options: ["active", "expired", "dismissed"] }]}
      />
    </div>
  );
}

export function Discovery() {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const [urls, setUrls] = useState("");
  const action = useAction();
  const decide = (id: string, verdict: "approve" | "reject") =>
    action.run(async () => {
      await client.post(`/discovery/candidates/${id}/${verdict}`, {});
      setReload((n) => n + 1);
    });
  return (
    <div className="page">
      <PageHeader title="Company discovery" subtitle="Candidates are verified, deduplicated and scored with evidence. Nothing enters the company master until approved." />
      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault();
          const list = urls.split(/\s+/).map((u) => u.trim()).filter(Boolean);
          void action.run(async () => {
            await client.post("/discovery/candidates", { candidates: list.map((website) => ({ name: website, website })), source_kind: "discovery", source_name: "manual web discovery" });
            setUrls("");
            setReload((n) => n + 1);
          });
        }}
      >
        <label className="field field--wide">
          <span className="field__label">Websites to discover (one per line)</span>
          <textarea className="input textarea" rows={3} value={urls} onChange={(e) => setUrls(e.target.value)} placeholder={"acme-manufacturing.com\nexample-industries.com"} />
        </label>
        <div className="form__actions">
          <button className="button button--primary" type="submit" disabled={action.busy || !urls.trim()}>Submit candidates</button>
          <button className="button button--ghost" type="button" disabled={action.busy} onClick={() => action.run(async () => { await client.post("/discovery/run", {}); setReload((n) => n + 1); })}>Verify pending</button>
        </div>
      </form>
      {action.error && <ErrorBanner error={action.error} />}
      <ResourceList
        reloadKey={String(reload)}
        load={(query, signal) => client.list("/discovery/candidates", query, signal)}
        columns={[
          { key: "name", label: "Candidate" },
          { key: "domain", label: "Domain", className: "mono small" },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "match_strength", label: "Match", render: (r) => <Pill value={r.match_strength} /> },
          { key: "website_verified", label: "Site verified" },
          { key: "ats", label: "ATS" },
          { key: "confidence", label: "Confidence" },
          { key: "source_name", label: "Source" },
          {
            key: "actions",
            label: "",
            render: (r) =>
              r.status === "APPROVED" || r.status === "REJECTED" ? null : (
                <span className="actions">
                  <button className="button button--small button--primary" onClick={() => decide(r.id, "approve")}>Approve</button>
                  <button className="button button--small button--ghost" onClick={() => decide(r.id, "reject")}>Reject</button>
                </span>
              ),
          },
        ]}
        filters={[{ key: "status", label: "Status", options: ["NEW_COMPANY_DISCOVERY", "DUPLICATE", "NEEDS_REVIEW", "APPROVED", "REJECTED"] }]}
      />
    </div>
  );
}
