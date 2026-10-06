// Jobs -> Hiring companies (Account Builder): companies found in job postings and hiring signals,
// grouped by normalised name, with their CRM state. Create or link CRM accounts in bulk (bounded,
// never automatic), resolve ambiguous matches, and add companies to prospect lists — keeping the
// originating jobs and signals as provenance.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import {
  FILTER_STATES, LIMITS, VIEW_KEYS, createEffect, jobsForCompany, readView, selectionCheck, stateLabel, stateTone,
  taskStopped, toItem, type CrmRef, type HiringCompany,
} from "../logic/accounts";
import { parseFilters } from "../logic/jobFilters";
import { useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import { canWrite } from "../logic/admin";
import { CompanyPicker, ListPicker, Modal, listChoiceReady, type ListChoice } from "./accountsShared";

interface Page {
  rows: HiringCompany[];
  total: number;
  all_total?: number;
  limit: number;
  offset: number;
  states: Record<string, number>;
  jobs_considered: number;
}

export interface BuildResult {
  mode: "sync" | "task";
  task_id?: string;
  queued?: number;
  stats?: Record<string, number>;
  results?: { name: string; outcome: string; company_id?: string; company_name?: string; reason?: string; error?: string }[];
  list?: Row | null;
}

const PAGE = 50;

function n(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

const signalText = (s: string) => s.replace(/_/g, " ").toLowerCase();

function Badge({ state }: { state: string }) {
  return <span className={`badge badge--${stateTone(state)}`}><span className="badge__dot" aria-hidden="true" />{stateLabel(state)}</span>;
}

export function HiringCompanies() {
  const client = useWs();
  const writable = canWrite(useWorkspace().current?.role);
  const [params, setParams] = useSearchParams();
  const jobFilters = useMemo(() => parseFilters(params), [params]);
  // CRM status, sort and page live in the URL with the Jobs filters, so Back (e.g. from a
  // company page) returns to the same view.
  const { state: stateFilter, sort, offset } = readView(params);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Record<string, HiringCompany>>({});
  const [dialog, setDialog] = useState<null | { kind: "create" | "list"; rows: HiringCompany[] } | { kind: "link" | "review"; row: HiringCompany }>(null);
  const [result, setResult] = useState<BuildResult | null>(null);
  const query = { ...jobFilters, state: stateFilter || undefined, sort, limit: PAGE, offset } as Record<string, string | number | undefined>;
  // Refresh (and the reload after a create / link) recomputes once instead of reusing the cached grouping
  const fresh = useRef(false);
  const refresh = () => { fresh.current = true; setReload((x) => x + 1); };
  const page = useLoad((signal) => {
    const once = fresh.current;
    fresh.current = false;
    return client.get<Page>("/hiring-companies", (once ? { ...query, refresh: 1 } : query) as never, signal);
  }, `${client.base}|hiring|${JSON.stringify(query)}|${reload}`);
  const rows = page.data?.rows ?? [];
  const chosen = Object.values(selected);
  const allVisible = rows.length > 0 && rows.every((r) => selected[r.key]);
  const toggle = (row: HiringCompany) => setSelected((s) => {
    const next = { ...s };
    if (next[row.key]) delete next[row.key];
    else next[row.key] = row;
    return next;
  });
  const toggleAll = () => setSelected((s) => {
    const next = { ...s };
    if (allVisible) rows.forEach((r) => delete next[r.key]);
    else rows.forEach((r) => { next[r.key] = r; });
    return next;
  });
  const setView = (changes: Record<string, string | number | undefined>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === undefined || value === "" || value === 0) next.delete(key);
      else next.set(key, String(value));
    }
    if (resetPage) next.delete(VIEW_KEYS.offset);
    next.set("tab", "hiring");
    setParams(next);
  };
  const setFilter = (key: string, value: string) => setView({ [key]: value });
  const setStateFilter = (value: string) => setView({ [VIEW_KEYS.state]: value });
  const setSort = (value: string) => setView({ [VIEW_KEYS.sort]: value === "open" ? "" : value });
  const setOffset = (value: number) => setView({ [VIEW_KEYS.offset]: value }, false);
  const activeJobFilters = Object.entries(jobFilters).filter(([k]) => !["company", "relevance", "change"].includes(k));
  const done = (res: BuildResult) => {
    setResult(res);
    setDialog(null);
    setSelected({});
    refresh();
  };

  return (
    <div className="card ab">
      <div className="pad">
        <p className="muted small">
          Companies hiring in your jobs, grouped by company name. Create or link CRM accounts in bulk — up to {LIMITS.maxPerAction} per action, never automatically.
          An exact name already in the CRM is linked, an ambiguous one goes to review, and a duplicate is never created silently.
        </p>
        <div className="ab-toolbar">
          <input key={jobFilters.company ?? ""} className="input input--small" placeholder="Company contains… (Enter)" defaultValue={jobFilters.company ?? ""}
                 onKeyDown={(e) => { if (e.key === "Enter") setFilter("company", (e.target as HTMLInputElement).value.trim()); }}
                 onBlur={(e) => { const v = e.target.value.trim(); if (v !== (jobFilters.company ?? "")) setFilter("company", v); }}
                 aria-label="Filter by company name" />
          <select className="input input--small" value={jobFilters.change ?? ""} onChange={(e) => setFilter("change", e.target.value)} aria-label="Job status">
            <option value="">Any job status</option>
            <option value="new">New in the last run</option>
            <option value="stale">Stale jobs</option>
            <option value="expired">Expired jobs</option>
            <option value="closed">Closed jobs</option>
            <option value="reopened">Reopened jobs</option>
          </select>
          <div className="chips" role="group" aria-label="Relevance">
            {["HIGH", "REVIEW"].map((rel) => {
              const on = (jobFilters.relevance ?? "").split(",").includes(rel);
              return (
                <button key={rel} type="button" className={`chip${on ? " chip--on" : ""}`} aria-pressed={on}
                        onClick={() => {
                          const set = new Set((jobFilters.relevance ?? "").split(",").filter(Boolean));
                          if (on) set.delete(rel); else set.add(rel);
                          setFilter("relevance", [...set].join(","));
                        }}>{rel}</button>
              );
            })}
          </div>
          <select className="input input--small" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
            <option value="open">Most open jobs</option>
            <option value="high">Most HIGH jobs</option>
            <option value="jobs">Most jobs</option>
            <option value="name">Name</option>
          </select>
          <button type="button" className="button button--ghost button--small" disabled={page.loading} onClick={() => refresh()}>{page.loading && page.data ? "Refreshing…" : "Refresh"}</button>
        </div>
        {activeJobFilters.length > 0 && (
          <p className="small ab-active">
            Also filtered by the Jobs tab: {activeJobFilters.map(([k, v]) => `${k.replace(/_/g, " ")} = ${v}`).join(" · ")}{" "}
            <button type="button" className="link-button small" onClick={() => setView(Object.fromEntries(activeJobFilters.map(([k]) => [k, ""])))}>Clear</button>
          </p>
        )}
        <div className="chips ab-states" role="group" aria-label="CRM status">
          <button type="button" className={`chip${stateFilter === "" ? " chip--on" : ""}`} aria-pressed={stateFilter === ""} onClick={() => setStateFilter("")}>
            All {page.data ? `(${n(page.data.all_total ?? page.data.total)})` : ""}
          </button>
          {FILTER_STATES.map((s) => (
            <button key={s} type="button" className={`chip${stateFilter === s ? " chip--on" : ""}`} aria-pressed={stateFilter === s} onClick={() => setStateFilter(s)}>
              {stateLabel(s)} {page.data ? `(${n(page.data.states?.[s] ?? 0)})` : ""}
            </button>
          ))}
        </div>
        <p className="muted small">Counts compare exact company names with the CRM. Similar (not identical) CRM names are checked for the companies shown and marked <em>Matched</em> or <em>Review</em>.</p>
        {!writable && <p className="alert alert--info small" role="note">You can view hiring companies. Members, managers and admins can create or link accounts and build lists.</p>}
        {writable && <div className="ab-bulk" role="toolbar" aria-label="Bulk actions">
          <label className="ab-check"><input type="checkbox" checked={allVisible} onChange={toggleAll} /> Select all visible</label>
          <span className="muted small">{chosen.length.toLocaleString()} selected</span>
          <button type="button" className="button button--primary button--small" disabled={!chosen.length} onClick={() => setDialog({ kind: "create", rows: chosen })}>Create accounts</button>
          <button type="button" className="button button--ghost button--small" disabled={!chosen.length} onClick={() => setDialog({ kind: "list", rows: chosen })}>Add to list</button>
          {chosen.length > 0 && <button type="button" className="link-button small" onClick={() => setSelected({})}>Clear selection</button>}
        </div>}
      </div>
      {result && <BuildSummary result={result} onClose={() => setResult(null)} onFinished={() => refresh()} />}
      {notice && <div className="alert alert--success ab-summary" role="status"><p>{notice}</p><button type="button" className="link-button small" onClick={() => setNotice(null)}>Dismiss</button></div>}
      {page.error && <ErrorBanner error={page.error} onRetry={page.refresh} />}
      {page.loading && !page.data ? <Loading /> : rows.length === 0 ? (
        <EmptyState icon="search" title="No hiring companies" description="No job matches these filters, or every company is filtered out by the CRM status." />
      ) : (
        <div className="table-wrap">
          <table className="table ab-table">
            <thead>
              <tr>
                <th aria-label="Select" />
                <th>Company</th><th className="tabular">Open jobs</th><th className="tabular" title="Open jobs classed HIGH">HIGH</th><th className="tabular" title="Open jobs classed REVIEW">REVIEW</th>
                <th>Technologies</th><th>Signals</th><th>CRM status</th><th className="tabular">Score</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key} className="table__row">
                  <td>{writable && <input type="checkbox" checked={Boolean(selected[r.key])} onChange={() => toggle(r)} aria-label={`Select ${r.display_name}`} />}</td>
                  <td data-label="Company">
                    <strong>{r.display_name}</strong>
                    {r.names.length > 1 && <div className="muted small" title={r.names.join(" · ")}>{r.names.length} spellings</div>}
                  </td>
                  <td data-label="Open jobs" className="tabular" title={`${n(r.jobs)} jobs match the filters (open, stale, expired and closed)`}><Link className="link" to={jobsForCompany(r.display_name)}>{n(r.open_jobs)}</Link></td>
                  <td data-label="HIGH" className="tabular">{n(r.high)}</td>
                  <td data-label="REVIEW" className="tabular">{n(r.review)}</td>
                  <td data-label="Technologies" className="small">{(r.technologies ?? []).slice(0, 4).join(", ") || <span className="muted">—</span>}</td>
                  <td data-label="Signals" className="small" title={(r.signals ?? []).map(signalText).join(", ")}>
                    {(r.signals ?? []).slice(0, 3).map(signalText).join(", ") || <span className="muted">—</span>}
                    {(r.signals ?? []).length > 3 && <span className="muted"> +{(r.signals ?? []).length - 3} more</span>}
                  </td>
                  <td data-label="CRM status">
                    <Badge state={r.state} />
                    {r.crm?.length === 1 && (r.state !== "REVIEW") && <div className="small"><Link className="link" to={`/companies/${encodeURIComponent(r.crm[0].id)}`}>{r.crm[0].name}</Link></div>}
                    {r.state === "REVIEW" && r.crm && r.crm.length > 0 && <div className="muted small">Possible: {r.crm.map((c) => c.name).join(", ")}</div>}
                  </td>
                  <td data-label="Score" className="tabular" title={r.score_source === "crm" ? "CRM hiring score" : "Hiring score preview from this company's signals"}>
                    {typeof r.hiring_score === "number" ? r.hiring_score.toFixed(0) : "—"}{r.score_source === "signals" && typeof r.hiring_score === "number" ? <span className="muted small"> preview</span> : null}
                  </td>
                  <td className="actions">
                    {!writable ? <span className="muted small">View only</span> : null}
                    {writable && (r.state === "REVIEW" ? (
                      <button type="button" className="button button--ghost button--small" title="Several CRM companies could be this company — pick one, or create a new account"
                              onClick={() => setDialog({ kind: "review", row: r })}>Review…</button>
                    ) : r.state !== "LINKED" ? (
                      <button type="button" className="button button--ghost button--small" title={createEffect(r)} onClick={() => setDialog({ kind: "create", rows: [r] })}>
                        {r.state === "NEW ACCOUNT" ? "Create account" : "Link"}
                      </button>
                    ) : null)}
                    {writable && r.state !== "LINKED" && r.state !== "REVIEW" && <button type="button" className="button button--ghost button--small" onClick={() => setDialog({ kind: "link", row: r })}>Link to…</button>}
                    {writable && <button type="button" className="button button--ghost button--small" onClick={() => setDialog({ kind: "list", rows: [r] })}>Add to list</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {page.data && page.data.total > PAGE && (
        <div className="pager pad">
          <button type="button" className="button button--ghost button--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <span className="muted small">{n(offset + 1)}–{n(Math.min(offset + PAGE, page.data.total))} of {n(page.data.total)}</span>
          <button type="button" className="button button--ghost button--small" disabled={offset + PAGE >= page.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}
      {dialog?.kind === "create" && <CreateDialog rows={dialog.rows} withList={false} onClose={() => setDialog(null)} onDone={done} />}
      {dialog?.kind === "list" && <CreateDialog rows={dialog.rows} withList onClose={() => setDialog(null)} onDone={done} />}
      {dialog?.kind === "review" && <ReviewDialog row={dialog.row} onClose={() => setDialog(null)}
        onLinked={(msg) => { setDialog(null); setResult(null); setNotice(msg); refresh(); }} onDone={done} />}
      {dialog?.kind === "link" && <LinkDialog row={dialog.row} onClose={() => setDialog(null)} onDone={(msg) => { setDialog(null); setResult(null); setNotice(msg); refresh(); }} />}
    </div>
  );
}

export function CreateDialog({ rows, withList, onClose, onDone }: { rows: HiringCompany[]; withList: boolean; onClose: () => void; onDone: (r: BuildResult) => void }) {
  const client = useWs();
  const action = useAction();
  const [confirmed, setConfirmed] = useState(false);
  const [createNew, setCreateNew] = useState(false);
  const [choice, setChoice] = useState<ListChoice>(withList ? { new_list: "" } : {});
  const check = selectionCheck(rows.length, confirmed);
  const counts = rows.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.state]: (acc[r.state] ?? 0) + 1 }), {});
  const review = counts.REVIEW ?? 0;
  const listOk = !withList || listChoiceReady(choice);
  const go = () => action.run(async () => {
    const res = await client.post<BuildResult>("/hiring-companies/build", {
      items: rows.map((r) => toItem(r, createNew && r.state === "REVIEW")), confirm: confirmed,
      ...(choice.list_id ? { list_id: choice.list_id } : {}), ...(choice.new_list ? { new_list: choice.new_list.trim() } : {}),
    });
    onDone(res);
  });
  return (
    <Modal label={withList ? "Add to list" : "Create accounts"} onClose={onClose}>
        <h3>{withList ? "Add to list" : "Create accounts"} — {rows.length.toLocaleString()} compan{rows.length === 1 ? "y" : "ies"}</h3>
        <ul className="small ab-effects">
          {(counts["NEW ACCOUNT"] ?? 0) > 0 && <li><strong>{counts["NEW ACCOUNT"]}</strong> new CRM account{counts["NEW ACCOUNT"] === 1 ? "" : "s"} will be created</li>}
          {((counts["ALREADY IN CRM"] ?? 0) + (counts.MATCHED ?? 0)) > 0 && <li><strong>{(counts["ALREADY IN CRM"] ?? 0) + (counts.MATCHED ?? 0)}</strong> will link to the existing CRM company (no duplicate)</li>}
          {(counts.LINKED ?? 0) > 0 && <li><strong>{counts.LINKED}</strong> already linked — any new jobs and signals are attached, nothing else changes</li>}
          {review > 0 && <li><strong>{review}</strong> ambiguous — {createNew ? "will be created as NEW accounts (your explicit choice)" : "sent to review, nothing created"}</li>}
          <li>SANA checks the CRM first: an exact company name is linked, an ambiguous one goes to review.</li>
          <li>Every job and job-derived signal of each company is attached; hiring counts and scores refresh.</li>
          {withList && <li>Companies not yet in the CRM are created or linked first; each list entry records the originating signals and jobs.</li>}
        </ul>
        {review > 0 && (
          <label className="ab-check"><input type="checkbox" checked={createNew} onChange={(e) => setCreateNew(e.target.checked)} /> Create new accounts for the ambiguous companies anyway (I checked they are not duplicates)</label>
        )}
        {withList && <ListPicker value={choice} onChange={setChoice} />}
        {rows.length > LIMITS.confirmAbove && (
          <label className="ab-check"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /> I confirm {rows.length.toLocaleString()} companies ({check.background ? "runs in the background" : "runs now"})</label>
        )}
        {check.message && !check.ok && <p className="alert alert--warning small">{check.message}</p>}
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions">
          <button type="button" className="button button--ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="button button--primary" disabled={!check.ok || !listOk || action.busy} onClick={() => void go()}>
            {action.busy ? "Working…" : withList ? "Add to list" : "Create accounts"}
          </button>
        </div>
    </Modal>
  );
}

