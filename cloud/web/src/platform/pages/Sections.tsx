// Section pages: one header and contextual tabs over the existing pages, so related
// work sits together (Companies › Lists / Segments / Signals…) instead of as
// separate top-level modules. Every tab is linkable (?tab=…).

import { Link } from "react-router-dom";
import { EmptyState } from "../../components/Feedback";
import { useAssistant } from "../../shell/Assistant";
import { Icon } from "../../shell/Icon";
import type { Row } from "../api";
import { Bars, Sparkline, counts, num } from "../charts";
import { MemoryPage } from "../controlroom/Memory";
import { ResourcePage } from "../ResourcePage";
import { ACTIVITIES, CAMPAIGNS, LISTS, SEGMENTS, SEQUENCES, SUPPRESSIONS, TEMPLATES } from "../resources";
import { SectionPage } from "../Section";
import { DataTable, Pill, ResourceList, Score, Tags, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import { Companies } from "./Companies";
import { Contacts, Discovery, HiringIntel, Postings } from "./Intel";
import { Credits, Research, Settings } from "./Tools";
import { Analytics, BackgroundTasks, Dashboard } from "./Work";

type Dash = Record<string, unknown>;

function useDashboard(key: string) {
  const client = useWs();
  return useLoad((s) => client.get<Dash>("/analytics/dashboard", undefined, s).catch(() => null), client.base + "dash:" + key);
}

function Summary({ items }: { items: { label: string; value: number; hint?: string; to?: string; points?: { date: string; count: number }[] }[] }) {
  return (
    <div className="metrics metrics--compact">
      {items.map((m) => {
        const body = (
          <>
            <span className="metric__label">{m.label}</span>
            <span className="metric__row">
              <span className="metric__value tabular">{m.value.toLocaleString()}</span>
              {m.points && m.points.some((p) => p.count > 0) && <Sparkline points={m.points} label={m.label} />}
            </span>
            {m.hint && <span className="metric__hint">{m.hint}</span>}
          </>
        );
        return m.to ? (
          <Link key={m.label} to={m.to} className="metric">{body}</Link>
        ) : (
          <div key={m.label} className="metric">{body}</div>
        );
      })}
    </div>
  );
}

function ImportExport() {
  return (
    <>
      <Link className="button button--ghost" to="/imports"><Icon name="upload" size={16} /> Import</Link>
      <Link className="button button--ghost" to="/exports"><Icon name="download" size={16} /> Export</Link>
    </>
  );
}

// --- CRM -------------------------------------------------------------------------------------

function CompaniesOverview() {
  const client = useWs();
  const dash = useDashboard("companies");
  const d = dash.data;
  const series = (d?.series ?? {}) as Record<string, { date: string; count: number }[]>;
  const top = useLoad((s) => client.list("/companies", { order: "-opportunity_score", limit: 8 }, s), client.base + "co-top");
  return (
    <>
      <Summary
        items={[
          { label: "Companies", value: num(d, "companies", "total"), points: series.companies, to: "?tab=all" },
          { label: "Hiring now", value: num(d, "companies", "with_open_jobs"), hint: "with open jobs" },
          { label: "Discovery candidates", value: num(d, "companies", "discovered"), to: "/prospecting?tab=discover" },
          { label: "Duplicates merged", value: num(d, "companies", "merged_duplicates") },
        ]}
      />
      <div className="grid-2">
        <section className="card panel">
          <header className="panel__head"><h2>Top opportunities</h2><Link className="link small" to="?tab=all">All companies</Link></header>
          <DataTable
            rows={top.data?.items ?? []}
            link={(r) => `/companies/${r.id}`}
            empty={{ title: "No companies yet", description: "Import or discover companies to see them ranked by opportunity.", icon: "building", action: <Link className="button button--primary button--small" to="?create=1">+ Add Company</Link> }}
            columns={[
              { key: "name", label: "Company" },
              { key: "industry", label: "Industry" },
              { key: "hiring_count", label: "Open jobs", className: "tabular" },
              { key: "opportunity_score", label: "Opportunity", render: (r) => <Score value={r.opportunity_score} /> },
            ]}
          />
        </section>
        <section className="card panel">
          <header className="panel__head"><h2>By lifecycle</h2></header>
          <Bars data={counts((d?.companies as Row | undefined)?.by_lifecycle)} empty="No companies yet." />
          <header className="panel__head"><h2>By source</h2></header>
          <Bars data={counts((d?.companies as Row | undefined)?.by_source_kind)} empty="No sources recorded yet." />
        </section>
      </div>
    </>
  );
}

export function CompaniesSection() {
  return (
    <SectionPage
      title="Companies"
      subtitle="Every account, deduplicated, scored and traceable to its sources."
      defaultTab="all"
      actions={
        <>
          <ImportExport />
          <Link className="button button--primary" to="?create=1"><Icon name="plus" size={16} /> Add Company</Link>
        </>
      }
      tabs={[
        { key: "overview", label: "Overview", render: () => <CompaniesOverview /> },
        { key: "all", label: "All Companies", render: () => <Companies /> },
        { key: "lists", label: "Lists", render: () => <ResourcePage config={LISTS} query={{ entity_type: "companies" }} emptyTitle="No company lists yet" /> },
        { key: "segments", label: "Segments", render: () => <ResourcePage config={SEGMENTS} query={{ entity_type: "companies" }} emptyTitle="No company segments yet" /> },
        { key: "signals", label: "Signals", render: () => <HiringIntel /> },
        { key: "activities", label: "Activities", render: () => <ResourcePage config={ACTIVITIES} /> },
      ]}
    />
  );
}

export function ContactsSection() {
  return (
    <SectionPage
      title="Contacts"
      subtitle="People at your target companies, with the source and validation status of every email."
      actions={
        <>
          <ImportExport />
          <Link className="button button--primary" to="?create=1"><Icon name="plus" size={16} /> Add Contact</Link>
        </>
      }
      tabs={[
        { key: "all", label: "All Contacts", render: () => <Contacts /> },
        { key: "lists", label: "Lists", render: () => <ResourcePage config={LISTS} query={{ entity_type: "contacts" }} emptyTitle="No contact lists yet" /> },
        { key: "segments", label: "Segments", render: () => <ResourcePage config={SEGMENTS} query={{ entity_type: "contacts" }} emptyTitle="No contact segments yet" /> },
        { key: "activities", label: "Activities", render: () => <ResourcePage config={ACTIVITIES} /> },
      ]}
    />
  );
}

// --- GTM -------------------------------------------------------------------------------------

function CompanySearch() {
  const client = useWs();
  return (
    <ResourceList
      filtersOpen
      load={(query, signal) => client.list("/companies", { order: "-opportunity_score", ...query }, signal)}
      link={(r) => `/companies/${r.id}`}
      empty={{
        title: "No companies to search yet",
        description: "Discover new companies from websites, import a file, or ask SANA GTM AI to find companies that match your ideal customer.",
        icon: "search",
        action: <Link className="button button--primary" to="?tab=discover">Discover companies</Link>,
      }}
      columns={[
        { key: "name", label: "Company" },
        { key: "domain", label: "Domain", className: "mono small" },
        { key: "industry", label: "Industry" },
        { key: "state", label: "Location", render: (r) => [r.city, r.state, r.country].filter(Boolean).join(", ") || "—" },
        { key: "technologies", label: "Technologies", render: (r) => <Tags values={r.technologies} /> },
        { key: "hiring_count", label: "Open jobs", className: "tabular" },
        { key: "opportunity_score", label: "Opportunity", render: (r) => <Score value={r.opportunity_score} /> },
      ]}
      filters={[
        { key: "industry__ilike", label: "Industry", placeholder: "industry contains…" },
        { key: "technologies", label: "Technology", placeholder: "technology (exact), e.g. SAP" },
        { key: "country", label: "Country", placeholder: "country" },
        { key: "state", label: "State", placeholder: "state" },
        { key: "lifecycle", label: "Lifecycle", options: ["prospect", "account", "customer", "partner", "disqualified"] },
      ]}
    />
  );
}

function SavedSearches() {
  const client = useWs();
  const assistant = useAssistant();
  const action = useAction();
  const saved = useLoad((s) => client.list("/agent/saved", undefined, s), client.base + "saved");
  const items = saved.data?.items ?? [];
  return (
    <div className="card">
      <DataTable
        rows={items}
        empty={{
          title: "No saved searches yet",
          description: "Ask SANA GTM AI to find companies, then save the request to run it again any time.",
          icon: "search",
          action: <button type="button" className="button button--primary" onClick={() => assistant.show("Find manufacturing companies using SAP.")}>Ask SANA GTM AI</button>,
        }}
        columns={[
          { key: "name", label: "Name" },
          { key: "request", label: "Request", render: (r) => <span className="muted small">{String(r.request).slice(0, 120)}</span> },
          { key: "kind", label: "Kind", render: (r) => <Pill value={r.kind} /> },
          {
            key: "actions",
            label: "",
            render: (r) => (
              <span className="actions">
                <button type="button" className="button button--ghost button--small" onClick={() => assistant.show(String(r.request), true)}>Run</button>
                <button type="button" className="button button--ghost button--small" disabled={action.busy} aria-label={`Delete ${String(r.name)}`} onClick={() => action.run(async () => { await client.del(`/agent/saved/${r.id}`); saved.refresh(); })}>Delete</button>
              </span>
            ),
          },
        ]}
      />
    </div>
  );
}

export function ProspectingSection() {
  const assistant = useAssistant();
  return (
    <SectionPage
      title="Prospecting"
      subtitle="Find the accounts worth your time: search what you have, discover new companies, and build lists."
      actions={
        <button type="button" className="button button--primary" onClick={() => assistant.show("Find ")}>
          <Icon name="sparkles" size={16} /> Find companies with AI
        </button>
      }
      tabs={[
        { key: "search", label: "Search", render: () => <CompanySearch /> },
        { key: "discover", label: "Discover", render: () => <Discovery /> },
        { key: "saved", label: "Saved Searches", render: () => <SavedSearches /> },
        { key: "lists", label: "Lists", render: () => <ResourcePage config={LISTS} /> },
      ]}
    />
  );
}

function CampaignPerformance() {
  const dash = useDashboard("campaigns");
  const c = (dash.data?.campaigns ?? {}) as Row;
  const rows = ((c.campaigns ?? []) as Row[]).map((r) => ({ ...r, id: String(r.campaign_id) }) as Row);
  return (
    <>
      <Summary
        items={[
          { label: "Campaigns", value: num(c, "total") },
          { label: "Messages sent", value: num(c, "message_events", "sent") },
          { label: "Replies", value: num(c, "message_events", "replied") },
          { label: "Suppressed addresses", value: num(c, "suppressions"), to: "?tab=suppressions" },
        ]}
      />
      <div className="card">
        <DataTable
          rows={rows}
          empty={{ title: "No campaign results yet", description: "Performance appears once a campaign has enrollments and messages.", icon: "chart" }}
          columns={[
            { key: "name", label: "Campaign" },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "enrollments", label: "Enrollments", className: "tabular" },
            { key: "opportunities", label: "Deals", className: "tabular" },
            { key: "reply_rate", label: "Reply rate", render: (r) => (typeof r.reply_rate === "number" ? `${(r.reply_rate * 100).toFixed(1)}%` : "—") },
            { key: "bounce_rate", label: "Bounce rate", render: (r) => (typeof r.bounce_rate === "number" ? `${(r.bounce_rate * 100).toFixed(1)}%` : "—") },
            { key: "sending_enabled", label: "Sending", render: (r) => (r.sending_enabled ? "Enabled" : "Off") },
          ]}
        />
      </div>
    </>
  );
}

