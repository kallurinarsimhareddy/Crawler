import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { useAssistant } from "../../shell/Assistant";
import type { PageOf, Row } from "../api";
import { CreateForm } from "../ResourcePage";
import { OPPORTUNITY_COLUMNS } from "../resources";
import { DataTable, Json, KeyValues, PageHeader, Pill, ResourceList, Score, Stat, Tabs, Tags, fmt, fmtDate, useAction, useLoad, type Empty } from "../ui";
import { useWs } from "../workspace";

const COMPANY_FIELDS = [
  { key: "name", label: "Company name", required: true },
  { key: "website", label: "Website", placeholder: "acme.com" },
  { key: "industry", label: "Industry" },
  { key: "country", label: "Country" },
  { key: "state", label: "State" },
  { key: "city", label: "City" },
  { key: "employee_range", label: "Employees" },
  { key: "revenue_range", label: "Revenue" },
];

export function Companies() {
  const client = useWs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const creating = params.get("create") === "1";
  const setCreating = (open: boolean) => {
    const next = new URLSearchParams(params);
    if (open) next.set("create", "1");
    else next.delete("create");
    setParams(next, { replace: true });
  };
  return (
    <div className="page">
      <PageHeader
        title="Companies"
        subtitle="The company master: every record deduplicated, scored and traceable to its sources."
        actions={creating ? <button className="button button--ghost button--small" onClick={() => setCreating(false)}>Close form</button> : undefined}
      />
      {creating && (
        <CreateForm
          fields={COMPANY_FIELDS}
          path="/companies"
          label="Add (deduplicated)"
          onCreated={(row) => {
            const company = (row as unknown as { company?: Row }).company ?? row;
            if (company?.id) navigate(`/companies/${company.id}`);
          }}
        />
      )}
      <ResourceList
        load={(query, signal) => client.list("/companies", { order: "-opportunity_score", ...query }, signal)}
        link={(r) => `/companies/${r.id}`}
        columns={[
          { key: "name", label: "Company" },
          { key: "domain", label: "Domain", className: "mono small" },
          { key: "industry", label: "Industry" },
          { key: "state", label: "Location", render: (r) => [r.city, r.state, r.country].filter(Boolean).join(", ") || "—" },
          { key: "hiring_count", label: "Open jobs", className: "tabular" },
          { key: "hiring_signals", label: "Signals", render: (r) => <Tags values={r.hiring_signals} /> },
          { key: "opportunity_score", label: "Opportunity", render: (r) => <Score value={r.opportunity_score} /> },
          { key: "lifecycle", label: "Stage", render: (r) => <Pill value={r.lifecycle} /> },
        ]}
        filters={[
          { key: "lifecycle", label: "Lifecycle", options: ["prospect", "account", "customer", "partner", "disqualified"] },
          { key: "industry__ilike", label: "Industry", placeholder: "industry contains…" },
          { key: "technologies", label: "Technology", placeholder: "technology (exact)" },
          { key: "country", label: "Country", placeholder: "country" },
        ]}
        empty={{
          title: "No companies yet",
          description: "Import company files, discover companies from websites, or add one by hand. Every record is deduplicated and scored.",
          icon: "building",
          action: (
            <span className="actions">
              <button className="button button--primary" onClick={() => setCreating(true)}>+ Add Company</button>
              <Link className="button button--ghost" to="/imports">Import</Link>
              <Link className="button button--ghost" to="/prospecting?tab=discover">Discover companies</Link>
            </span>
          ),
        }}
      />
    </div>
  );
}

function SubList({ path, columns, link, empty }: { path: string; columns: Parameters<typeof DataTable>[0]["columns"]; link?: (r: Row) => string; empty: Empty }) {
  const client = useWs();
  const { data, error, loading, refresh } = useLoad((signal) => client.list(path, { limit: 100 }, signal), client.base + path);
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (loading && !data) return <Loading />;
  return <DataTable rows={(data as PageOf).items} columns={columns} link={link} empty={empty} />;
}