/** An ambiguous company: link it to one of the candidate CRM companies (or any other), or —
 * only with an explicit confirmation — create a new account. Nothing happens automatically. */
export function ReviewDialog({ row, onClose, onLinked, onDone }: { row: HiringCompany; onClose: () => void; onLinked: (message: string) => void; onDone: (r: BuildResult) => void }) {
  const client = useWs();
  const action = useAction();
  const [company, setCompany] = useState<CrmRef | null>(null);
  const [sure, setSure] = useState(false);
  const link = () => action.run(async () => {
    if (!company) return;
    const res = await client.post<{ company_name?: string; jobs_attached?: number; signals_attached?: number }>(
      "/hiring-companies/link", { item: toItem(row), company_id: company.id });
    onLinked(`Linked “${row.display_name}” to ${res.company_name ?? company.name}: ${n(res.jobs_attached)} jobs and ${n(res.signals_attached)} signals now attached.`);
  });
  const create = () => action.run(async () => {
    onDone(await client.post<BuildResult>("/hiring-companies/build", { items: [toItem(row, true)] }));
  });
  return (
    <Modal label={`Review ${row.display_name}`} onClose={onClose}>
        <h3>Review “{row.display_name}”</h3>
        <p className="small">{row.match_reason ? <>Why: {row.match_reason}. </> : null}{n(row.open_jobs)} open jobs{row.signals?.length ? <> · signals: {row.signals.map((s) => s.replace(/_/g, " ").toLowerCase()).join(", ")}</> : null}</p>
        <p className="small"><strong>Possible CRM companies</strong> — pick the right one below:</p>
        <ul className="small ab-effects">
          {(row.crm ?? []).map((c) => (
            <li key={c.id}>
              <strong>{c.name}</strong>
              {c.domain ? <span className="muted"> · {c.domain}</span> : null}{" "}
              <Link className="link small" to={`/companies/${encodeURIComponent(c.id)}`} target="_blank" rel="noreferrer">open ↗</Link>
            </li>
          ))}
        </ul>
        <CompanyPicker value={company} onChange={setCompany} suggestions={row.crm ?? []} />
        <label className="ab-check"><input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} /> None of these — it is a different company (create a new account)</label>
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions">
          <button type="button" className="button button--ghost" onClick={onClose}>Keep reviewing</button>
          <button type="button" className="button button--ghost" disabled={!sure || action.busy} onClick={() => void create()}>Create new account</button>
          <button type="button" className="button button--primary" disabled={!company || action.busy} onClick={() => void link()}>{action.busy ? "Working…" : company ? `Link to ${company.name}` : "Link"}</button>
        </div>
    </Modal>
  );
}

