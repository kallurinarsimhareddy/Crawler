// The Jobs area: every job from monitored sources and historical imports, with
// URL-synced filters (deep links from notifications and SANA chat land here),
// advanced AND/OR conditions, the job page with its change history, and the
// company-name review queue.

import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { PageOf, Row } from "../api";
import {
  CHANGE_OPTIONS, JOB_TABLE_COLUMNS, REMOTE_OPTIONS, STATUS_OPTIONS, display, goneCheckText, historyDetails, jobClosureText,
  joinKeywords, relevanceLabel, tableCell, type JobTableColumn,
} from "../logic/jobFields";
import { RELEVANCE_CLASSES, parseRelevanceParam, relevanceTone, toggleRelevance } from "../logic/jobspy";
import {
  CONDITION_FIELDS, CONDITION_OPS, ORDER_OPTIONS, buildConditions, children, clearScope, conditionValueText, deepLink,
  emptyGroup, groupMode, hasScope, isGroup, parseConditionValue, parseConditionsParam, parseFilters, parsePage, scopeBanner,
  searchBody, toQueryString, validateConditions, type FilterKey, type Group, type JobFilters, type Node,
} from "../logic/jobFilters";
import { FilterBar, KeyValues, PageHeader, Tabs, fmt, fmtDate, useAction, useLoad, type FilterDef } from "../ui";
import { useWs } from "../workspace";
import { Badge, Cell, ChangeBadge, Chips, JobStatus, JobUrl, Relevance, type Job, type JobPage, type Monitor } from "./jobsShared";

const FILTERS: FilterDef[] = [
  { key: "source", label: "Source", placeholder: "source contains…" },
  { key: "company", label: "Company", placeholder: "company contains…" },
  { key: "title", label: "Job title", placeholder: "title contains…" },
  { key: "location", label: "Location", placeholder: "location contains…" },
  { key: "country", label: "Country" },
  { key: "experience", label: "Experience" },
  { key: "salary", label: "Salary" },
  { key: "remote", label: "Remote", options: [...REMOTE_OPTIONS] },
  { key: "keyword", label: "Keyword" },
  { key: "status", label: "Status", options: [...STATUS_OPTIONS] },
  { key: "relevance", label: "Relevance", placeholder: "HIGH,REVIEW,REJECT" },
  { key: "relevance_min", label: "Min relevance score", placeholder: "0–100" },
  { key: "category", label: "Category", placeholder: "matched category…" },
  { key: "source_board", label: "Board", placeholder: "Indeed, LinkedIn…" },
  { key: "search_term", label: "Search term", placeholder: "JobSpy keyword…" },
  { key: "change", label: "Change", options: [...CHANGE_OPTIONS] },
  { key: "since", label: "Change since", placeholder: "YYYY-MM-DD" },
  { key: "scraped_from", label: "Scraped from", placeholder: "YYYY-MM-DD" },
  { key: "scraped_to", label: "Scraped to", placeholder: "YYYY-MM-DD" },
  { key: "first_seen_from", label: "First seen from", placeholder: "YYYY-MM-DD" },
  { key: "first_seen_to", label: "First seen to", placeholder: "YYYY-MM-DD" },
  { key: "last_changed_from", label: "Last changed from", placeholder: "YYYY-MM-DD" },
  { key: "last_changed_to", label: "Last changed to", placeholder: "YYYY-MM-DD" },
  { key: "monitor", label: "Monitor id" },
  { key: "run", label: "Run id" },
  { key: "import", label: "Import id" },
];

/** Columns shown when the person has not chosen (all of them; the table scrolls sideways). */
const COLUMNS_KEY = "sana.jobs.columns";

function loadColumns(): JobTableColumn[] {
  try {
    const raw = window.localStorage.getItem(COLUMNS_KEY);
    if (!raw) return [...JOB_TABLE_COLUMNS];
    const picked = new Set(JSON.parse(raw) as string[]);
    const cols = JOB_TABLE_COLUMNS.filter((c) => picked.has(c));
    return cols.length ? cols : [...JOB_TABLE_COLUMNS];
  } catch {
    return [...JOB_TABLE_COLUMNS];
  }
}

function saveColumns(cols: JobTableColumn[]): void {
  try {
    window.localStorage.setItem(COLUMNS_KEY, JSON.stringify(cols));
  } catch {
    /* storage blocked: the choice lasts for this page view only */
  }
}