export function CampaignsSection() {
  return (
    <SectionPage
      title="Campaigns"
      subtitle="Target accounts with a message. Nothing is ever sent without an explicit, approved campaign action."
      tabs={[
        { key: "all", label: "Campaigns", render: () => <ResourcePage config={CAMPAIGNS} /> },
        { key: "drafts", label: "Drafts", render: () => <ResourcePage config={CAMPAIGNS} query={{ status: "draft" }} emptyTitle="No draft campaigns" /> },
        { key: "templates", label: "Templates", render: () => <ResourcePage config={TEMPLATES} /> },
        { key: "performance", label: "Performance", render: () => <CampaignPerformance /> },
        { key: "suppressions", label: "Suppression list", render: () => <ResourcePage config={SUPPRESSIONS} /> },
      ]}
    />
  );
}

export function SequencesSection() {
  return (
    <SectionPage
      title="Sequences"
      subtitle="Multi-step outreach. Enrollments wait for approval; development and staging never send email."
      tabs={[
        { key: "active", label: "Active", render: () => <ResourcePage config={SEQUENCES} query={{ status: "active" }} emptyTitle="No active sequences" /> },
        { key: "drafts", label: "Drafts", render: () => <ResourcePage config={SEQUENCES} query={{ status: "draft" }} emptyTitle="No draft sequences" /> },
        { key: "templates", label: "Templates", render: () => <ResourcePage config={TEMPLATES} /> },
        { key: "all", label: "All", render: () => <ResourcePage config={SEQUENCES} /> },
      ]}
    />
  );
}

