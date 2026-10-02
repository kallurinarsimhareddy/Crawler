// Job source monitors: a careers page or job board checked on a schedule. Each run
// records new, changed and closed jobs; every count links to the filtered Jobs table.

import { useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import { newIdempotencyKey, type PageOf } from "../api";
import { JOB_FIELDS, display, safeJobUrl } from "../logic/jobFields";
import { jobsLink } from "../logic/jobFilters";
import {
  BOARD_DISABLED_NOTE, FRESHNESS_OPTIONS, HOURS_MAX, HOURS_MIN, JOBSPY_DEFAULTS, MAX_SEARCH_TERMS, RESULTS_MAX, RESULTS_MIN,
  buildJobSpyBody, countSearchTerms, disabledSelected, isJobSpyMonitor, jobSpySummary, parseSearchTerms, validateJobSpyForm,
  type JobSpyBoard,
} from "../logic/jobspy";
import { DataTable, KeyValues, PageHeader, Pill, Stat, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import { ACTIVE_RUN, Cell, Chips, JobUrl, patchFlat, type JobSources, type LifecycleResult, type Monitor, type MonitorRun } from "./jobsShared";

const SCHEDULES = ["daily", "weekly", "manual"];
const PAGE = 50;

interface Plan {
  name: string;
  source_url: string;
  schedule: string;
  strategy?: string;
  profile?: string;
  source_name?: string;
  newest_first?: boolean;
  filters?: Record<string, unknown> | null;
  fields?: string[];
  steps?: string[];
  preview?: {
    outcome?: string;
    reason?: string;
    jobs_on_page?: number;
    has_next_page?: boolean;
    sample?: (Record<string, unknown> & { problems?: unknown[] })[];
    filled?: Record<string, number>;
  } | null;
}

function lastResult(m: Monitor): Record<string, unknown> {
  return (m.last_result && typeof m.last_result === "object" ? m.last_result : {}) as Record<string, unknown>;
}

function count(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

/** The 14-column sample table shared by the monitor preview and the import validation. */
export function SampleTable({ rows, fields = [...JOB_FIELDS] }: { rows: Record<string, unknown>[]; fields?: string[] }) {
  if (rows.length === 0) return <p className="muted small">No sample rows.</p>;
  return (
    <div className="table-wrap jm-sample">
      <table className="table">
        <thead>
          <tr>
            <th>#</th>
            {fields.map((f) => <th key={f}>{f}</th>)}
            <th>Problems</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const problems = Array.isArray(row.problems) ? (row.problems as unknown[]).map(display).filter(Boolean) : [];
            return (
              <tr key={i} className="table__row">
                <td className="muted tabular">{i + 1}</td>
                {fields.map((f) => (
                  <td key={f} data-label={f} className={f === "Job URL" ? "small jm-url" : undefined}>
                    {f === "Job URL" ? <JobUrl url={row[f]} stop={false} /> : <Cell value={row[f]} />}
                  </td>
                ))}
                <td className="small">{problems.length ? <span className="jm-problem">{problems.join("; ")}</span> : <span className="muted">—</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** "Filled" counts per field: how many sample/valid rows carried a value. */
export function FilledCounts({ filled, of }: { filled: Record<string, number> | null | undefined; of?: number }) {
  if (!filled || Object.keys(filled).length === 0) return null;
  return (
    <div className="chips jm-filled" aria-label="Filled values per field">
      {JOB_FIELDS.filter((f) => f in filled).map((f) => (
        <span key={f} className={`chip${filled[f] ? "" : " chip--warn"}`}>
          {f}: {Number(filled[f] ?? 0).toLocaleString()}{of !== undefined ? ` / ${of.toLocaleString()}` : ""}
        </span>
      ))}
    </div>
  );
}

type SourceKind = "url" | "jobspy";

function SourcePicker({ value, onChange }: { value: SourceKind; onChange: (v: SourceKind) => void }) {
  return (
    <fieldset className="jm-source-pick">
      <legend className="field__label">Source</legend>
      <label><input type="radio" name="jm-source" value="url" checked={value === "url"} onChange={() => onChange("url")} /> Website / site profile (URL)</label>
      <label><input type="radio" name="jm-source" value="jobspy" checked={value === "jobspy"} onChange={() => onChange("jobspy")} /> JobSpy</label>
    </fieldset>
  );
}

function NewMonitorForm({ initialUrl, onCreated, onClose }: { initialUrl: string; onCreated: (m: Monitor) => void; onClose: () => void }) {
  const [source, setSource] = useState<SourceKind>("url");
  const picker = <SourcePicker value={source} onChange={setSource} />;
  return source === "jobspy"
    ? <JobSpyMonitorForm picker={picker} onCreated={onCreated} onClose={onClose} />
    : <UrlMonitorForm picker={picker} initialUrl={initialUrl} onCreated={onCreated} onClose={onClose} />;
}

/** Keywords as chips: type and press Enter or comma, or paste a comma / newline separated list. */
function TermsInput({ terms, onChange }: { terms: string[]; onChange: (terms: string[]) => void }) {
  const [text, setText] = useState("");
  const [dropped, setDropped] = useState(0);
  const add = (raw: string) => {
    const all = [...terms, raw];
    onChange(parseSearchTerms(all));
    setDropped(Math.max(0, countSearchTerms(all) - MAX_SEARCH_TERMS));
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.key === "Enter" && !e.shiftKey) || e.key === ",") {
      e.preventDefault();
      if (text.trim()) add(text);
    } else if (e.key === "Backspace" && !text && terms.length) {
      onChange(terms.slice(0, -1));
    }
  };
  return (
    <div className="jm-terms">
      {terms.length > 0 && (
        <span className="chips" aria-label="Keywords">
          {terms.map((t) => (
            <span key={t} className="chip">
              {t}
              <button type="button" className="jm-chip-x" aria-label={`Remove ${t}`} onClick={() => { onChange(terms.filter((x) => x !== t)); setDropped(0); }}>×</button>
            </span>
          ))}
        </span>
      )}
      <textarea
        className="input textarea"
        rows={2}
        aria-label="Add keywords"
        placeholder="SAP, ERP, Oracle EBS — comma or newline separated, Enter to add"
        value={text}
        disabled={terms.length >= MAX_SEARCH_TERMS}
        onChange={(e) => {
          const value = e.target.value;
          if (/[,\n]/.test(value)) add(value);
          else setText(value);
        }}
        onKeyDown={onKey}
        onBlur={() => { if (text.trim()) add(text); }}
      />
      <span className="field__hint">
        {terms.length} / {MAX_SEARCH_TERMS} keywords. Each keyword is searched on every chosen board.
        {dropped > 0 && <span className="jm-problem"> {dropped} more were not added (limit {MAX_SEARCH_TERMS}).</span>}
      </span>
    </div>
  );
}

function JobSpyMonitorForm({ picker, onCreated, onClose }: { picker: ReactNode; onCreated: (m: Monitor) => void; onClose: () => void }) {
  const client = useWs();
  const sources = useLoad((signal) => client.get<JobSources>("/job-sources", undefined, signal), client.base + "|job-sources");
  const boards: JobSpyBoard[] = sources.data?.jobspy?.boards ?? [];
  const defaults = { ...JOBSPY_DEFAULTS, ...(sources.data?.jobspy?.defaults ?? {}) };
  const [picked, setPicked] = useState<string[] | null>(null);
  const selected = picked ?? defaults.boards;
  const [terms, setTerms] = useState<string[]>([]);
  const [location, setLocation] = useState<string | null>(null);
  const [freshness, setFreshness] = useState<string | null>(null);
  const [customHours, setCustomHours] = useState("");
  const [results, setResults] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("daily");
  const [runNow, setRunNow] = useState(true);
  const [touched, setTouched] = useState(false);
  const action = useAction();

  const presets = FRESHNESS_OPTIONS.map((o) => String(o.hours));
  const freshValue = freshness ?? String(defaults.hours_old);
  const freshSelect = presets.includes(freshValue) ? freshValue : "custom";
  // A non-preset default (e.g. 36) shows as "Custom" with that number until edited.
  const customValue = customHours || (freshness === null ? freshValue : "");
  const hoursOld = freshSelect === "custom" ? customValue : freshValue;
  const form = {
    name, schedule, runNow, boards: selected, terms,
    location: location ?? defaults.location,
    hoursOld,
    resultsWanted: results ?? String(defaults.results_wanted),
  };
  const problems = validateJobSpyForm(form);
  const warn = disabledSelected(boards, selected);
  const toggle = (id: string) => setPicked(selected.includes(id) ? selected.filter((b) => b !== id) : [...selected, id]);

  const save = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (problems.length) return;
    void action.run(async () => {
      const created = await client.post<Monitor>("/job-monitors", buildJobSpyBody(form, defaults.country), newIdempotencyKey());
      onCreated(created);
    });
  };

  return (
    <form className="card pad form jm-new" onSubmit={save} noValidate>
      <div className="title-row"><h3>New monitor</h3><button type="button" className="button button--ghost button--small" onClick={onClose}>Close</button></div>
      {picker}
      {sources.error && <ErrorBanner error={sources.error} onRetry={sources.refresh} />}
      {sources.loading && !sources.data ? <Loading /> : (
        <>
          <fieldset className="jm-boards">
            <legend className="field__label">Board(s) <span aria-hidden="true">*</span></legend>
            {boards.length === 0 && <p className="muted small">No JobSpy boards are available on this deployment.</p>}
            {boards.map((b) => (
              <label key={b.id}>
                <input type="checkbox" checked={selected.includes(b.id)} onChange={() => toggle(b.id)} />
                <span>{b.label}</span>
                {!b.enabled && <span className="jm-board-note">— {b.note || BOARD_DISABLED_NOTE}</span>}
              </label>
            ))}
          </fieldset>
          {warn.length > 0 && (
            <p className="alert alert--warning small" role="status">
              {warn.map((b) => b.label).join(", ")} {warn.length === 1 ? "is" : "are"} {BOARD_DISABLED_NOTE}. Runs will be reported as partial until an admin enables {warn.length === 1 ? "the board" : "these boards"}.
            </p>
          )}
          <div className="form-grid">
            <div className="field field--wide">
              <span className="field__label">Keywords <span aria-hidden="true">*</span></span>
              <TermsInput terms={terms} onChange={setTerms} />
            </div>
            <label className="field">
              <span className="field__label">Location</span>
              <input className="input" value={form.location} onChange={(e) => setLocation(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Freshness</span>
              <select className="input" value={freshSelect} onChange={(e) => setFreshness(e.target.value)}>
                {FRESHNESS_OPTIONS.map((o) => <option key={o.hours} value={String(o.hours)}>{o.label}</option>)}
                <option value="custom">Custom…</option>
              </select>
            </label>
            {freshSelect === "custom" && (
              <label className="field">
                <span className="field__label">Posted within (hours, {HOURS_MIN}–{HOURS_MAX})</span>
                <input className="input" type="number" min={HOURS_MIN} max={HOURS_MAX} step={1} placeholder={`${HOURS_MIN}–${HOURS_MAX}`} value={customValue} onChange={(e) => setCustomHours(e.target.value)} />
              </label>
            )}
            <label className="field">
              <span className="field__label">Results limit per search ({RESULTS_MIN}–{RESULTS_MAX})</span>
              <input className="input" type="number" min={RESULTS_MIN} max={RESULTS_MAX} step={1} value={form.resultsWanted} onChange={(e) => setResults(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Schedule</span>
              <select className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)}>
                {SCHEDULES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="field">
              <span className="field__label">Name (optional)</span>
              <input className="input" value={name} placeholder="Named from the boards and keywords" onChange={(e) => setName(e.target.value)} />
            </label>
          </div>
          <p className="muted small">JobSpy monitors have no preview — save the monitor and run it to see jobs.</p>
          {touched && problems.length > 0 && <p className="small jm-problem" role="alert">{problems.join(" ")}</p>}
          {action.error && <ErrorBanner error={action.error} />}
          <div className="form__actions">
            <label className="checkbox"><input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} /> <span>Run now</span></label>
            <button type="submit" className="button button--primary" disabled={action.busy || (touched && problems.length > 0)}>{action.busy ? "Saving…" : "Save monitor"}</button>
          </div>
        </>
      )}
    </form>
  );
}

function UrlMonitorForm({ picker, initialUrl, onCreated, onClose }: { picker: ReactNode; initialUrl: string; onCreated: (m: Monitor) => void; onClose: () => void }) {
  const client = useWs();
  const [url, setUrl] = useState(initialUrl);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("daily");
  const [maxPages, setMaxPages] = useState("");
  const [runNow, setRunNow] = useState(true);
  const [plan, setPlan] = useState<Plan | null>(null);
  const action = useAction();
  const urlOk = Boolean(safeJobUrl(url));

  const preview = (event?: FormEvent) => {
    event?.preventDefault();
    if (!urlOk) return;
    void action.run(async () => setPlan(await client.post<Plan>("/job-monitors/plan", { source_url: url.trim(), name: name.trim() || undefined, schedule, preview: true })));
  };

  const save = () =>
    action.run(async () => {
      const body: Record<string, unknown> = { source_url: url.trim(), schedule, run_now: runNow };
      if (name.trim()) body.name = name.trim();
      if (maxPages.trim()) body.max_pages_incremental = Number(maxPages);
      if (plan?.filters && Object.keys(plan.filters).length) body.filters = plan.filters;
      const created = await client.post<Monitor>("/job-monitors", body, newIdempotencyKey());
      onCreated(created);
    });

  const p = plan?.preview;
  return (
    <form className="card pad form jm-new" onSubmit={preview}>
      <div className="title-row"><h3>New monitor</h3><button type="button" className="button button--ghost button--small" onClick={onClose}>Close</button></div>
      {picker}
      <div className="form-grid">
        <label className="field field--wide">
          <span className="field__label">Source URL <span aria-hidden="true">*</span></span>
          <input className="input mono" type="url" required placeholder="https://boards.greenhouse.io/acme" value={url} onChange={(e) => { setUrl(e.target.value); setPlan(null); }} />
          {url && !urlOk && <span className="field__hint jm-problem">Enter a full http(s) URL.</span>}
        </label>
        <label className="field">
          <span className="field__label">Name (optional)</span>
          <input className="input" value={name} placeholder="Detected from the source" onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span className="field__label">Schedule</span>
          <select className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)}>
            {SCHEDULES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Max pages per incremental run</span>
          <input className="input" type="number" min={1} max={100000} placeholder="200" value={maxPages} onChange={(e) => setMaxPages(e.target.value)} />
        </label>
      </div>
      <div className="form__actions">
        <button type="submit" className="button button--ghost" disabled={action.busy || !urlOk}>{action.busy && !plan ? "Checking…" : "Preview"}</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {plan && (
        <div className="jm-plan">
          <KeyValues
            items={[
              ["Name", plan.name],
              ["Source", plan.source_name],
              ["Strategy", plan.strategy ? <Pill value={plan.strategy} /> : null],
              ["Profile", plan.profile],
              ["Newest first", plan.newest_first === undefined ? null : plan.newest_first ? "Yes" : "No"],
              ["Schedule", plan.schedule],
              ["Filters", plan.filters && Object.keys(plan.filters).length ? <code className="small">{JSON.stringify(plan.filters)}</code> : null],
            ]}
          />
          {plan.steps && plan.steps.length > 0 && (
            <>
              <h4 className="section-title">Steps</h4>
              <ol className="small jm-steps">{plan.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </>
          )}
          {p && (
            <>
              <h4 className="section-title">Preview of the first page</h4>
              <p className="small">
                <Pill value={p.outcome} /> {p.reason ? <span className="muted">{p.reason}</span> : null}
                {typeof p.jobs_on_page === "number" ? ` · ${p.jobs_on_page} jobs on the page` : ""}
                {p.has_next_page !== undefined ? ` · ${p.has_next_page ? "more pages" : "single page"}` : ""}
              </p>
              <FilledCounts filled={p.filled} of={p.sample?.length} />
              <SampleTable rows={p.sample ?? []} fields={plan.fields?.length ? plan.fields : undefined} />
            </>
          )}
          <div className="form__actions">
            <label className="checkbox"><input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} /> <span>Run now</span></label>
            <button type="button" className="button button--primary" disabled={action.busy || !urlOk} onClick={() => void save()}>{action.busy ? "Saving…" : "Save monitor"}</button>
          </div>
        </div>
      )}
    </form>
  );
}

/** The Source cell: the site name, or "JobSpy · Indeed" with its keywords / freshness / limit. */
function MonitorSource({ monitor }: { monitor: Monitor }) {
  if (!isJobSpyMonitor(monitor)) return <Cell value={monitor.source_name} />;
  const s = jobSpySummary(monitor.filters);
  const terms = s.terms.length ? `${s.terms.slice(0, 4).join(", ")}${s.terms.length > 4 ? ` +${s.terms.length - 4}` : ""}` : "";
  const meta = [terms, s.freshness, s.results].filter(Boolean).join(" · ");
  return (
    <span>
      JobSpy{s.boards.length ? ` · ${s.boards.join(", ")}` : ""}
      {meta && <span className="muted small jm-block">{meta}</span>}
    </span>
  );
}

export function MonitorsPage() {
  const client = useWs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const prefill = params.get("new");
  const [open, setOpen] = useState(prefill !== null);
  const [offset, setOffset] = useState(0);
  const list = useLoad((signal) => client.list<Monitor>("/job-monitors", { limit: PAGE, offset }, signal) as Promise<PageOf<Monitor>>, `${client.base}|monitors|${offset}`);
  const close = () => {
    setOpen(false);
    if (params.has("new")) {
      const next = new URLSearchParams(params);
      next.delete("new");
      setParams(next, { replace: true });
    }
  };
  return (
    <div className="page">
      <PageHeader
        title="Monitors"
        subtitle="Careers pages and job boards checked on a schedule. Each run finds new, changed and closed jobs."
        actions={
          <>
            <Link className="button button--ghost" to="/jobs">Jobs</Link>
            {!open && <button type="button" className="button button--primary" onClick={() => setOpen(true)}>+ New monitor</button>}
          </>
        }
      />
      {open && <NewMonitorForm initialUrl={prefill ?? ""} onClose={close} onCreated={(m) => navigate(`/monitors/${m.id}`)} />}
      <div className="card">
        {list.error && <ErrorBanner error={list.error} onRetry={list.refresh} />}
        {list.loading && !list.data ? <Loading /> : list.data ? (
          <>
            <DataTable<Monitor>
              rows={list.data.items}
              link={(r) => `/monitors/${r.id}`}
              empty={{
                title: "No monitors yet",
                description: "Add a careers page or job board URL. SANA detects how to read it, previews the first page, and checks it on your schedule.",
                icon: "eye",
                action: <button type="button" className="button button--primary" onClick={() => setOpen(true)}>+ New monitor</button>,
              }}
              columns={[
                { key: "name", label: "Name", render: (r) => display(r.name) || r.id },
                { key: "source_name", label: "Source", render: (r) => <MonitorSource monitor={r} /> },
                { key: "schedule", label: "Schedule", render: (r) => <Cell value={r.schedule} /> },
                { key: "last_run_at", label: "Last Run", render: (r) => (r.last_run_at ? fmt(r.last_run_at) : <span className="muted">—</span>) },
                { key: "next_run_at", label: "Next Run", render: (r) => (r.next_run_at ? fmt(r.next_run_at) : <span className="muted">—</span>) },
                { key: "last_run_status", label: "Last status", render: (r) => <Pill value={r.last_run_status} /> },
                { key: "new", label: "New", className: "tabular", render: (r) => count(lastResult(r).new_count) },
                { key: "changed", label: "Changed", className: "tabular", render: (r) => count(lastResult(r).changed_count) },
                { key: "closed", label: "Closed", className: "tabular", render: (r) => count(lastResult(r).closed_count) },
                { key: "enabled", label: "Enabled", render: (r) => (r.enabled ? "Yes" : <span className="muted">Paused</span>) },
              ]}
            />
            <div className="pager">
              <span className="muted small tabular">{list.data.total === 0 ? "0 monitors" : `${offset + 1}–${offset + list.data.items.length} of ${list.data.total.toLocaleString()}`}</span>
              <div className="actions">
                <button className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
                <button className="button button--ghost button--small" disabled={!list.data.has_more} onClick={() => setOffset(offset + PAGE)}>Next</button>
              </div>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

// --- monitor detail ----------------------------------------------------------------------------

interface MonitorDetailResponse {
  monitor: Monitor;
  runs: MonitorRun[];
  active_run: MonitorRun | null;
  jobs: { total: number; active: number; closed: number; stale?: number; expired?: number; unknown?: number };
  new_since_last_run?: number;
  links?: { new?: string; changed?: string; closed?: string };
}

function CountLink({ monitorId, runId, change, value }: { monitorId: string; runId: string; change: string; value: unknown }) {
  const n = typeof value === "number" ? value : 0;
  if (!n) return <span className="muted tabular">{typeof value === "number" ? "0" : "—"}</span>;
  return <Link className="link tabular" to={jobsLink({ monitor: monitorId, run: runId, change })} onClick={(e) => e.stopPropagation()}>{n.toLocaleString()}</Link>;
}

/** Whole number in [min, max] from a form field; null when blank (= no window), undefined when invalid. */
function intField(raw: string, min: number, max: number, allowBlank = false): number | null | undefined {
  const text = raw.trim();
  if (!text) return allowBlank ? null : undefined;
  const n = Number(text);
  return Number.isInteger(n) && n >= min && n <= max ? n : undefined;
}

/** Lifecycle: STALE after N days, the source's visible listing window (EXPIRED, not CLOSED, beyond it), gone checks. */
function LifecycleCard({ monitor, onSaved }: { monitor: Monitor; onSaved: () => void }) {
  const client = useWs();
  const action = useAction();
  const [editing, setEditing] = useState(false);
  const [stale, setStale] = useState(String(monitor.stale_after_days ?? 30));
  const [listWindow, setListWindow] = useState(monitor.visible_window_days == null ? "" : String(monitor.visible_window_days));
  const [checks, setChecks] = useState(String(monitor.gone_checks_per_run ?? 500));
  const [autoSweep, setAutoSweep] = useState(monitor.next_full_sweep_at != null);
  const [queued, setQueued] = useState(false);
  const result = (monitor.last_lifecycle_result && typeof monitor.last_lifecycle_result === "object" ? monitor.last_lifecycle_result : {}) as LifecycleResult;
  const staleN = intField(stale, 1, 365);
  const windowN = intField(listWindow, 1, 3650, true);
  const checksN = intField(checks, 0, 20000);
  const valid = staleN !== undefined && windowN !== undefined && checksN !== undefined;
  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    void action.run(async () => {
      await patchFlat(client, `/job-monitors/${monitor.id}`, { stale_after_days: staleN, visible_window_days: windowN, gone_checks_per_run: checksN, auto_full_sweep: autoSweep });
      setEditing(false);
      onSaved();
    });
  };
  const evaluate = () =>
    action.run(async () => {
      await client.post(`/job-monitors/${monitor.id}/lifecycle`, {});
      setQueued(true);
    });
  const byStatus = result.by_status ?? {};
  const last24 = result.last_24h ?? {};
  return (
    <div className="card pad jm-lifecycle">
      <div className="title-row">
        <h3>Job lifecycle</h3>
        <span className="actions">
          <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => void evaluate()}>Evaluate now</button>
          {!editing && <button type="button" className="button button--ghost button--small" onClick={() => setEditing(true)}>Edit</button>}
        </span>
      </div>
      {queued && <p className="alert alert--info small" role="status">Evaluation queued — the worker marks stale jobs and refreshes this summary shortly.</p>}
      {action.error && <ErrorBanner error={action.error} />}
      {editing ? (
        <form className="form" onSubmit={save}>
          <div className="form-grid">
            <label className="field">
              <span className="field__label">Stale after (days)</span>
              <input className="input" type="number" min={1} max={365} value={stale} onChange={(e) => setStale(e.target.value)} />
              <span className="field__hint">An active job older than this becomes STALE (still open, flagged).</span>
            </label>
            <label className="field">
              <span className="field__label">Source listing window (days)</span>
              <input className="input" type="number" min={1} max={3650} placeholder="whole history" value={listWindow} onChange={(e) => setListWindow(e.target.value)} />
              <span className="field__hint">Jobs older than the source shows become EXPIRED, never CLOSED, when missed. Blank = the source shows its whole history.</span>
            </label>
            <label className="field">
              <span className="field__label">Job URL checks per full sweep</span>
              <input className="input" type="number" min={0} max={20000} value={checks} onChange={(e) => setChecks(e.target.value)} />
              <span className="field__hint">Missed jobs whose own page answers 404/410 are CLOSED with that evidence. 0 = off.</span>
            </label>
            <label className="field">
              <span className="field__label">Automatic weekly full sweep</span>
              <span><input type="checkbox" checked={autoSweep} onChange={(e) => setAutoSweep(e.target.checked)} /> Run a full sweep every {String(monitor.full_sweep_days ?? 7)} days</span>
              <span className="field__hint">Off = full sweeps run only when started by hand (missing / closure counters move only on completed full sweeps).</span>
            </label>
          </div>
          {!valid && <p className="small jm-problem">Use whole numbers: stale 1–365, window 1–3650 (or blank), checks 0–20000.</p>}
          <div className="form__actions">
            <button type="button" className="button button--ghost" onClick={() => setEditing(false)}>Cancel</button>
            <button type="submit" className="button button--primary" disabled={action.busy || !valid}>{action.busy ? "Saving…" : "Save"}</button>
          </div>
        </form>
      ) : (
        <KeyValues
          items={[
            ["Stale after", `${monitor.stale_after_days ?? 30} days`],
            ["Source listing window", monitor.visible_window_days ? `${monitor.visible_window_days} days` : "Whole history"],
            ["Close after", `${monitor.close_after_missed ?? 2} missed completed full sweeps (or a 404/410 job page)`],
            ["URL checks per sweep", fmt(monitor.gone_checks_per_run ?? 500)],
            ["Weekly full sweep", monitor.next_full_sweep_at ? `automatic — next ${fmt(monitor.next_full_sweep_at)}` : "manual only (automatic sweeps off)"],
            ["Last evaluation", monitor.last_lifecycle_at ? fmt(monitor.last_lifecycle_at) : null],
            ["Next evaluation", monitor.next_lifecycle_at ? fmt(monitor.next_lifecycle_at) : null],
          ]}
        />
      )}
      {monitor.last_lifecycle_at && (
        <>
          <div className="stats stats--wrap">
            <Stat label="Became stale" value={result.became_stale ? <Link className="link" to={jobsLink({ monitor: monitor.id, change: "stale", since: String(monitor.last_lifecycle_at).slice(0, 10) })}>{count(result.became_stale)}</Link> : count(result.became_stale ?? 0)} />
            <Stat label="Closure candidates" value={count(result.closure_candidates ?? 0)} hint="missed a completed full sweep" />
            {(["ACTIVE", "STALE", "EXPIRED", "CLOSED", "UNKNOWN"] as const).map((s) => (
              <Stat key={s} label={s} value={count(byStatus[s] ?? 0)} />
            ))}
          </div>
          <p className="muted small">
            Last 24 hours: {["new", "changed", "reopened", "closed", "expired"].map((k) => `${count(last24[k] ?? 0)} ${k}`).join(" · ")}
          </p>
        </>
      )}
    </div>
  );
}

export function MonitorDetail() {
  const { monitorId = "" } = useParams();
  const client = useWs();
  const [polling, setPolling] = useState(false);
  const detail = useLoad(
    async (signal) => {
      const loaded = await client.get<MonitorDetailResponse>(`/job-monitors/${encodeURIComponent(monitorId)}`, undefined, signal);
      setPolling(Boolean(loaded.active_run) || loaded.runs.some((r) => ACTIVE_RUN.includes(String(r.status))));
      return loaded;
    },
    client.base + monitorId,
    polling ? 3000 : undefined,
  );
  const action = useAction();
  if (detail.error && !detail.data) return <div className="page"><Link to="/monitors" className="back">← Monitors</Link><ErrorBanner error={detail.error} onRetry={detail.refresh} /></div>;
  if (!detail.data) return <div className="page"><Loading /></div>;
  const { monitor: m, runs, active_run: active, jobs } = detail.data;
  const latest: MonitorRun | undefined = active ?? runs[0];
  const result = lastResult(m);
  const pick = (key: string) => (latest && typeof latest[key] === "number" ? latest[key] : result[key]);
  const run = (mode: "incremental" | "full") =>
    action.run(async () => {
      await client.post(`/job-monitors/${m.id}/run`, { mode });
      setPolling(true);
      detail.refresh();
    });
  const toggle = () =>
    action.run(async () => {
      await patchFlat(client, `/job-monitors/${m.id}`, { enabled: !m.enabled });
      detail.refresh();
    });
  const cancel = (runId: string) =>
    action.run(async () => {
      await client.post(`/job-monitor-runs/${runId}/cancel`);
      detail.refresh();
    });
  const sourceUrl = safeJobUrl(m.source_url);
  const spy = isJobSpyMonitor(m) ? jobSpySummary(m.filters) : null;

  return (
    <div className="page">
      <Link to="/monitors" className="back">← Monitors</Link>
      <PageHeader
        title={display(m.name) || "Monitor"}
        crumbTitle="Monitor"
        subtitle={
          spy ? (
            <>JobSpy{spy.boards.length ? ` · ${spy.boards.join(", ")}` : ""}{spy.terms.length ? ` · ${spy.terms.length} keyword${spy.terms.length === 1 ? "" : "s"}` : ""}</>
          ) : (
            <>
              {display(m.source_name) || "Source"}
              {" · "}
              {sourceUrl ? <a className="link" href={sourceUrl} target="_blank" rel="noopener noreferrer">{display(m.source_url)}</a> : display(m.source_url)}
            </>
          )
        }
        actions={
          <>
            {m.enabled ? null : <Pill value="paused" />}
            <button type="button" className="button button--primary" disabled={action.busy || Boolean(active)} onClick={() => void run("incremental")}>Run now (incremental)</button>
            <button type="button" className="button button--ghost" disabled={action.busy || Boolean(active)} onClick={() => void run("full")}>Run full sweep</button>
            <button type="button" className="button button--ghost" disabled={action.busy} onClick={() => void toggle()}>{m.enabled ? "Pause" : "Resume"}</button>
          </>
        }
      />
      {action.error && <ErrorBanner error={action.error} />}
      {detail.error && <ErrorBanner error={detail.error} onRetry={detail.refresh} />}
      {active && (
        <p className="alert alert--info" role="status">
          <span>A {display(active.mode)} run is {display(active.status)} — {count(active.pages)} pages, {count(active.found)} jobs found so far. This page refreshes every 3 seconds.</span>
          <button type="button" className="link-button" disabled={action.busy} onClick={() => void cancel(active.id)}>Cancel run</button>
        </p>
      )}
      <div className="stats stats--wrap">
        <Stat label="Jobs found" value={count(pick("found"))} />
        <Stat label="New" value={latest ? <CountLink monitorId={m.id} runId={latest.id} change="new" value={pick("new_count")} /> : count(result.new_count)} />
        <Stat label="Changed" value={latest ? <CountLink monitorId={m.id} runId={latest.id} change="changed" value={pick("changed_count")} /> : count(result.changed_count)} />
        <Stat label="Closed" value={latest ? <CountLink monitorId={m.id} runId={latest.id} change="closed" value={pick("closed_count")} /> : count(result.closed_count)} />
        <Stat label="Expired" value={latest ? <CountLink monitorId={m.id} runId={latest.id} change="expired" value={pick("expired_count")} /> : count(result.expired_count)} />
        <Stat label="Errors" value={count(pick("error_count"))} />
      </div>
      <div className="detail-grid">
        <div className="card pad">
          <KeyValues
            items={[
              ["Monitor Name", display(m.name)],
              ...(spy
                ? ([
                    ["Source", "JobSpy"],
                    ["Board(s)", spy.boards.join(", ")],
                    ["Keywords", <Chips values={spy.terms} />],
                    ["Location", spy.location],
                    ["Freshness", spy.freshness],
                    ["Results limit", spy.results],
                  ] as [string, ReactNode][])
                : ([["Source", <>{display(m.source_name) || "—"}{sourceUrl ? <> · <a className="link" href={sourceUrl} target="_blank" rel="noopener noreferrer">open</a></> : null}</>]] as [string, ReactNode][])),
              ["Schedule", display(m.schedule)],
              ["Last Run", m.last_run_at ? fmt(m.last_run_at) : null],
              ["Next Run", m.next_run_at ? fmt(m.next_run_at) : null],
              ["Last Run Status", <Pill value={latest?.status ?? m.last_run_status} />],
              ["Stop reason", latest?.stop_reason ? <span className="jm-reason">{String(latest.stop_reason)}</span> : null],
              ...(spy ? [] : ([["Max pages (incremental)", fmt(m.max_pages_incremental)]] as [string, ReactNode][])),
              ["Enabled", m.enabled ? "Yes" : "Paused"],
            ]}
          />
        </div>
        <div className="card pad">
          <h3>Jobs from this monitor</h3>
          <div className="stats">
            <Stat label="Total" value={<Link className="link" to={jobsLink({ monitor: m.id })}>{count(jobs?.total)}</Link>} />
            <Stat label="Active" value={count(jobs?.active)} />
            <Stat label="Stale" value={<Link className="link" to={jobsLink({ monitor: m.id, change: "stale" })}>{count(jobs?.stale)}</Link>} />
            <Stat label="Expired" value={<Link className="link" to={jobsLink({ monitor: m.id, change: "expired" })}>{count(jobs?.expired)}</Link>} />
            <Stat label="Closed" value={count(jobs?.closed)} />
          </div>
          <div className="actions">
            <Link className="button button--ghost button--small" to={jobsLink({ monitor: m.id, since_last_run: true })}>New since last run{typeof detail.data.new_since_last_run === "number" ? ` (${detail.data.new_since_last_run})` : ""}</Link>
            <Link className="button button--ghost button--small" to={jobsLink({ monitor: m.id })}>All jobs</Link>
            <Link className="button button--ghost button--small" to={jobsLink({ monitor: m.id, change: "stale" })}>Stale</Link>
            <Link className="button button--ghost button--small" to={jobsLink({ monitor: m.id, change: "expired" })}>Expired</Link>
          </div>
        </div>
        <LifecycleCard key={`${m.id}|${m.updated_at ?? ""}`} monitor={m} onSaved={detail.refresh} />
      </div>
      <div className="card">
        <div className="card__header"><h2>Runs</h2></div>
        <DataTable<MonitorRun>
          rows={runs}
          empty={{ title: "No runs yet", description: "Run the monitor now, or wait for its schedule.", icon: "clock" }}
          columns={[
            { key: "mode", label: "Mode", render: (r) => <Cell value={r.mode} /> },
            { key: "trigger", label: "Trigger", render: (r) => <Cell value={r.trigger} /> },
            { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
            { key: "started_at", label: "Started", render: (r) => (r.started_at ? fmt(r.started_at) : <span className="muted">—</span>) },
            { key: "finished_at", label: "Finished", render: (r) => (r.finished_at ? fmt(r.finished_at) : <span className="muted">—</span>) },
            { key: "pages", label: "Pages", className: "tabular", render: (r) => count(r.pages) },
            { key: "found", label: "Found", className: "tabular", render: (r) => count(r.found) },
            { key: "new_count", label: "New", render: (r) => <CountLink monitorId={m.id} runId={r.id} change="new" value={r.new_count} /> },
            { key: "changed_count", label: "Changed", render: (r) => <CountLink monitorId={m.id} runId={r.id} change="changed" value={r.changed_count} /> },
            { key: "unchanged_count", label: "Unchanged", className: "tabular", render: (r) => count(r.unchanged_count) },
            { key: "reopened_count", label: "Reopened", render: (r) => <CountLink monitorId={m.id} runId={r.id} change="reopened" value={r.reopened_count} /> },
            { key: "closed_count", label: "Closed", render: (r) => <CountLink monitorId={m.id} runId={r.id} change="closed" value={r.closed_count} /> },
            { key: "expired_count", label: "Expired", render: (r) => <CountLink monitorId={m.id} runId={r.id} change="expired" value={r.expired_count} /> },
            {
              key: "gone", label: "URL checks", className: "tabular small",
              render: (r) => (typeof r.gone_checked_count === "number" && r.gone_checked_count > 0
                ? <span title="job URLs checked / closed as gone (404/410)">{count(r.gone_checked_count)} / {count(r.gone_closed_count ?? 0)} gone</span>
                : <span className="muted">—</span>),
            },
            { key: "error_count", label: "Errors", className: "tabular", render: (r) => count(r.error_count) },
            { key: "stop_reason", label: "Stop reason", className: "small", render: (r) => <Cell value={r.stop_reason ?? r.error} /> },
          ]}
        />
      </div>
      <p className="muted small">Created {fmtDate(m.created_at)}.</p>
    </div>
  );
}