export function LinkDialog({ row, onClose, onDone }: { row: HiringCompany; onClose: () => void; onDone: (message: string) => void }) {
  const client = useWs();
  const action = useAction();
  const [company, setCompany] = useState<CrmRef | null>(null);
  const go = () => action.run(async () => {
    if (!company) return;
    const res = await client.post<{ company_name?: string; jobs_attached?: number; signals_attached?: number }>(
      "/hiring-companies/link", { item: toItem(row), company_id: company.id });
    onDone(`Linked “${row.display_name}” to ${res.company_name ?? company.name}: ${n(res.jobs_attached)} jobs and ${n(res.signals_attached)} signals now attached.`);
  });
  return (
    <Modal label="Link to an existing company" onClose={onClose}>
        <h3>Link “{row.display_name}” to an existing CRM company</h3>
        <p className="muted small">All {n(row.jobs)} jobs and its signals will point at the company you choose. Nothing new is created.</p>
        <CompanyPicker value={company} onChange={setCompany} suggestions={row.crm ?? []} autoFocus />
        {action.error && <ErrorBanner error={action.error} />}
        <div className="form__actions">
          <button type="button" className="button button--ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="button button--primary" disabled={!company || action.busy} onClick={() => void go()}>{action.busy ? "Linking…" : "Link"}</button>
        </div>
    </Modal>
  );
}