// --- Intelligence ----------------------------------------------------------------------------

function HiringOverview() {
  const dash = useDashboard("hiring");
  const d = dash.data;
  const series = (d?.series ?? {}) as Record<string, { date: string; count: number }[]>;
  return (
    <>
      <Summary
        items={[
          { label: "Open jobs", value: num(d, "jobs", "open"), points: series.job_postings, to: "?tab=jobs" },
          { label: "Relevant jobs", value: num(d, "jobs", "relevant") },
          { label: "Active signals", value: num(d, "signals", "active"), points: series.hiring_signals, to: "?tab=signals" },
          { label: "Companies hiring", value: num(d, "companies", "with_open_jobs"), to: "?tab=companies" },
        ]}
      />
      <div className="grid-2">
        <section className="card panel">
          <header className="panel__head"><h2>Signals by type</h2><Link className="link small" to="?tab=signals">All signals</Link></header>
          <Bars data={counts((d?.signals as Row | undefined)?.by_type)} empty="No active signals. Run signal detection from the Signals tab." />
        </section>
        <section className="card panel">
          <header className="panel__head"><h2>Top technologies in open jobs</h2><Link className="link small" to="?tab=technology">Technology</Link></header>
          <Bars data={counts((d?.jobs as Row | undefined)?.top_technologies)} empty="No open jobs with technologies yet." />
        </section>
      </div>
    </>
  );
}

