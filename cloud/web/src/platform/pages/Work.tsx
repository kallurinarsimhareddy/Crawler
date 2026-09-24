// Dashboard, analytics, the opportunity pipeline and list detail.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { CreateForm } from "../ResourcePage";
import { OPPORTUNITY_COLUMNS } from "../resources";
import { DataTable, Json, KeyValues, PageHeader, Pill, ResourceList, Score, Stat, Tags, fmt, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";

type Dash = Record<string, unknown>;

function num(value: unknown, ...path: string[]): number {
  let current: unknown = value;
  for (const key of path) current = current && typeof current === "object" ? (current as Record<string, unknown>)[key] : undefined;
  return typeof current === "number" ? current : 0;
}

function Bars({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).filter(([, v]) => typeof v === "number");
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (entries.length === 0) return <p className="muted small">No data yet.</p>;
  return (
    <ul className="bars">
      {entries.map(([label, value]) => (
        <li key={label} className="bars__row">
          <span className="bars__label">{label.replace(/_/g, " ")}</span>
          <span className="bars__track"><span className="bars__fill" style={{ width: `${(value / max) * 100}%` }} /></span>
          <span className="bars__value tabular">{value.toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}

function section(d: Dash | null, key: string): Record<string, number> {
  const value = d?.[key];
  if (!value || typeof value !== "object") return {};
  const obj = value as Record<string, unknown>;
  const inner = (obj.by_type ?? obj.by_status ?? obj.by_stage ?? obj.by_provider ?? obj.by_event ?? obj) as Record<string, unknown>;
  return Object.fromEntries(Object.entries(inner).filter(([, v]) => typeof v === "number")) as Record<string, number>;
}

export function Dashboard() {
  const client = useWs();
  const { current } = useWorkspace();
  const { data, error, loading, refresh } = useLoad((signal) => client.get<Dash>("/analytics/dashboard", undefined, signal), client.base + "dashboard", 30000);
  return (
    <div className="page">
      <PageHeader title={current?.name ?? "Dashboard"} subtitle="Company intelligence, hiring signals and pipeline at a glance." />
      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !data ? (
        <Loading />
      ) : (
        <>
          <div className="stats">
            <Stat label="Companies" value={num(data, "companies", "total").toLocaleString()} hint={`${num(data, "companies", "discovered")} discovered`} />
            <Stat label="Contacts" value={num(data, "contacts", "total").toLocaleString()} hint={`${num(data, "contacts", "verified_emails")} verified emails`} />
            <Stat label="Open jobs" value={num(data, "jobs", "open").toLocaleString()} hint={`${num(data, "jobs", "relevant")} relevant`} />
            <Stat label="Hiring signals" value={num(data, "hiring_signals", "total").toLocaleString()} />
            <Stat label="Opportunities" value={num(data, "opportunities", "open").toLocaleString()} hint={num(data, "opportunities", "pipeline_value") ? `$${num(data, "opportunities", "pipeline_value").toLocaleString()} pipeline` : undefined} />
          </div>
          <div className="grid-2">
            <div className="card pad"><h3>Hiring signals by type</h3><Bars data={section(data, "hiring_signals")} /></div>
            <div className="card pad"><h3>Pipeline by stage</h3><Bars data={section(data, "opportunities")} /></div>
            <div className="card pad"><h3>Email validation</h3><Bars data={section(data, "validation")} /></div>
            <div className="card pad"><h3>Credits used by provider</h3><Bars data={section(data, "credits")} /></div>
          </div>
          <div className="quick">
            <Link className="button button--primary" to="/research">Ask the research agent</Link>
            <Link className="button button--ghost" to="/imports">Import company files</Link>
            <Link className="button button--ghost" to="/discovery">Discover companies</Link>
            <Link className="button button--ghost" to="/scraper">AI scraper</Link>
          </div>
        </>
      )}
    </div>
  );
}

export function Analytics() {
  const client = useWs();
  const { data, error, loading, refresh } = useLoad((signal) => client.get<Dash>("/analytics/dashboard", undefined, signal), client.base + "analytics");
  const keys = data ? Object.keys(data).filter((k) => data[k] && typeof data[k] === "object") : [];
  return (
    <div className="page">
      <PageHeader title="Analytics" subtitle="Discovery, matching, contacts, validation, jobs, signals, opportunities, campaigns, sources, credits and research." />
      {error && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !data ? <Loading /> : (
        <div className="grid-2">
          {keys.map((key) => (
            <div key={key} className="card pad">
              <h3>{key.replace(/_/g, " ")}</h3>
              {Object.keys(section(data, key)).length ? <Bars data={section(data, key)} /> : <Json value={data?.[key]} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface Stage extends Row { name: string; position: number; pipeline_id: string; is_won?: boolean; is_lost?: boolean }

export function Opportunities() {
  const client = useWs();
  const [view, setView] = useState<"board" | "list">("board");
  const [creating, setCreating] = useState(false);
  const [reload, setReload] = useState(0);
  const stages = useLoad((signal) => client.list<Stage>("/pipeline-stages", { limit: 200, order: "position" }, signal), client.base + "stages");
  const opps = useLoad((signal) => client.list("/opportunities", { limit: 500, status: "open" }, signal), client.base + "opps" + reload);
  const action = useAction();
  const move = (id: string, stageId: string) =>
    action.run(async () => {
      await client.post(`/opportunities/${id}/stage`, { stage_id: stageId });
      opps.refresh();
    });
  return (
    <div className="page">
      <PageHeader
        title="Opportunities"
        subtitle="Scored, evidence-backed opportunities from hiring signals and manual work."
        actions={
          <>
            <button className="button button--ghost" onClick={() => setView(view === "board" ? "list" : "board")}>{view === "board" ? "List view" : "Board view"}</button>
            <button className="button button--primary" onClick={() => setCreating((c) => !c)}>{creating ? "Close" : "New opportunity"}</button>
          </>
        }
      />
      {creating && (
        <CreateForm
          path="/opportunities"
          fields={[
            { key: "company_id", label: "Company id", required: true },
            { key: "title", label: "Title", required: true },
            { key: "reason", label: "Why", type: "textarea" },
            { key: "amount", label: "Amount", type: "number" },
          ]}
          onCreated={() => { setCreating(false); setReload((n) => n + 1); }}
        />
      )}
      {(stages.error || opps.error || action.error) && <ErrorBanner error={(stages.error || opps.error || action.error) as Error} />}
      {view === "list" ? (
        <ResourceList reloadKey={String(reload)} load={(q, s) => client.list("/opportunities", q, s)} columns={OPPORTUNITY_COLUMNS as never} filters={[{ key: "status", label: "Status", options: ["open", "won", "lost"] }]} />
      ) : !stages.data || !opps.data ? (
        <Loading />
      ) : (
        <div className="board">
          {stages.data.items.map((stage) => {
            const items = opps.data!.items.filter((o) => o.stage_id === stage.id);
            return (
              <section key={stage.id} className="board__col">
                <header className="board__head"><span>{stage.name}</span><span className="tab__count">{items.length}</span></header>
                {items.map((o) => (
                  <article key={o.id} className="board__card">
                    <div className="board__title">{String(o.title)}</div>
                    <Score value={o.score} />
                    <Tags values={o.signal_types} />
                    <select className="input input--small" value={String(o.stage_id)} aria-label="Move to stage" onChange={(e) => move(o.id, e.target.value)}>
                      {stages.data!.items.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </select>
                  </article>
                ))}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function ListDetail() {
  const { listId = "" } = useParams();
  const client = useWs();
  const list = useLoad((signal) => client.get<Row>(`/lists/${listId}`, undefined, signal), client.base + listId);
  const members = useLoad((signal) => client.list(`/lists/${listId}/members`, { limit: 200 }, signal), client.base + listId + "m");
  const action = useAction();
  if (list.error) return <div className="page"><ErrorBanner error={list.error} /></div>;
  if (!list.data) return <div className="page"><Loading /></div>;
  return (
    <div className="page">
      <Link to="/lists" className="back">← Lists</Link>
      <PageHeader
        title={String(list.data.name)}
        subtitle={`${fmt(list.data.member_count)} ${String(list.data.entity_type).replace(/_/g, " ")}`}
        actions={<button className="button button--ghost" disabled={action.busy} onClick={() => action.run(() => client.post("/exports", { entity_type: "list", filters: { list_id: listId }, format: "xlsx" }))}>Export XLSX</button>}
      />
      {action.error && <ErrorBanner error={action.error} />}
      <KeyValues items={[["Description", fmt(list.data.description)], ["Source", fmt(list.data.source)]]} />
      {members.data && (
        <DataTable
          rows={members.data.items}
          columns={[
            { key: "entity_id", label: "Record", render: (r) => <Link className="link" to={`/${String(list.data!.entity_type) === "contacts" ? "contacts" : "companies"}/${r.entity_id}`}>{String(r.entity_id)}</Link> },
            { key: "added_reason", label: "Why added" },
            { key: "created_at", label: "Added", render: (r) => fmt(r.created_at) },
          ]}
          empty="This list is empty."
        />
      )}
    </div>
  );
}

export function BackgroundTasks() {
  const client = useWs();
  return (
    <div className="page">
      <PageHeader title="Background tasks" subtitle="Crawls, discovery, scraping, enrichment, validation, research, imports and exports — with pause, resume and cancel." />
      <TaskTable client={client} />
    </div>
  );
}

function TaskTable({ client }: { client: ReturnType<typeof useWs> }) {
  const [reload, setReload] = useState(0);
  const action = useAction();
  const act = (id: string, verb: string) => action.run(async () => { await client.post(`/tasks/${id}/${verb}`); setReload((n) => n + 1); });
  return (
    <>
      {action.error && <ErrorBanner error={action.error} />}
      <ResourceList
        reloadKey={String(reload)}
        load={(q, s) => client.list("/tasks", q, s)}
        columns={[
          { key: "kind", label: "Kind", render: (r) => <Pill value={r.kind} /> },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "progress", label: "Progress", render: (r) => String((r.progress as Record<string, unknown>)?.message ?? "") },
          { key: "attempts", label: "Attempts", render: (r) => `${r.attempts}/${r.max_attempts}` },
          { key: "created_at", label: "Created", render: (r) => fmt(r.created_at) },
          {
            key: "controls",
            label: "",
            render: (r) => (
              <span className="actions">
                {["queued", "retrying", "running"].includes(String(r.status)) && <button className="button button--small button--ghost" onClick={() => act(r.id, "pause")}>Pause</button>}
                {r.status === "paused" && <button className="button button--small button--ghost" onClick={() => act(r.id, "resume")}>Resume</button>}
                {!["completed", "failed", "cancelled"].includes(String(r.status)) && <button className="button button--small button--danger" onClick={() => act(r.id, "cancel")}>Cancel</button>}
              </span>
            ),
          },
        ]}
        filters={[{ key: "status", label: "Status", options: ["queued", "running", "paused", "retrying", "completed", "failed", "cancelled"] }, { key: "kind", label: "Kind", options: ["crawl", "discovery", "scraper", "enrichment", "validation", "research", "analytics", "workflow", "import_merge", "monitor", "signals", "export", "source_search"] }]}
      />
    </>
  );
}