const ORDER_LABELS: Record<string, string> = {
  "-first_seen_at": "Newest first seen", first_seen_at: "Oldest first seen", "-last_changed_at": "Recently changed",
  "-last_seen_at": "Recently seen", "-scraped_date": "Newest scraped", scraped_date: "Oldest scraped", title: "Title A–Z",
  company_name: "Company A–Z", location: "Location A–Z", "-relevance_score": "Most relevant",
};

export function JobsPage() {
  const [params] = useSearchParams();
  const tab = params.get("tab") === "reviews" ? "reviews" : "jobs";
  const navigate = useNavigate();
  return (
    <div className="page">
      <PageHeader
        title="Jobs"
        subtitle="Every job from your monitored sources and historical imports. Blank fields were not shown by the source — nothing is guessed."
        actions={
          <>
            <Link className="button button--ghost" to="/monitors">Monitors</Link>
            <Link className="button button--ghost" to="/jobs/keywords">Relevance keywords</Link>
            <Link className="button button--primary" to="/jobs/import">Import historical jobs</Link>
          </>
        }
        tabs={<Tabs active={tab} onChange={(k) => navigate(k === "reviews" ? "/jobs?tab=reviews" : "/jobs")} tabs={[{ key: "jobs", label: "Jobs" }, { key: "reviews", label: "Company review" }]} />}
      />
      {tab === "reviews" ? <CompanyReviews /> : <JobsTable />}
    </div>
  );
}