function HiringCompanies() {
  const client = useWs();
  return (
    <ResourceList
      load={(query, signal) => client.list("/companies", { order: "-hiring_score", hiring_count__gt: 0, ...query }, signal)}
      link={(r) => `/companies/${r.id}`}
      empty={{ title: "No companies are hiring yet", description: "Crawl careers pages or import jobs to see which companies are hiring and how fast.", icon: "trend", action: <Link className="button button--ghost" to="?tab=jobs">Go to jobs</Link> }}
      columns={[
        { key: "name", label: "Company" },
        { key: "hiring_count", label: "Open jobs", className: "tabular" },
        { key: "hiring_velocity", label: "Velocity" },
        { key: "hiring_signals", label: "Signals", render: (r) => <Tags values={r.hiring_signals} /> },
        { key: "hiring_score", label: "Hiring score", render: (r) => <Score value={r.hiring_score} /> },
        { key: "ats", label: "ATS" },
      ]}
      filters={[{ key: "industry__ilike", label: "Industry", placeholder: "industry contains…" }, { key: "technologies", label: "Technology", placeholder: "technology (exact)" }]}
    />
  );
}

function TechnologyTab() {
  const client = useWs();
  const dash = useDashboard("tech");
  return (
    <>
      <section className="card panel">
        <header className="panel__head"><h2>Most requested in open jobs</h2></header>
        <Bars data={counts((dash.data?.jobs as Row | undefined)?.top_technologies)} limit={15} empty="No open jobs with technologies yet." />
      </section>
      <ResourceList
        load={(query, signal) => client.list("/company-technologies", query, signal)}
        empty={{ title: "No technology evidence yet", description: "Technologies are detected from job postings, websites and imports — with the evidence for each.", icon: "database" }}
        columns={[
          { key: "technology", label: "Technology" },
          { key: "category", label: "Category", render: (r) => <Pill value={r.category} /> },
          { key: "company_id", label: "Company", render: (r) => <Link className="link" to={`/companies/${r.company_id}`}>open</Link> },
          { key: "source", label: "Source" },
          { key: "evidence_text", label: "Evidence", render: (r) => <span className="muted small">{fmt(r.evidence_text)}</span> },
          { key: "observed_at", label: "Observed", render: (r) => fmtDate(r.observed_at) },
        ]}
        filters={[{ key: "technology", label: "Technology", placeholder: "technology (exact)" }, { key: "category", label: "Category", placeholder: "category" }]}
      />
    </>
  );
}

function TrendsTab() {
  const dash = useDashboard("trends");
  const d = dash.data;
  const series = (d?.series ?? {}) as Record<string, { date: string; count: number }[]>;
  const total = (name: string) => (series[name] ?? []).reduce((sum, p) => sum + p.count, 0);
  return (
    <>
      <Summary
        items={[
          { label: "New jobs · 30 days", value: total("job_postings"), points: series.job_postings },
          { label: "New signals · 30 days", value: total("hiring_signals"), points: series.hiring_signals },
          { label: "New companies · 30 days", value: total("companies"), points: series.companies },
        ]}
      />
      <div className="grid-2">
        <section className="card panel">
          <header className="panel__head"><h2>Jobs by workplace</h2></header>
          <Bars data={counts((d?.jobs as Row | undefined)?.by_workplace_type)} />
        </section>
        <section className="card panel">
          <header className="panel__head"><h2>Jobs by source</h2></header>
          <Bars data={counts((d?.jobs as Row | undefined)?.by_source)} />
        </section>
      </div>
    </>
  );
}

