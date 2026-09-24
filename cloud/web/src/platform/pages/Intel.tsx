// Contacts, job postings, hiring intelligence and company discovery.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { CreateForm } from "../ResourcePage";
import { KeyValues, PageHeader, Pill, ResourceList, Score, Stat, Tabs, Tags, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

export function Contacts() {
  const client = useWs();
  const [creating, setCreating] = useState(false);
  const [reload, setReload] = useState(0);
  return (
    <div className="page">
      <PageHeader
        title="Contacts"
        subtitle="People at target companies, with the source and validation status of every email."
        actions={<button className="button button--primary" onClick={() => setCreating((c) => !c)}>{creating ? "Close" : "Add contact"}</button>}
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
      <Tabs active={tab} onChange={setTab} tabs={["profile", "company", "role", "email", "validation", "source", "activities", "sequences"].map((k) => ({ key: k, label: k[0].toUpperCase() + k.slice(1) }))} />
      <div className="card pad">
        {tab === "profile" && (
          <KeyValues items={[["Name", fmt(c.full_name)], ["Location", fmt(c.location)], ["Phone", fmt(c.phone)], ["LinkedIn", c.linkedin_url ? <a className="link" href={String(c.linkedin_url)} target="_blank" rel="noreferrer noopener">profile</a> : null], ["Tags", <Tags values={c.tags} />], ["Status", <Pill value={c.status} />], ["Owner", fmt(c.owner_id)]]} />
        )}
        {tab === "company" && (c.company_id ? <Link className="link" to={`/companies/${c.company_id}`}>Open company →</Link> : <p className="muted">Not linked to a company.</p>)}
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
        actions={<button className="button button--ghost" disabled={action.busy} onClick={() => action.run(() => client.post("/crawl", { all: true }))}>Crawl all companies</button>}
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

const SIGNAL_TYPES = ["NEW_ROLE", "MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE", "HIRING_VELOCITY", "LONG_OPEN_ROLE", "HARD_TO_FILL", "SPECIALIZED_TECHNOLOGY", "PROJECT_IMPLEMENTATION", "EXPANSION_HIRING", "BACKFILL_REPLACEMENT", "LEADERSHIP_HIRING"];

export function HiringIntel() {
  const client = useWs();
  const action = useAction();
  const counts = useLoad((signal) => client.get<Record<string, unknown>>("/analytics/dashboard", undefined, signal).catch(() => null), client.base + "dash");
  const bySignal = ((counts.data?.hiring_signals as Record<string, unknown> | undefined)?.by_type ?? {}) as Record<string, number>;
  return (
    <div className="page">
      <PageHeader
        title="Hiring intelligence"
        subtitle="Eleven evidence-backed signals. Backfill is only claimed when the posting says so."
        actions={<button className="button button--primary" disabled={action.busy} onClick={() => action.run(() => client.post("/signals/run", {}))}>Detect signals now</button>}
      />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="stats stats--wrap">
        {SIGNAL_TYPES.map((t) => (
          <Stat key={t} label={t.replace(/_/g, " ").toLowerCase()} value={bySignal[t] ?? 0} />
        ))}
      </div>
      <ResourceList
        load={(query, signal) => client.list("/hiring-signals", query, signal)}
        columns={[
          { key: "signal_type", label: "Signal", render: (r) => <Pill value={r.signal_type} /> },
          { key: "company_id", label: "Company", render: (r) => <Link className="link" to={`/companies/${r.company_id}`}>open</Link> },
          { key: "summary", label: "Evidence summary" },
          { key: "confidence", label: "Confidence" },
          { key: "reason_codes", label: "Reason codes", render: (r) => <Tags values={r.reason_codes} /> },
          { key: "detected_at", label: "Detected", render: (r) => fmtDate(r.detected_at) },
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