function JobsTable() {
  const client = useWs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => parseFilters(params), [params]);
  const page = useMemo(() => parsePage(params), [params]);
  const applied = useMemo(() => parseConditionsParam(params.get("conditions")), [params]);
  const [q, setQ] = useState(filters.q ?? "");
  const [showAdvanced, setShowAdvanced] = useState(Boolean(applied));
  const [draft, setDraft] = useState<Group>(() => applied ?? emptyGroup());
  const [columns, setColumns] = useState<JobTableColumn[]>(loadColumns);
  const [showColumns, setShowColumns] = useState(false);
  const toggleColumn = (c: JobTableColumn) => {
    const on = columns.includes(c);
    const next = JOB_TABLE_COLUMNS.filter((x) => (x === c ? !on : columns.includes(x)));
    if (next.length === 0) return;
    setColumns(next);
    saveColumns(next);
  };

  const go = (next: JobFilters, offset = 0, conditions: Group | null = applied) => {
    const qs = new URLSearchParams(toQueryString(next, { offset, limit: page.limit }));
    const tree = conditions ? buildConditions(conditions) : null;
    if (tree) qs.set("conditions", JSON.stringify(tree));
    setParams(qs, { replace: false });
  };

  const key = `${client.base}|${params.toString()}`;
  const feed = useLoad<JobPage>(
    (signal) =>
      applied
        ? client.post<JobPage>("/job-feed/search", searchBody(filters, applied, page))
        : client.get<JobPage>("/job-feed", { ...filters, limit: page.limit, offset: page.offset }, signal),
    key,
  );
  const monitors = useLoad((signal) => client.list<Monitor>("/job-monitors", { limit: 100 }, signal).catch(() => null), client.base + "monitors");
  const monitorList = monitors.data?.items ?? [];
  const link = deepLink(filters);
  const monitorName = link.monitor ? display(monitorList.find((m) => m.id === link.monitor)?.name) || null : null;

  const values: Record<string, string> = {};
  for (const f of FILTERS) if (filters[f.key as FilterKey]) values[f.key] = filters[f.key as FilterKey]!;

  const chip = (label: string, active: boolean, next: JobFilters) => (
    <button type="button" className={`chip chip--button${active ? " jm-chip--on" : ""}`} aria-pressed={active} onClick={() => go(active ? clearScope(filters) : next)}>
      {label}
    </button>
  );
  const base = clearScope(filters);
  const problems = validateConditions(draft);
  const data = feed.data;

  return (
    <>
      {hasScope(filters) && (
        <p className="alert alert--info" role="status">
          <span>{scopeBanner(filters, data ? data.total : null, monitorName)}</span>
          <button type="button" className="link-button" onClick={() => go(clearScope(filters))}>Clear filter</button>
        </p>
      )}
      <div className="card">
        <FilterBar
          search={q}
          onSearch={setQ}
          filters={FILTERS}
          values={values}
          onChange={(next) => go({ q: filters.q, order: filters.order, company_id: filters.company_id, since_last_run: next.monitor ? filters.since_last_run : undefined, ...next } as JobFilters)}
          onSubmit={() => go({ ...filters, q: q.trim() || undefined })}
          extra={
            <>
              <label className="sr-only" htmlFor="jm-order">Sort</label>
              <select id="jm-order" className="input input--small" value={filters.order ?? "-first_seen_at"} onChange={(e) => go({ ...filters, order: e.target.value === "-first_seen_at" ? undefined : e.target.value })}>
                {ORDER_OPTIONS.map((o) => <option key={o} value={o}>{ORDER_LABELS[o] ?? o}</option>)}
              </select>
              <button type="button" className={`button button--ghost button--small${showAdvanced || applied ? " button--on" : ""}`} aria-expanded={showAdvanced} onClick={() => setShowAdvanced((s) => !s)}>
                Advanced conditions{applied ? " ✓" : ""}
              </button>
              <button type="button" className={`button button--ghost button--small${showColumns ? " button--on" : ""}`} aria-expanded={showColumns} onClick={() => setShowColumns((s) => !s)}>
                Columns ({columns.length}/{JOB_TABLE_COLUMNS.length})
              </button>
            </>
          }
        />
        {showColumns && (
          <fieldset className="jm-columns">
            <legend className="sr-only">Visible columns</legend>
            {JOB_TABLE_COLUMNS.map((c) => (
              <label key={c}>
                <input type="checkbox" checked={columns.includes(c)} onChange={() => toggleColumn(c)} /> {c}
              </label>
            ))}
            <button type="button" className="link-button small" onClick={() => { setColumns([...JOB_TABLE_COLUMNS]); saveColumns([...JOB_TABLE_COLUMNS]); }}>Show all</button>
          </fieldset>
        )}
        <div className="chips jm-chips" aria-label="Quick filters">
          {monitorList.length > 0 && (
            <label className="jm-inline">
              <span className="muted small">New since last run:</span>
              <select
                className="input input--small"
                value={link.sinceLastRun ? link.monitor ?? "" : ""}
                onChange={(e) => go(e.target.value ? { ...base, monitor: e.target.value, since_last_run: "1" } : base)}
              >
                <option value="">Choose a monitor…</option>
                {monitorList.map((m) => <option key={m.id} value={m.id}>{display(m.name) || m.id}</option>)}
              </select>
            </label>
          )}
          {monitorList.length > 0 && (
            <label className="jm-inline">
              <span className="muted small">Monitor:</span>
              <select
                className="input input--small"
                value={!link.sinceLastRun ? link.monitor ?? "" : ""}
                onChange={(e) => go(e.target.value ? { ...filters, monitor: e.target.value, since_last_run: undefined } : { ...filters, monitor: undefined, run: undefined, since_last_run: undefined })}
              >
                <option value="">All monitors</option>
                {monitorList.map((m) => <option key={m.id} value={m.id}>{display(m.name) || m.id}</option>)}
              </select>
            </label>
          )}
          {chip("New", link.change === "new" && !link.run, { ...base, monitor: filters.monitor, change: "new" })}
          {chip("Changed", link.change === "changed" && !link.run, { ...base, monitor: filters.monitor, change: "changed" })}
          {chip("Unchanged", link.change === "unchanged" && !link.run, { ...base, monitor: filters.monitor, change: "unchanged" })}
          {chip("Stale", link.change === "stale" && !link.run, { ...base, monitor: filters.monitor, change: "stale" })}
          {chip("Expired", link.change === "expired" && !link.run, { ...base, monitor: filters.monitor, change: "expired" })}
          {chip("Closed", link.change === "closed" && !link.run, { ...base, monitor: filters.monitor, change: "closed" })}
          {chip("Reopened", link.change === "reopened" && !link.run, { ...base, monitor: filters.monitor, change: "reopened" })}
          <span className="muted small">Relevance:</span>
          {RELEVANCE_CLASSES.map((cls) => {
            const on = parseRelevanceParam(filters.relevance).includes(cls);
            return (
              <button key={cls} type="button" className={`chip chip--button${on ? " jm-chip--on" : ""}`} aria-pressed={on} onClick={() => go({ ...filters, relevance: toggleRelevance(filters.relevance, cls) || undefined })}>
                {cls}
              </button>
            );
          })}
        </div>
        {showAdvanced && (
          <div className="jm-advanced">
            <p className="muted small">Combine conditions with AND (ALL) / OR (ANY). They apply on top of the filters above.</p>
            <JobConditionGroup group={draft} onChange={setDraft} />
            {problems.length > 0 && <p className="small jm-problem">{problems.slice(0, 3).join(" · ")}</p>}
            <div className="actions">
              <button type="button" className="button button--primary button--small" disabled={problems.length > 0 || !buildConditions(draft)} onClick={() => go(filters, 0, draft)}>Apply conditions</button>
              {applied && <button type="button" className="button button--ghost button--small" onClick={() => { setDraft(emptyGroup()); go(filters, 0, null); }}>Remove conditions</button>}
            </div>
          </div>
        )}
        {feed.error && <ErrorBanner error={feed.error} onRetry={feed.refresh} />}
        {feed.loading && !data ? (
          <Loading />
        ) : data ? (
          data.rows.length === 0 ? (
            <EmptyState
              icon="search"
              title={Object.keys(filters).length || applied ? "No matching jobs" : "No jobs yet"}
              description={Object.keys(filters).length || applied ? "Nothing matches these filters." : "Create a monitor for a job source, or import a historical job file."}
              action={
                <span className="actions">
                  {Object.keys(filters).length || applied ? <button type="button" className="button button--ghost button--small" onClick={() => { setQ(""); go({}, 0, null); }}>Clear filters</button> : null}
                  <Link className="button button--ghost button--small" to="/monitors">New monitor</Link>
                  <Link className="button button--ghost button--small" to="/jobs/import">Import historical jobs</Link>
                </span>
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table jm-table">
                <thead>
                  <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {data.rows.map((job) => (
                    <tr key={job.id} className="table__row jm-row" onClick={() => navigate(`/jobs/${job.id}`)}>
                      {columns.map((c) => <JobTableCell key={c} job={job} column={c} />)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}
        {data && (
          <div className="pager">
            <span className="muted small tabular">
              {data.total === 0 ? "0 results" : `${data.offset + 1}–${data.offset + data.rows.length} of ${data.total.toLocaleString()}`}
            </span>
            <div className="actions">
              <button className="button button--ghost button--small" disabled={page.offset === 0} onClick={() => go(filters, Math.max(0, page.offset - page.limit))}>Previous</button>
              <button className="button button--ghost button--small" disabled={data.offset + data.rows.length >= data.total} onClick={() => go(filters, page.offset + page.limit)}>Next</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

/** One Jobs-table cell; the column order comes from JOB_TABLE_COLUMNS. */
function JobTableCell({ job, column }: { job: Job; column: JobTableColumn }) {
  switch (column) {
    case "Job Title":
      return <td data-label={column}><Link className="link" to={`/jobs/${job.id}`} onClick={(e) => e.stopPropagation()}><Cell value={job.title} /></Link></td>;
    case "Keywords":
      return <td data-label={column} className="small"><Cell value={tableCell(job, column)} /></td>;
    case "Scraped Date":
    case "First Seen":
    case "Last Seen":
    case "Last Changed":
    case "Stale Date":
    case "Score":
      return <td data-label={column} className="tabular"><Cell value={tableCell(job, column)} /></td>;
    case "Closed Date": {
      const why = jobClosureText(job);
      return <td data-label={column} className="tabular" title={why || undefined}><Cell value={tableCell(job, column)} /></td>;
    }
    case "Reason": {
      const reason = tableCell(job, column);
      return <td data-label={column} className="small"><span className="jm-reason-cell" title={reason || undefined}><Cell value={reason} /></span></td>;
    }
    case "Status":
      return <td data-label={column}><JobStatus job={job} /></td>;
    case "New/Changed":
      return <td data-label={column}><ChangeBadge value={job.change_badge} /></td>;
    case "Relevance": {
      const cls = relevanceLabel(job.relevance_class);
      const tone = relevanceTone(cls);
      return <td data-label={column}>{cls && tone ? <Badge text={cls} tone={tone} /> : <span className="muted" aria-label="not scored">—</span>}</td>;
    }
    case "Job URL":
      return <td data-label={column} className="small jm-url"><JobUrl url={job.job_url} className="button button--ghost button--small">Open original</JobUrl></td>;
    default:
      return <td data-label={column}><Cell value={tableCell(job, column)} /></td>;
  }
}

/** A small AND/OR editor for job conditions (the same shape as the workflow builder's groups). */
function JobConditionGroup({ group, onChange, depth = 0 }: { group: Group; onChange: (g: Group) => void; depth?: number }) {
  const mode = groupMode(group);
  const items = children(group);
  const set = (next: Node[]) => onChange((mode === "any" ? { any: next } : { all: next }) as Group);
  return (
    <div className={`cr-cond cr-cond--depth${depth}`}>
      <div className="cr-cond__head">
        <label>
          <span className="sr-only">Combine conditions with</span>
          <select className="input input--small" value={mode} onChange={(e) => onChange((e.target.value === "any" ? { any: items } : { all: items }) as Group)}>
            <option value="all">ALL of these (AND)</option>
            <option value="any">ANY of these (OR)</option>
          </select>
        </label>
      </div>
      {items.length === 0 && <p className="muted small">No conditions yet.</p>}
      {items.map((child, i) =>
        isGroup(child) ? (
          <div key={i} className="cr-cond__row">
            <JobConditionGroup group={child} depth={depth + 1} onChange={(g) => set(items.map((c, k) => (k === i ? g : c)))} />
            <button type="button" className="button button--ghost button--small" onClick={() => set(items.filter((_, k) => k !== i))}>Remove group</button>
          </div>
        ) : (
          <div key={i} className="cr-cond__row cr-cond__leaf">
            <select className="input input--small" aria-label="Field" value={child.field} onChange={(e) => set(items.map((c, k) => (k === i ? { ...child, field: e.target.value } : c)))}>
              {CONDITION_FIELDS.map((f) => <option key={f} value={f}>{f.replace(/_/g, " ")}</option>)}
            </select>
            <select className="input input--small" aria-label="Operator" value={child.op} onChange={(e) => set(items.map((c, k) => (k === i ? { ...child, op: e.target.value, value: parseConditionValue(e.target.value, conditionValueText(child.value)) } : c)))}>
              {CONDITION_OPS.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
            </select>
            {child.op !== "empty" && child.op !== "not_empty" && (
              <input
                className="input input--small"
                aria-label="Value"
                placeholder={child.op === "in" ? "a, b, c" : child.field === "relevance" ? "HIGH / REVIEW / REJECT" : child.field === "relevance_score" ? "0–100" : ["scraped_date", "first_seen", "last_seen", "last_changed"].includes(child.field) ? "YYYY-MM-DD" : "value"}
                value={conditionValueText(child.value)}
                onChange={(e) => set(items.map((c, k) => (k === i ? { ...child, value: child.op === "in" ? e.target.value.split(",").map((s) => s.trimStart()) : e.target.value } : c)))}
              />
            )}
            <button type="button" className="button button--ghost button--small" aria-label="Remove condition" onClick={() => set(items.filter((_, k) => k !== i))}>×</button>
          </div>
        ),
      )}
      <div className="actions">
        <button type="button" className="button button--ghost button--small" onClick={() => set([...items, { field: "title", op: "contains", value: "" }])}>+ Condition</button>
        {depth < 2 && <button type="button" className="button button--ghost button--small" onClick={() => set([...items, { any: [] }])}>+ Group</button>}
      </div>
    </div>
  );
}

// --- job page ----------------------------------------------------------------------------------

interface JobDetailResponse {
  job: Job;
  history: Row[];
  monitor: { id: string; name: string; source_name?: string | null; last_run_at?: string | null } | null;
  company: { id: string; name: string } | null;
}

function ChangeLine({ change }: { change: Row }) {
  const kind = String(change.change ?? "");
  // A lifecycle entry's "changed field" is just status; its details say what happened.
  const fields = Array.isArray(change.changed_fields) && !["stale", "expired", "closed"].includes(kind)
    ? (change.changed_fields as string[])
    : [];
  const before = (change.before ?? {}) as Record<string, unknown>;
  const after = (change.after ?? {}) as Record<string, unknown>;
  const details = historyDetails(change);
  return (
    <li className="jm-history__item">
      <div className="jm-history__head">
        <ChangeBadge value={String(change.change ?? "").replace(/^./, (c) => c.toUpperCase())} />
        <span className="muted small">{fmt(change.detected_at)}</span>
        {change.run_id ? <span className="muted small mono">run {String(change.run_id)}</span> : kind === "stale" ? <span className="muted small">daily evaluation</span> : null}
      </div>
      {details.length > 0 && <ul className="jm-history__detail small">{details.map((d) => <li key={d}>{d}</li>)}</ul>}
      {fields.length > 0 && (
        <ul className="jm-history__fields small">
          {fields.map((f) => (
            <li key={f}>
              <strong>{f}</strong>: <span className="jm-before">{display(before[f]) || "—"}</span> → <span>{display(after[f]) || "—"}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function JobView() {
  const { jobId = "" } = useParams();
  const client = useWs();
  const { data, error, loading, refresh } = useLoad((signal) => client.get<JobDetailResponse>(`/job-feed/${encodeURIComponent(jobId)}`, undefined, signal), client.base + jobId);
  if (error) return <div className="page"><Link to="/jobs" className="back">← Jobs</Link><ErrorBanner error={error} onRetry={refresh} /></div>;
  if (loading || !data) return <div className="page"><Loading /></div>;
  const { job, history, monitor, company } = data;
  const companyCell = company ? <Link className="link" to={`/companies/${company.id}`}>{display(job.company_name) || company.name}</Link> : <Cell value={job.company_name} />;
  return (
    <div className="page">
      <Link to="/jobs" className="back">← Jobs</Link>
      <PageHeader
        title={display(job.title) || "Untitled job"}
        crumbTitle="Job"
        subtitle={[display(job.company_name), display(job.location)].filter(Boolean).join(" · ") || undefined}
        actions={
          <>
            <JobStatus job={job} />
            <ChangeBadge value={job.change_badge} />
            <JobUrl url={job.job_url} className="button button--primary" stop={false}>Open Original Job</JobUrl>
          </>
        }
      />
      <div className="detail-grid">
        <div className="card pad">
          <KeyValues
            items={[
              ["Job Title", <Cell value={job.title} />],
              ["Company", companyCell],
              ["Location", <Cell value={job.location} />],
              ["Experience", <Cell value={job.experience_level} />],
              ["Salary", <Cell value={job.salary_budget} />],
              ["Remote", <Cell value={job.remote} />],
              ["Keywords", <Cell value={joinKeywords(job)} />],
              ["Source", <Cell value={tableCell(job, "Source")} />],
              ["Posted", job.posted_at ? fmtDate(job.posted_at) : null],
              ["Scraped Date", <Cell value={job.scraped_date} />],
              ["Listing date", job.listing_date ? fmtDate(job.listing_date) : null],
              ["First Seen", job.first_seen_at ? fmt(job.first_seen_at) : null],
              ["Last Seen", job.last_seen_at ? fmt(job.last_seen_at) : null],
              ["Last Changed", job.last_changed_at ? fmt(job.last_changed_at) : null],
              ["Status", <JobStatus job={job} />],
              ["Stale since", job.stale_at ? fmt(job.stale_at) : null],
              ["Expired", job.expired_at ? <>{fmt(job.expired_at)} <span className="muted small">— older than the source's listing window; kept, not closed</span></> : null],
              ["Closed", job.closed_at ? <>{fmt(job.closed_at)}{jobClosureText(job) ? <span className="muted small"> — {jobClosureText(job)}</span> : null}</> : null],
              ["Reopened", job.reopened_at ? fmt(job.reopened_at) : null],
              ["Missed full sweeps", typeof job.missed_full_sweeps === "number" && job.missed_full_sweeps > 0 ? String(job.missed_full_sweeps) : null],
              ["Last URL check", goneCheckText(job) || null],
              ["Job URL", <JobUrl url={job.job_url} stop={false}>{display(job.job_url)}</JobUrl>],
              ["Monitor", monitor ? <Link className="link" to={`/monitors/${monitor.id}`}>{monitor.name}</Link> : null],
            ]}
          />
        </div>
        <RelevancePanel job={job} />
        <div className="card pad">
          <h3>Change history</h3>
          {history.length === 0 ? (
            <p className="muted small">No changes recorded{job.first_seen_at ? ` since it was first seen on ${fmtDate(job.first_seen_at)}` : ""}.</p>
          ) : (
            <ul className="jm-history">{history.map((h) => <ChangeLine key={h.id} change={h} />)}</ul>
          )}
        </div>
      </div>
    </div>
  );
}

/** Why the job scored what it did: the score, the reason (verbatim), what matched, and the description. */
function RelevancePanel({ job }: { job: Job }) {
  const description = typeof job.description === "string" ? job.description.trim() : "";
  const [open, setOpen] = useState(description.length <= 600);
  return (
    <div className="card pad">
      <h3>Relevance</h3>
      <KeyValues
        items={[
          ["Score", <Relevance score={job.relevance_score} cls={job.relevance_class} />],
          ["Reason", display(job.relevance_reason) ? <span className="jm-reason">{String(job.relevance_reason)}</span> : null],
          ["Matched keywords", <Chips values={job.matched_keywords} />],
          ["Matched categories", <Chips values={job.matched_categories} />],
          ["Search term", <Cell value={job.search_term} />],
          ["Board", <Cell value={job.source_board} />],
          ["Posted", job.posted_at ? fmtDate(job.posted_at) : null],
        ]}
      />
      {description && (
        <>
          <div className="title-row">
            <h4 className="section-title">Description</h4>
            <button type="button" className="link-button small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "Hide" : "Show"}</button>
          </div>
          {open ? <p className="jm-desc">{description}</p> : <p className="muted small">{description.slice(0, 200)}{description.length > 200 ? "…" : ""}</p>}
        </>
      )}
    </div>
  );
}

// --- company review ----------------------------------------------------------------------------

function CompanyReviews() {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const [targets, setTargets] = useState<Record<string, string>>({});
  const list = useLoad((signal) => client.list<Row>("/job-company-reviews", { status_filter: "pending", limit: 100 }, signal), client.base + "reviews" + reload);
  const action = useAction();
  const resolve = (id: string, body: Record<string, unknown>) =>
    action.run(async () => {
      await client.post(`/job-company-reviews/${id}/resolve`, body);
      setReload((n) => n + 1);
    });
  const items = (list.data as PageOf | null)?.items ?? [];
  return (
    <div className="card">
      <p className="muted small pad">Company names from jobs that did not match exactly one CRM company. Link each to a company (by its id) or ignore it — companies are never created from jobs automatically.</p>
      {(list.error || action.error) && <ErrorBanner error={(list.error ?? action.error)!} onRetry={list.refresh} />}
      {list.loading && !list.data ? <Loading /> : items.length === 0 ? (
        <EmptyState icon="check" title="Nothing to review" description="Every job company name matched a CRM company or was already resolved." />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Company name</th><th>Jobs</th><th>Candidates</th><th>Link to company id</th><th /></tr></thead>
            <tbody>
              {items.map((r) => {
                const candidates = Array.isArray(r.candidate_ids) ? (r.candidate_ids as string[]) : [];
                return (
                  <tr key={r.id} className="table__row">
                    <td data-label="Company name"><strong>{display(r.company_name)}</strong>{r.reason ? <div className="muted small">{String(r.reason)}</div> : null}</td>
                    <td data-label="Jobs" className="tabular">{fmt(r.job_count)}</td>
                    <td data-label="Candidates" className="small">
                      {candidates.length === 0 ? <span className="muted">—</span> : candidates.map((c) => (
                        <button key={c} type="button" className="link-button mono" onClick={() => setTargets((t) => ({ ...t, [r.id]: c }))}>{c}</button>
                      ))}
                    </td>
                    <td data-label="Link to company id">
                      <input className="input input--small" placeholder="co_…" value={targets[r.id] ?? ""} onChange={(e) => setTargets((t) => ({ ...t, [r.id]: e.target.value }))} />
                    </td>
                    <td className="actions">
                      <button type="button" className="button button--primary button--small" disabled={action.busy || !(targets[r.id] ?? "").trim()} onClick={() => void resolve(r.id, { action: "link", company_id: targets[r.id].trim() })}>Link</button>
                      <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void resolve(r.id, { action: "ignore" })}>Ignore</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