export function HiringSection() {
  return (
    <SectionPage
      title="Hiring Intelligence"
      subtitle="Who is hiring, for what, and how fast — with evidence for every signal."
      tabs={[
        { key: "overview", label: "Overview", render: () => <HiringOverview /> },
        { key: "signals", label: "Signals", render: () => <HiringIntel /> },
        { key: "companies", label: "Companies", render: () => <HiringCompanies /> },
        { key: "jobs", label: "Jobs", render: () => <Postings /> },
        { key: "technology", label: "Technology", render: () => <TechnologyTab /> },
        { key: "trends", label: "Trends", render: () => <TrendsTab /> },
      ]}
    />
  );
}

function SavedResearch() {
  const client = useWs();
  const assistant = useAssistant();
  const saved = useLoad((s) => client.list("/agent/saved", undefined, s), client.base + "saved-research");
  return (
    <div className="card">
      <DataTable
        rows={saved.data?.items ?? []}
        empty={{ title: "No saved research yet", description: "Save a request in the AI workspace to rerun it later with one click.", icon: "bot", action: <Link className="button button--ghost" to="/ai">Open AI workspace</Link> }}
        columns={[
          { key: "name", label: "Name" },
          { key: "request", label: "Request", render: (r) => <span className="muted small">{String(r.request).slice(0, 140)}</span> },
          { key: "run", label: "", render: (r) => <button type="button" className="button button--ghost button--small" onClick={() => assistant.show(String(r.request))}>Ask again</button> },
        ]}
      />
    </div>
  );
}

function AgentHistory() {
  const client = useWs();
  return (
    <>
      <Research part="history" />
      <h2 className="section-title">AI workspace runs</h2>
      <ResourceList
        load={(q, s) => client.list("/agent/runs", q, s)}
        link={() => "/ai"}
        empty={{ title: "No AI workspace runs yet", description: "Questions asked with Ask SANA GTM AI are planned and kept here.", icon: "sparkles" }}
        columns={[
          { key: "request", label: "Request", render: (r) => String(r.request).slice(0, 110) },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "mode", label: "Agent" },
          { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
        ]}
      />
    </>
  );
}

export function ResearchSection() {
  return (
    <SectionPage
      title="Research Agent"
      subtitle="Ask in plain language. The agent plans, shows the plan and its credit cost, and only runs after you approve."
      actions={<Link className="button button--ghost" to="/ai"><Icon name="bot" size={16} /> Open AI workspace</Link>}
      tabs={[
        { key: "research", label: "Research", render: () => <Research part="form" /> },
        { key: "saved", label: "Saved Research", render: () => <SavedResearch /> },
        { key: "history", label: "Research History", render: () => <AgentHistory /> },
      ]}
    />
  );
}

// --- Analytics & Admin -----------------------------------------------------------------------

export function AnalyticsSection() {
  return (
    <SectionPage
      title="Analytics"
      subtitle="Discovery, contacts, jobs, signals, pipeline, campaigns, sources, credits and research."
      tabs={[
        { key: "overview", label: "Overview", render: () => <Dashboard /> },
        { key: "reports", label: "All reports", render: () => <Analytics /> },
      ]}
    />
  );
}

function Crawls() {
  return (
    <EmptyState
      icon="scraper"
      title="Careers crawls"
      description="The CareerCloud careers crawler runs as its own jobs, with progress, logs and results for each crawl."
      action={
        <span className="actions">
          <Link className="button button--primary" to="/new">New crawl</Link>
          <Link className="button button--ghost" to="/jobs">All crawls</Link>
        </span>
      }
    />
  );
}

export function SettingsSection() {
  return (
    <SectionPage
      title="Settings"
      subtitle="Workspace, AI, credits and background work."
      tabs={[
        { key: "general", label: "General", render: () => <Settings /> },
        { key: "credits", label: "Credits", render: () => <Credits /> },
        { key: "background", label: "Background jobs", render: () => <BackgroundTasks /> },
        { key: "memory", label: "AI memory", render: () => <MemoryPage /> },
        { key: "crawls", label: "Crawls", render: () => <Crawls /> },
      ]}
    />
  );
}