export function BuildSummary({ result, onClose, onFinished }: { result: BuildResult; onClose: () => void; onFinished?: () => void }) {
  const client = useWs();
  const task = useLoad(
    (signal) => (result.mode === "task" && result.task_id ? client.get<Row>(`/tasks/${encodeURIComponent(result.task_id)}`, undefined, signal) : Promise.resolve(null)),
    `${client.base}|ab-task|${result.task_id ?? ""}`, result.mode === "task" ? 2000 : undefined,
  );
  const t = task.data as Row | null;
  const progress = (t?.progress ?? {}) as Record<string, unknown>;
  const status = String(t?.status ?? "queued");
  const finished = result.mode === "task" && taskStopped(status);
  const stats = (result.stats ?? (progress.stats as Record<string, number> | undefined) ?? {}) as Record<string, number>;
  // the table refreshes once when a background build stops, so states and counts are current
  useEffect(() => { if (finished) onFinished?.(); }, [finished]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className={`alert ${result.mode === "task" && status === "failed" ? "alert--error" : "alert--info"} ab-summary`} role="status">
      {result.mode === "task" && !finished ? (
        <p>Running in the background: {n(Number(progress.done ?? 0))} of {n(result.queued)} companies · {status}. You can leave this page.</p>
      ) : result.mode === "task" ? (
        <p>
          Background build {status}: <strong>{n(stats.created ?? 0)}</strong> created · <strong>{n(stats.linked ?? 0)}</strong> linked · <strong>{n(stats.review ?? 0)}</strong> to review
          {stats.errors ? <> · <strong>{n(stats.errors)}</strong> errors</> : null} · {n(stats.jobs_linked ?? 0)} jobs and {n(stats.signals_linked ?? 0)} signals attached
          {status === "paused" ? " · resume it from Tasks" : ""}
        </p>
      ) : (
        <p>
          Done: <strong>{n(stats.created)}</strong> created · <strong>{n(stats.linked)}</strong> linked · <strong>{n(stats.review)}</strong> to review
          {stats.skipped ? <> · {n(stats.skipped)} already up to date</> : null}{stats.errors ? <> · <strong>{n(stats.errors)}</strong> errors</> : null}
          {" "}· {n(stats.jobs_linked)} jobs and {n(stats.signals_linked)} signals attached
          {result.list ? <> · {n(stats.listed)} added to <Link className="link" to={`/lists/${encodeURIComponent(String(result.list.id))}`}>{String(result.list.name)}</Link></> : null}
        </p>
      )}
      {(result.results ?? []).filter((r) => r.outcome === "error").map((r) => <p key={r.name} className="small">{r.name}: {r.error}</p>)}
      <button type="button" className="link-button small" onClick={onClose}>Dismiss</button>
    </div>
  );
}
