// Home: the GTM command center — quick actions, the numbers that matter, and
// what changed recently (activity, scrapes, AI insights).

import { Link } from "react-router-dom";
import { useAuth } from "../../auth/AuthProvider";
import { EmptyState } from "../../components/Feedback";
import { useAssistant } from "../../shell/Assistant";
import { Icon, type IconName } from "../../shell/Icon";
import type { PageOf, Row } from "../api";
import { Sparkline, num } from "../charts";
import { Pill, fmtDate, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";

type Dash = Record<string, unknown>;

const QUICK: { label: string; hint: string; icon: IconName; to: string }[] = [
  { label: "Find Companies", hint: "Search and discover accounts", icon: "search", to: "/prospecting" },
  { label: "Research Company", hint: "Plan research with the agent", icon: "bot", to: "/research" },
  { label: "Scrape Websites", hint: "Extract data from any URLs", icon: "scraper", to: "/scraper" },
  { label: "Build Prospect List", hint: "Save accounts into a list", icon: "list", to: "/lists?create=1" },
  { label: "Create Campaign", hint: "Target accounts with a message", icon: "megaphone", to: "/campaigns?create=1" },
  { label: "Create Workflow", hint: "Automate repeatable work", icon: "workflow", to: "/workflows" },
];

function endOfToday(): string {
  const d = new Date();
  d.setHours(23, 59, 59, 999);
  return d.toISOString();
}

function Metric({ label, value, to, hint, points }: { label: string; value: number | null; to: string; hint?: string; points?: { date: string; count: number }[] }) {
  return (
    <Link to={to} className="metric">
      <span className="metric__label">{label}</span>
      <span className="metric__row">
        <span className="metric__value tabular">{value === null ? "—" : value.toLocaleString()}</span>
        {points && points.some((p) => p.count > 0) && <Sparkline points={points} label={label} />}
      </span>
      {hint && <span className="metric__hint">{hint}</span>}
    </Link>
  );
}

function Panel({ title, to, linkLabel = "View all", children }: { title: string; to?: string; linkLabel?: string; children: React.ReactNode }) {
  return (
    <section className="card panel">
      <header className="panel__head">
        <h2>{title}</h2>
        {to && (
          <Link to={to} className="link small">
            {linkLabel}
          </Link>
        )}
      </header>
      {children}
    </section>
  );
}

export function Home() {
  const client = useWs();
  const { current } = useWorkspace();
  const { session } = useAuth();
  const assistant = useAssistant();
  const action = useAction();
  const dash = useLoad((s) => client.get<Dash>("/analytics/dashboard", undefined, s).catch(() => null), client.base + "home-dash", 60000);
  const due = useLoad(
    (s) =>
      client
        .list("/crm-tasks", { status: "open", due_at__lte: endOfToday(), limit: 1 }, s)
        .catch(() => client.list("/crm-tasks", { status: "open", limit: 1 }, s))
        .catch(() => null),
    client.base + "home-due",
  );
  const activity = useLoad((s) => client.list("/activities", { limit: 6, order: "-occurred_at" }, s).catch(() => null), client.base + "home-act");
  const scrapes = useLoad((s) => client.list("/scraper/runs", { limit: 5 }, s).catch(() => null), client.base + "home-scr", 30000);
  const insights = useLoad((s) => client.list("/agent/insights", undefined, s).catch(() => null), client.base + "home-ins");

  const d = dash.data;
  const series = (d?.series ?? {}) as Record<string, { date: string; count: number }[]>;
  const campaigns = ((d?.campaigns as Row | undefined)?.campaigns ?? []) as Row[];
  const activeCampaigns = d ? campaigns.filter((c) => c.status === "active").length : null;
  const value = (n: number) => (d ? n : null);
  const name = session?.email?.split("@")[0];

  return (
    <div className="page page--home">
      <section className="hero">
        <div className="min-w-0">
          <p className="hero__eyebrow">{current?.name ?? "Workspace"}</p>
          <h1 className="hero__title">Welcome back{name ? `, ${name}` : ""}</h1>
          <p className="muted">Find, research and reach the companies that are hiring for what you sell.</p>
        </div>
        <button type="button" className="hero__ask" onClick={() => assistant.show()}>
          <Icon name="sparkles" />
          <span>Ask SANA GTM AI anything — “Show companies hiring SAP managers.”</span>
          <kbd className="kbd">Ctrl K</kbd>
        </button>
      </section>

      <section aria-label="Quick actions" className="quick-grid">
        {QUICK.map((q) => (
          <Link key={q.label} to={q.to} className="quick-card">
            <span className="quick-card__icon"><Icon name={q.icon} /></span>
            <span className="min-w-0">
              <span className="quick-card__label">{q.label}</span>
              <span className="quick-card__hint">{q.hint}</span>
            </span>
          </Link>
        ))}
      </section>

      <section aria-label="Metrics" className="metrics">
        <Metric label="Companies" value={value(num(d, "companies", "total"))} to="/companies" points={series.companies} hint={d ? `${num(d, "companies", "with_open_jobs").toLocaleString()} hiring now` : undefined} />
        <Metric label="Contacts" value={value(num(d, "contacts", "total"))} to="/contacts" hint={d ? `${num(d, "contacts", "verified_emails").toLocaleString()} verified emails` : undefined} />
        <Metric label="Open Jobs" value={value(num(d, "jobs", "open"))} to="/hiring?tab=jobs" points={series.job_postings} hint={d ? `${num(d, "jobs", "relevant").toLocaleString()} relevant` : undefined} />
        <Metric label="Hiring Signals" value={value(num(d, "signals", "active"))} to="/signals" points={series.hiring_signals} hint={d ? `${num(d, "signals", "total").toLocaleString()} detected in total` : undefined} />
        <Metric label="Active Campaigns" value={activeCampaigns} to="/campaigns" hint={d ? `${campaigns.length.toLocaleString()} campaigns` : undefined} />
        <Metric label="Tasks Due" value={due.data ? (due.data as PageOf).total : due.loading ? null : 0} to="/tasks" hint="Open, due by today" />
      </section>

      <div className="home-grid">
        <Panel title="Recent Activity" to="/activities">
          {(activity.data?.items ?? []).length === 0 ? (
            <EmptyState icon="activity" title="No activity yet" description="Calls, meetings, emails, imports and stage changes show up here as a timeline." action={<Link className="button button--ghost button--small" to="/activities?create=1">Log an activity</Link>} />
          ) : (
            <ul className="feed">
              {activity.data!.items.map((a) => (
                <li key={a.id} className="feed__item">
                  <span className="feed__dot" />
                  <span className="min-w-0">
                    <span className="feed__text">{String(a.summary ?? a.kind)}</span>
                    <span className="feed__meta">{String(a.kind ?? "")} · {fmtDate(a.occurred_at ?? a.created_at)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent Scrapes" to="/scraper">
          {(scrapes.data?.items ?? []).length === 0 ? (
            <EmptyState icon="scraper" title="No scrapes yet" description="Paste URLs, say what to collect, and get structured rows with evidence for every value." action={<Link className="button button--primary button--small" to="/scraper">Scrape websites</Link>} />
          ) : (
            <ul className="feed">
              {scrapes.data!.items.map((r) => {
                const stats = (r.stats ?? {}) as Row;
                return (
                  <li key={r.id} className="feed__item">
                    <Pill value={r.status} />
                    <Link to={`/scraper/${r.id}`} className="min-w-0 feed__link">
                      <span className="feed__text truncate">{String(r.instruction ?? r.id)}</span>
                      <span className="feed__meta">
                        {String(stats.records ?? 0)} records · {String(stats.url_count ?? "?")} URLs · {fmtDate(r.created_at)}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel title="AI Insights" to="/ai" linkLabel="Open AI workspace">
          {(insights.data?.items ?? []).length === 0 ? (
            <EmptyState
              icon="sparkles"
              title="No new insights"
              description="SANA GTM watches for hiring spikes, ERP implementation hiring, missing IT leaders and ATS changes."
              action={
                <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => { await client.post("/agent/insights/refresh"); insights.refresh(); })}>
                  {action.busy ? "Checking…" : "Check now"}
                </button>
              }
            />
          ) : (
            <ul className="feed">
              {insights.data!.items.slice(0, 5).map((i) => (
                <li key={i.id} className={`feed__item insight insight--${String(i.severity)}`}>
                  <span className="feed__dot" />
                  <span className="min-w-0">
                    <span className="feed__text">{String(i.title)}</span>
                    {i.detail ? <span className="feed__meta">{String(i.detail)}</span> : null}
                    {i.suggested_request ? (
                      <button type="button" className="link link-button small" onClick={() => assistant.show(String(i.suggested_request))}>
                        Investigate with AI →
                      </button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