export function CompanyDetail() {
  const { companyId = "" } = useParams();
  const client = useWs();
  const [tab, setTab] = useState("overview");
  const assistant = useAssistant();
  const { data: company, error, loading, refresh } = useLoad((signal) => client.get<Row>(`/companies/${companyId}`, undefined, signal), client.base + companyId);
  const scores = useLoad((signal) => client.get<Record<string, unknown>>(`/companies/${companyId}/scores`, undefined, signal).catch(() => null), client.base + companyId + "scores");
  const action = useAction();

  if (error) return <div className="page"><ErrorBanner error={error} onRetry={refresh} /></div>;
  if (loading || !company) return <div className="page"><Loading /></div>;
  const c = company;
  const breakdown = (c.score_breakdown ?? {}) as Record<string, unknown>;

  return (
    <div className="page">
      <PageHeader
        title={String(c.name)}
        subtitle={
          <>
            {c.domain ? <a className="link" href={String(c.website ?? `https://${c.domain}`)} target="_blank" rel="noreferrer noopener">{String(c.domain)}</a> : "no domain"}
            {" · "}
            {[c.industry, c.city, c.state, c.country].filter(Boolean).join(" · ") || "no firmographics yet"}
          </>
        }
        actions={
          <>
            <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(async () => { await client.post(`/companies/${companyId}/scores`); refresh(); scores.refresh(); })}>
              Recompute scores
            </button>
            <button className="button button--ghost" disabled={action.busy} onClick={() => action.run(() => client.post("/crawl", { company_ids: [companyId] }))}>
              Crawl careers
            </button>
            <button className="button button--primary" disabled={action.busy} onClick={() => action.run(() => client.post("/contacts/find", { company_ids: [companyId], allow_paid: false }))}>
              Find contacts (free sources)
            </button>
          </>
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="stats">
        <Stat label="Account score" value={<Score value={c.account_score} />} />
        <Stat label="Hiring score" value={<Score value={c.hiring_score} />} />
        <Stat label="Opportunity score" value={<Score value={c.opportunity_score} />} />
        <Stat label="Open jobs" value={fmt(c.hiring_count)} hint={c.ats ? `ATS: ${c.ats}` : undefined} />
      </div>
      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { key: "overview", label: "Overview" },
          { key: "contacts", label: "People" },
          { key: "jobs", label: "Jobs" },
          { key: "technology", label: "Technologies" },
          { key: "signals", label: "Hiring Signals" },
          { key: "activities", label: "Activities" },
          { key: "notes", label: "Notes" },
          { key: "campaigns", label: "Campaigns" },
          { key: "research", label: "Research" },
          { key: "opportunities", label: "Deals" },
          { key: "sources", label: "Sources" },
        ]}
      />
      <div className="card pad">
        {tab === "overview" && (
          <div className="detail-grid">
            <KeyValues
              items={[
                ["Legal name", fmt(c.legal_name)],
                ["Aliases", <Tags values={c.aliases} />],
                ["Website", fmt(c.website)],
                ["Careers page", c.careers_url ? <a className="link" href={String(c.careers_url)} target="_blank" rel="noreferrer noopener">{String(c.careers_url)}</a> : null],
                ["Industry", fmt(c.industry)],
                ["SIC / NAICS", [fmt(c.sic_codes), fmt(c.naics_codes)].join(" / ")],
                ["Employees", fmt(c.employee_range ?? c.employee_count)],
                ["Revenue", fmt(c.revenue_range)],
                ["Technologies", <Tags values={c.technologies} />],
                ["Hiring velocity", fmt(c.hiring_velocity)],
                ["Lifecycle", <Pill value={c.lifecycle} />],
                ["Tags", <Tags values={c.tags} />],
                ["Confidence", fmt(c.confidence)],
                ["First seen", fmtDate(c.first_seen_at)],
                ["Last seen", fmtDate(c.last_seen_at)],
                ["Sources", fmt(c.source_count)],
              ]}
            />
            <div>
              <h3>Why this score</h3>
              <p className="muted small">Every score is a weighted sum of named components — no opaque AI score.</p>
              <Json value={scores.data ?? breakdown} />
            </div>
          </div>
        )}
        {tab === "jobs" && (
          <SubList
            path={`/jobs?company_id=${companyId}`}
            empty="No jobs recorded for this company yet. Run a careers crawl."
            columns={[
              { key: "title", label: "Title", render: (r) => <a className="link" href={String(r.job_url)} target="_blank" rel="noreferrer noopener">{String(r.title)}</a> },
              { key: "location", label: "Location" },
              { key: "workplace_type", label: "Mode", render: (r) => <Pill value={r.workplace_type} /> },
              { key: "seniority", label: "Seniority" },
              { key: "technologies", label: "Technologies", render: (r) => <Tags values={r.technologies} /> },
              { key: "first_seen_at", label: "First seen", render: (r) => fmtDate(r.first_seen_at) },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            ]}
          />
        )}
        {tab === "contacts" && <CompanyContacts companyId={companyId} />}
        {tab === "technology" && (
          <SubList
            path={`/company-technologies?company_id=${companyId}`}
            empty="No technology evidence yet."
            columns={[
              { key: "technology", label: "Technology" },
              { key: "category", label: "Category", render: (r) => <Pill value={r.category} /> },
              { key: "source", label: "Source" },
              { key: "evidence_text", label: "Evidence", render: (r) => (r.evidence_url ? <a className="link" href={String(r.evidence_url)} target="_blank" rel="noreferrer noopener">{fmt(r.evidence_text)}</a> : fmt(r.evidence_text)) },
              { key: "observed_at", label: "Observed", render: (r) => fmtDate(r.observed_at) },
              { key: "confidence", label: "Confidence" },
            ]}
          />
        )}
        {tab === "signals" && (
          <SubList
            path={`/hiring-signals?company_id=${companyId}`}
            empty="No hiring signals detected."
            columns={[
              { key: "signal_type", label: "Signal", render: (r) => <Pill value={r.signal_type} /> },
              { key: "summary", label: "Summary" },
              { key: "confidence", label: "Confidence" },
              { key: "reason_codes", label: "Reasons", render: (r) => <Tags values={r.reason_codes} /> },
              { key: "detected_at", label: "Detected", render: (r) => fmtDate(r.detected_at) },
            ]}
          />
        )}
        {tab === "activities" && <Timeline companyId={companyId} />}
        {tab === "notes" && <CompanyNotes companyId={companyId} />}
        {tab === "campaigns" && (
          <>
            <p className="muted small">Campaign involvement is tracked through this company's deals that belong to a campaign.</p>
            <SubList
              path={`/opportunities?company_id=${companyId}`}
              link={(r) => `/opportunities/${r.id}`}
              empty={{
                title: "Not in a campaign yet",
                description: "Add this account to a campaign to start targeted outreach. Every send waits for approval.",
                icon: "megaphone",
                action: <button className="button button--primary button--small" onClick={() => assistant.show(`Create a campaign for ${String(c.name)}`)}>Create a campaign with AI</button>,
              }}
              columns={[
                { key: "name", label: "Deal" },
                { key: "campaign_id", label: "Campaign", className: "mono small" },
                { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
                { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
              ]}
            />
          </>
        )}
        {tab === "research" && (
          <EmptyState
            icon="bot"
            title={`Research ${String(c.name)}`}
            description="Ask the research agent about this company — hiring plans, technology, leaders and buying signals — or scrape its website for structured data."
            action={
              <span className="actions">
                <button className="button button--primary" onClick={() => assistant.show(`Research ${String(c.name)}${c.domain ? ` (${String(c.domain)})` : ""}: hiring, technologies, IT leaders and buying signals.`)}>Research with AI</button>
                {c.website || c.domain ? <Link className="button button--ghost" to={`/scraper?urls=${encodeURIComponent(String(c.website ?? `https://${String(c.domain)}`))}`}>Scrape the website</Link> : null}
                <Link className="button button--ghost" to="/research">Research agent</Link>
              </span>
            }
          />
        )}
        {tab === "opportunities" && (
          <SubList path={`/opportunities?company_id=${companyId}`} columns={OPPORTUNITY_COLUMNS as never} link={(r) => `/opportunities/${r.id}`} empty="No opportunities." />
        )}
        {tab === "sources" && (
          <SubList
            path={`/provenance/companies/${companyId}`}
            empty="No provenance records."
            columns={[
              { key: "source_kind", label: "Kind", render: (r) => <Pill value={r.source_kind} /> },
              { key: "source_name", label: "Source" },
              { key: "row_number", label: "Row" },
              { key: "match_rule", label: "Matched by" },
              { key: "observed_at", label: "Observed", render: (r) => fmtDate(r.observed_at) },
              { key: "original", label: "Original values", render: (r) => <code className="small">{JSON.stringify(r.original).slice(0, 160)}</code> },
            ]}
          />
        )}
      </div>
    </div>
  );
}

function CompanyNotes({ companyId }: { companyId: string }) {
  const client = useWs();
  const [text, setText] = useState("");
  const [reload, setReload] = useState(0);
  const action = useAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void action.run(async () => {
      await client.post("/activities", { kind: "note", summary: text.trim(), company_id: companyId });
      setText("");
      setReload((n) => n + 1);
    });
  };
  return (
    <>
      <form className="note-form" onSubmit={submit}>
        <label htmlFor="note" className="sr-only">Add a note</label>
        <textarea id="note" className="input textarea" rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="Add a note about this company…" />
        <button className="button button--primary" type="submit" disabled={action.busy || !text.trim()}>{action.busy ? "Saving…" : "Add note"}</button>
      </form>
      {action.error && <ErrorBanner error={action.error} />}
      <SubList
        key={reload}
        path={`/activities?company_id=${companyId}&kind=note`}
        empty={{ title: "No notes yet", description: "Notes you add here are saved to this company's activity timeline.", icon: "note" }}
        columns={[
          { key: "summary", label: "Note" },
          { key: "occurred_at", label: "Added", render: (r) => fmtDate(r.occurred_at ?? r.created_at) },
        ]}
      />
    </>
  );
}

function CompanyContacts({ companyId }: { companyId: string }) {
  const client = useWs();
  const gaps = useLoad((signal) => client.get<Record<string, unknown>>(`/companies/${companyId}/contact-gaps`, undefined, signal).catch(() => null), client.base + "gaps" + companyId);
  const gapRows = gaps.data && typeof gaps.data === "object" ? Object.entries((gaps.data.functions ?? gaps.data) as Record<string, { status?: string }>) : [];
  return (
    <>
      {gapRows.length > 0 && (
        <div className="gap-grid">
          {gapRows.map(([fn, info]) => (
            <div key={fn} className="gap">
              <span className="small muted">{fn.replace(/_/g, " ")}</span>
              <Pill value={(info && typeof info === "object" ? info.status : info) as string} />
            </div>
          ))}
        </div>
      )}
      <SubList
        path={`/contacts?company_id=${companyId}`}
        link={(r) => `/contacts/${r.id}`}
        empty="No contacts yet."
        columns={[
          { key: "full_name", label: "Name" },
          { key: "title", label: "Title" },
          { key: "function", label: "Function" },
          { key: "email", label: "Email", className: "mono small" },
          { key: "email_status", label: "Email status", render: (r) => <Pill value={r.email_status} /> },
          { key: "source", label: "Source" },
        ]}
      />
    </>
  );
}

function Timeline({ companyId }: { companyId: string }) {
  const client = useWs();
  const { data, error, loading } = useLoad((signal) => client.get<{ items: Row[] } | Row[]>(`/companies/${companyId}/timeline`, undefined, signal), client.base + "tl" + companyId);
  if (error) return <ErrorBanner error={error} />;
  if (loading && !data) return <Loading />;
  const items = (Array.isArray(data) ? data : data?.items ?? []) as Row[];
  if (items.length === 0) return <EmptyState icon="activity" title="Nothing on the timeline yet" description="Imports, calls, emails, stage changes and signals for this company appear here in order." />;
  return (
    <ol className="timeline">
      {items.map((item, i) => (
        <li key={String(item.id ?? i)} className="timeline__item">
          <span className="timeline__time">{fmt(item.occurred_at ?? item.created_at ?? item.detected_at)}</span>
          <strong>{String(item.kind ?? item.type ?? item.change_type ?? "event").replace(/_/g, " ")}</strong> — {String(item.summary ?? item.title ?? item.body ?? "")}
        </li>
      ))}
    </ol>
  );
}
