// The Research Canvas: REQUEST · PLAN · SOURCES · LIVE PROGRESS · RESULTS · EVIDENCE · ACTIONS
// for one Control Room run. Every score is shown with its reason codes; every
// high-impact action waits behind an explicit approval dialog.

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { Json, Pill, Score, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import {
  TERMINAL,
  creditsText,
  type AgentRun,
  type Approval,
  type CompanyResult,
  type EvidenceItem,
  type PlanStep,
  type ReasonCode,
  type ToolInfo,
} from "./types";

// --- small pieces ------------------------------------------------------------------

function StepIcon({ status }: { status: string }) {
  const icon = status === "done" ? "✓" : status === "failed" ? "✗" : status === "rejected" ? "⊘" : status === "skipped" ? "–" : status === "running" ? "…" : "⏳";
  const tone = status === "done" ? "ok" : status === "failed" || status === "rejected" ? "bad" : "wait";
  return <span className={`cr-icon cr-icon--${tone}`} aria-label={status}>{icon}</span>;
}

function RiskPill({ risk }: { risk: string }) {
  return <span className={`cr-risk cr-risk--${risk}`}>{risk}</span>;
}

export function ReasonChips({ reasons, max = 4 }: { reasons?: ReasonCode[]; max?: number }) {
  if (!reasons || reasons.length === 0) return <span className="muted small">no scored evidence yet</span>;
  return (
    <span className="chips">
      {reasons.slice(0, max).map((r, i) => (
        <span key={`${r.code}-${i}`} className={`chip ${r.points >= 0 ? "cr-plus" : "cr-minus"}`} title={r.label}>
          {r.code}
        </span>
      ))}
      {reasons.length > max && <span className="chip chip--more">+{reasons.length - max}</span>}
    </span>
  );
}

function Section({ id, title, children, actions }: { id: string; title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card pad cr-section" aria-labelledby={`cr-${id}`}>
      <div className="title-row">
        <h3 id={`cr-${id}`} className="cr-section__title">{title}</h3>
        {actions && <div className="actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export function ConfirmDialog({ title, children, confirmLabel, danger, busy, onConfirm, onCancel }: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  // Runs once per dialog: focus Cancel (never the confirm button, so Enter cannot
  // approve by accident), close on Escape, restore focus on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") cancelRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (previous && document.contains(previous)) previous.focus();
    };
  }, []);
  return (
    <div className="cr-modal" role="presentation" onClick={onCancel}>
      <div className="cr-modal__box card pad" role="dialog" aria-modal="true" aria-labelledby="cr-dialog-title" ref={ref} onClick={(e) => e.stopPropagation()}>
        <h3 id="cr-dialog-title">{title}</h3>
        <div className="cr-modal__body">{children}</div>
        <div className="form__actions">
          <button type="button" className="button button--ghost" onClick={onCancel}>Cancel</button>
          <button type="button" className={`button ${danger ? "button--danger" : "button--primary"}`} disabled={busy} onClick={onConfirm}>
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// --- PLAN -----------------------------------------------------------------------------

function PlanCards({ plan }: { plan: PlanStep[] }) {
  return (
    <ol className="cr-plan">
      {plan.map((step) => {
        const credits = creditsText(step.credits);
        return (
          <li key={step.id} className={`cr-plan__card cr-plan__card--${step.status}`}>
            <div className="cr-plan__head">
              <StepIcon status={step.status} />
              <strong>{step.title || step.tool}</strong>
              <RiskPill risk={step.risk} />
              {step.requires_approval && <span className="chip chip--warn">needs approval</span>}
            </div>
            <div className="muted small">
              <code>{step.tool}</code>
              {step.affected ? ` · ${step.affected.toLocaleString()} affected` : ""}
              {credits ? ` · credits: ${credits}` : ""}
            </div>
            {step.explain && <div className="small cr-explain">{step.explain}</div>}
            {step.detail && <div className="small">{step.detail}</div>}
          </li>
        );
      })}
    </ol>
  );
}

function EstimatePanel({ run }: { run: AgentRun }) {
  const est = run.estimate ?? {};
  const counts = est.counts ?? {};
  const credits = creditsText(est.credits);
  return (
    <div className="cr-estimate">
      <div className="cr-estimate__grid">
        {(["companies", "jobs", "contacts", "missing_contacts", "emails_unvalidated"] as const).map((key) =>
          counts[key] !== undefined ? (
            <div key={key} className="cr-estimate__item">
              <span className="muted small">{key.replace(/_/g, " ")}</span>
              <strong className="tabular">~{Number(counts[key]).toLocaleString()}</strong>
            </div>
          ) : null,
        )}
        <div className="cr-estimate__item">
          <span className="muted small">paid credits (upper bound)</span>
          <strong className={credits ? "cr-warn" : ""}>{credits || "0"}</strong>
        </div>
      </div>
      {est.expected && <p className="small">Expected: {est.expected}</p>}
      {(est.explain ?? []).filter(Boolean).map((line, i) => (
        <p key={i} className="small cr-explain">{line}</p>
      ))}
      {est.note && <p className="muted small">{est.note}</p>}
    </div>
  );
}

interface EditableStep { tool: string; params: string; title: string }

function PlanEditor({ run, onSaved, onClose }: { run: AgentRun; onSaved: (run: AgentRun) => void; onClose: () => void }) {
  const client = useWs();
  const tools = useLoad((signal) => client.get<{ items: ToolInfo[] }>("/agent/tools", { mode: run.mode }, signal), client.base + "tools" + run.mode);
  const [steps, setSteps] = useState<EditableStep[]>(() =>
    run.plan.map((s) => ({ tool: s.tool, title: s.title ?? s.tool, params: JSON.stringify(s.params ?? {}, null, 1) })),
  );
  const action = useAction();
  const move = (i: number, delta: number) =>
    setSteps((list) => {
      const next = [...list];
      const j = i + delta;
      if (j < 0 || j >= next.length) return list;
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  const save = () =>
    action.run(async () => {
      const payload = steps.map((s) => {
        let params: unknown;
        try {
          params = JSON.parse(s.params || "{}");
        } catch {
          throw new Error(`Parameters for "${s.title || s.tool}" are not valid JSON`);
        }
        return { tool: s.tool, title: s.title, params };
      });
      const updated = await client.put<AgentRun>(`/agent/runs/${run.id}/plan`, { steps: payload });
      onSaved(updated);
    });
  const options = tools.data?.items ?? [];
  return (
    <div className="cr-editor">
      {steps.map((s, i) => (
        <div key={i} className="cr-editor__row">
          <span className="tabular muted">{i + 1}</span>
          <label className="field">
            <span className="field__label">Tool</span>
            <select className="input" value={s.tool} onChange={(e) => setSteps((l) => l.map((x, k) => (k === i ? { ...x, tool: e.target.value } : x)))}>
              {!options.some((o) => o.name === s.tool) && <option value={s.tool}>{s.tool}</option>}
              {options.map((o) => (
                <option key={o.name} value={o.name} disabled={o.allowed_for_you === false}>
                  {o.name} ({o.risk}{o.approval !== "never" ? `, approval ${o.approval}` : ""})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">Title</span>
            <input className="input" value={s.title} onChange={(e) => setSteps((l) => l.map((x, k) => (k === i ? { ...x, title: e.target.value } : x)))} />
          </label>
          <label className="field field--wide">
            <span className="field__label">Parameters (JSON)</span>
            <textarea className="input textarea mono small" rows={2} value={s.params} onChange={(e) => setSteps((l) => l.map((x, k) => (k === i ? { ...x, params: e.target.value } : x)))} />
          </label>
          <div className="actions">
            <button type="button" className="button button--ghost button--small" aria-label="Move up" onClick={() => move(i, -1)}>↑</button>
            <button type="button" className="button button--ghost button--small" aria-label="Move down" onClick={() => move(i, 1)}>↓</button>
            <button type="button" className="button button--ghost button--small" aria-label="Remove step" onClick={() => setSteps((l) => l.filter((_, k) => k !== i))}>Remove</button>
          </div>
        </div>
      ))}
      <div className="form__actions">
        <button type="button" className="button button--ghost" onClick={() => setSteps((l) => [...l, { tool: options[0]?.name ?? "search_companies", title: "New step", params: "{}" }])}>Add step</button>
        <button type="button" className="button button--ghost" onClick={onClose}>Close</button>
        <button type="button" className="button button--primary" disabled={action.busy || steps.length === 0} onClick={save}>Save plan</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}

// --- RESULTS ---------------------------------------------------------------------------

const VIEWS = ["companies", "contacts", "jobs", "signals", "opportunities", "lists", "evidence"] as const;
type View = (typeof VIEWS)[number];

const VIEW_COLUMNS: Record<Exclude<View, "companies">, [string, string][]> = {
  contacts: [["full_name", "Name"], ["title", "Title"], ["function", "Function"], ["email", "Email"], ["email_status", "Email status"], ["source", "Source"]],
  jobs: [["title", "Title"], ["company_name", "Company"], ["location", "Location"], ["technologies", "Technologies"], ["first_seen_at", "First seen"], ["status", "Status"]],
  signals: [["signal_type", "Signal"], ["summary", "Summary"], ["confidence", "Confidence"], ["reason_codes", "Reason codes"], ["detected_at", "Detected"]],
  opportunities: [["title", "Opportunity"], ["status", "Status"], ["score", "Score"], ["signal_types", "Signals"], ["reason", "Why"]],
  lists: [["name", "List"], ["entity_type", "Of"], ["member_count", "Members"]],
  evidence: [["company", "Company"], ["step", "Step"], ["reason", "Reason"], ["source", "Source"], ["url", "Link"]],
};

function cell(value: unknown): ReactNode {
  if (typeof value === "string" && /^https?:\/\//.test(value)) {
    return <a className="link" href={value} target="_blank" rel="noreferrer noopener">open</a>;
  }
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ") || "—";
  return fmt(value);
}

function CompanyDetail({ row }: { row: CompanyResult }) {
  const scores = row.data.scores ?? {};
  return (
    <div className="cr-detail">
      <div className="cr-scorecards">
        {(["opportunity", "intent", "hiring", "account"] as const).map((key) =>
          scores[key] ? (
            <div key={key} className="cr-scorecard">
              <div className="cr-scorecard__head">
                <span className="small muted">{key} score</span>
                <Score value={scores[key].score ?? undefined} />
              </div>
              <ul className="cr-reasons">
                {scores[key].reasons.length === 0 && <li className="muted small">no contributing evidence</li>}
                {scores[key].reasons.map((r, i) => (
                  <li key={i}><span className={r.points >= 0 ? "cr-plus" : "cr-minus"}>{r.code.split(" ")[0]}</span> {r.label}</li>
                ))}
              </ul>
            </div>
          ) : null,
        )}
      </div>
      <div className="grid-2">
        <div>
          <h4>Signals</h4>
          {(row.data.signals ?? []).length === 0 ? <p className="muted small">none</p> : (
            <ul className="small">{row.data.signals!.map((s) => <li key={s.id}><Pill value={s.type} /> {s.summary}</li>)}</ul>
          )}
          <h4>Jobs</h4>
          {(row.data.jobs ?? []).length === 0 ? <p className="muted small">none</p> : (
            <ul className="small">{row.data.jobs!.map((j) => <li key={j.id}>{j.url ? <a className="link" href={j.url} target="_blank" rel="noreferrer noopener">{j.title}</a> : j.title}{j.first_seen ? ` · first seen ${fmtDate(j.first_seen)}` : ""}</li>)}</ul>
          )}
          <h4>Contact gaps</h4>
          <p className="small">{Object.entries(row.data.contact_gap ?? {}).map(([fn, st]) => `${fn.toUpperCase()}: ${st}`).join(" · ") || "not analysed"}</p>
        </div>
        <div>
          <h4>Evidence</h4>
          <ul className="small cr-evidence">
            {row.evidence.length === 0 && <li className="muted">no evidence recorded</li>}
            {row.evidence.map((e, i) => <EvidenceLine key={i} item={e} />)}
          </ul>
          <Link className="link small" to={`/companies/${row.entity_id}`}>Open company record →</Link>
        </div>
      </div>
    </div>
  );
}

function EvidenceLine({ item }: { item: EvidenceItem }) {
  const url = (item.evidence_url || item.url) as string | undefined;
  const when = item.detected_at || item.observed_at || item.first_seen;
  return (
    <li>
      <span className="muted">[{item.step ?? "source"}]</span> {item.reason}
      {item.source ? ` · ${item.source}` : ""}
      {when ? ` · ${fmtDate(when)}` : ""}
      {url ? <> · <a className="link" href={url} target="_blank" rel="noreferrer noopener">source</a></> : null}
    </li>
  );
}

function Results({ run, onNewRun }: { run: AgentRun; onNewRun: (run: AgentRun) => void }) {
  const client = useWs();
  const [view, setView] = useState<View>("companies");
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "rank", desc: false });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ action: string; params: Record<string, unknown>; label: string } | null>(null);
  const action = useAction();
  const data = useLoad(
    (signal) => client.get<{ items: Row[]; total: number }>(`/agent/runs/${run.id}/results`, { view, limit: 500 }, signal),
    `${client.base}${run.id}${view}${run.status}${run.plan.map((p) => p.status).join()}`,
  );
  const counts = run.result?.counts ?? run.progress?.counts ?? {};

  const rows = useMemo(() => {
    const items = (data.data?.items ?? []) as Row[];
    const needle = filter.trim().toLowerCase();
    const filtered = needle ? items.filter((r) => JSON.stringify(view === "companies" ? { t: r.title, d: r.data, r: r.reasons } : r).toLowerCase().includes(needle)) : items;
    const value = (r: Row) => (view === "companies" && sort.key !== "rank" && sort.key !== "score" && sort.key !== "title" ? (r.data as Record<string, unknown>)?.[sort.key] : r[sort.key]);
    return [...filtered].sort((a, b) => {
      const x = value(a) as never;
      const y = value(b) as never;
      if (x === y) return 0;
      if (x === undefined || x === null) return 1;
      if (y === undefined || y === null) return -1;
      return (x < y ? -1 : 1) * (sort.desc ? -1 : 1);
    });
  }, [data.data, filter, sort, view]);

  const sortBy = (key: string) => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key === "score" }));
  const header = (key: string, label: string) => (
    <th key={key}>
      <button type="button" className="cr-sort" onClick={() => sortBy(key)} aria-label={`Sort by ${label}`}>
        {label}{sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );

  const doAction = (name: string, params: Record<string, unknown> = {}) =>
    action.run(async () => {
      const created = await client.post<AgentRun>(`/agent/runs/${run.id}/actions`, {
        action: name, company_ids: [...selected], params,
      });
      setConfirm(null);
      setSelected(new Set());
      onNewRun(created);
    });

  const saveView = () =>
    action.run(async () => {
      const name = window.prompt("Name this view", `${view} — ${run.request.slice(0, 60)}`);
      if (!name) return;
      await client.post("/agent/saved", { kind: "view", name, request: run.request, config: { run_id: run.id, view, filter, sort } });
    });

  const all = view === "companies" ? rows.map((r) => String(r.entity_id)) : [];
  const toggleAll = () => setSelected((s) => (s.size === all.length ? new Set() : new Set(all)));

  return (
    <>
      <Tabs
        active={view}
        onChange={(v) => { setView(v as View); setExpanded(null); }}
        tabs={VIEWS.map((v) => ({ key: v, label: v[0].toUpperCase() + v.slice(1), count: v === "evidence" ? undefined : counts[v] }))}
      />
      <div className="toolbar">
        <input className="input toolbar__search" placeholder={`Filter ${view}…`} aria-label={`Filter ${view}`} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button type="button" className="button button--ghost button--small" onClick={saveView}>Save view</button>
        {view === "companies" && (
          <>
            <span className="muted small">{selected.size} selected</span>
            <button type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => setConfirm({ action: "create_list", label: "Add the selected companies to a new list", params: { name: window.prompt("List name", "Control room list") || "Control room list" } })}>Add to list</button>
            <button type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => setConfirm({ action: "create_opportunity", label: "Create one opportunity per selected company", params: {} })}>Create opportunity</button>
            <button type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => setConfirm({ action: "create_task", label: "Create a research task per selected company", params: { title: "Research follow-up" } })}>Create task</button>
            <button type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => doAction("campaign_proposal")}>Campaign proposal</button>
            <button type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => setConfirm({ action: "start_monitor", label: "Monitor the selected companies weekly", params: { frequency: "weekly" } })}>Monitor</button>
            {(["csv", "xlsx", "json"] as const).map((f) => (
              <button key={f} type="button" className="button button--ghost button--small" disabled={!selected.size} onClick={() => doAction("export", { format: f })}>{f.toUpperCase()}</button>
            ))}
          </>
        )}
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {data.error && <ErrorBanner error={data.error} onRetry={data.refresh} />}
      {data.loading && !data.data ? <Loading /> : rows.length === 0 ? (
        <p className="muted pad">No {view} in these results{filter ? " match the filter" : ""}.</p>
      ) : view === "companies" ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th><input type="checkbox" aria-label="Select all companies" checked={selected.size > 0 && selected.size === all.length} onChange={toggleAll} /></th>
                {header("rank", "#")}
                {header("title", "Company")}
                {header("score", "Score")}
                <th>Why (reason codes)</th>
                <th>Signals</th>
                <th>Contact gaps</th>
                {header("campaign", "Campaign")}
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((raw) => {
                const r = raw as unknown as CompanyResult;
                const open = expanded === r.id;
                return (
                  <Fragment key={r.id}>
                    <tr className="table__row">
                      <td><input type="checkbox" aria-label={`Select ${r.title}`} checked={selected.has(r.entity_id)} onChange={() => setSelected((s) => { const n = new Set(s); if (n.has(r.entity_id)) n.delete(r.entity_id); else n.add(r.entity_id); return n; })} /></td>
                      <td className="tabular">{r.rank}</td>
                      <td><strong>{r.title}</strong><div className="muted small">{[r.data.domain, r.data.industry, r.data.state, r.data.country].filter(Boolean).join(" · ")}</div></td>
                      <td><Score value={r.score ?? undefined} /></td>
                      <td><ReasonChips reasons={r.reasons} /></td>
                      <td className="small">{[...new Set((r.data.signals ?? []).map((s) => s.type.replace(/_/g, " ").toLowerCase()))].join(", ") || "—"}</td>
                      <td className="small">{Object.entries(r.data.contact_gap ?? {}).map(([k, v]) => `${k}:${v === "FOUND" ? "✓" : v === "MISSING" ? "✗" : "?"}`).join(" ") || "—"}</td>
                      <td className="small">{r.data.campaign?.name ?? "—"}</td>
                      <td><button type="button" className="button button--ghost button--small" aria-expanded={open} onClick={() => setExpanded(open ? null : r.id)}>{open ? "Hide" : "Why?"}</button></td>
                    </tr>
                    {open && (
                      <tr><td colSpan={9}><CompanyDetail row={r} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr>{VIEW_COLUMNS[view].map(([k, l]) => header(k, l))}</tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={String(r.id ?? i)} className="table__row">
                  {VIEW_COLUMNS[view].map(([k]) => (
                    <td key={k} className="small">
                      {k === "url" ? cell(r.url ?? r.evidence_url) : k === "status" || k === "signal_type" || k === "email_status" ? <Pill value={r[k]} /> : k === "score" ? <Score value={r[k]} /> : k === "company" && r.company_id ? <Link className="link" to={`/companies/${r.company_id}`}>{String(r.company)}</Link> : cell(r[k])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {confirm && (
        <ConfirmDialog
          title="Propose this action?"
          confirmLabel="Create proposal"
          busy={action.busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => doAction(confirm.action, confirm.params)}
        >
          <p>{confirm.label} ({selected.size} selected).</p>
          <p className="muted small">This creates a new run. Anything that changes CRM data will still wait in its ACTIONS panel for your approval.</p>
        </ConfirmDialog>
      )}
    </>
  );
}

// --- ACTIONS (approvals) -------------------------------------------------------------------

function Approvals({ run, onChange }: { run: AgentRun; onChange: (run: AgentRun) => void }) {
  const client = useWs();
  const [pending, setPending] = useState<{ approval: Approval; decision: "approve" | "reject" } | null>(null);
  const action = useAction();
  const approvals = run.approvals ?? [];
  const open = approvals.filter((a) => a.status === "pending");
  const history = approvals.filter((a) => a.status !== "pending");
  // A synchronous lock: a second click (or a double-fired event) before React has
  // re-rendered the busy state must never send a second decision.
  const sending = useRef(false);
  const decide = () => {
    if (!pending || sending.current) return;
    sending.current = true;
    void action
      .run(async () => {
        const updated = await client.post<AgentRun>(`/agent/approvals/${pending.approval.id}/${pending.decision}`);
        setPending(null);
        onChange(updated);
        return true;
      })
      .then(async (ok) => {
        // On a refusal (e.g. credits not available, or the approved step failed), show the
        // run's real state: the approval may be recorded even though its step failed.
        if (!ok) {
          try {
            onChange(await client.get<AgentRun>(`/agent/runs/${run.id}`));
          } catch {
            // the error banner already explains what went wrong
          }
        }
      })
      .finally(() => {
        sending.current = false;
      });
  };
  return (
    <>
      {open.length === 0 ? <p className="muted small">Nothing is waiting for approval.</p> : (
        <div className="cr-approvals">
          {open.map((a) => {
            const credits = creditsText(a.credits);
            return (
              <article key={a.id} className="cr-approval" aria-label={`Proposed action: ${a.action}`}>
                <div className="small muted">PROPOSED ACTION</div>
                <strong>{a.action}</strong>
                <div className="chips"><RiskPill risk={a.risk} />{a.impact?.affected ? <span className="chip">{a.impact.affected.toLocaleString()} affected</span> : null}{credits && <span className="chip chip--warn">{credits} credits</span>}</div>
                {a.reason && <p className="small">{a.reason}</p>}
                <div className="actions">
                  <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => { action.clear(); setPending({ approval: a, decision: "approve" }); }}>Approve</button>
                  <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => { action.clear(); setPending({ approval: a, decision: "reject" }); }}>Reject</button>
                </div>
              </article>
            );
          })}
        </div>
      )}
      {history.length > 0 && (
        <details className="small cr-history">
          <summary>Decision history ({history.length})</summary>
          <ul>{history.map((a) => <li key={a.id}><Pill value={a.status} /> {a.action}{a.decided_at ? ` · ${fmt(a.decided_at)}` : ""}</li>)}</ul>
        </details>
      )}
      {action.error && <ErrorBanner error={action.error} />}
      {pending && (
        <ConfirmDialog
          title={pending.decision === "approve" ? "Approve this action?" : "Reject this action?"}
          confirmLabel={pending.decision === "approve" ? "Approve and run" : "Reject"}
          danger={pending.decision === "reject"}
          busy={action.busy}
          onCancel={() => setPending(null)}
          onConfirm={decide}
        >
          <p><strong>{pending.approval.action}</strong></p>
          <p className="small">Risk: {pending.approval.risk}. Records affected: {pending.approval.impact?.affected ?? "—"}.</p>
          <p className={creditsText(pending.approval.credits) ? "small cr-warn" : "small"}>
            Credits: {creditsText(pending.approval.credits) || "none"}
            {creditsText(pending.approval.credits) ? " (held now, released if unused or if the step fails)" : ""}
          </p>
          {pending.approval.reason && <p className="muted small">{pending.approval.reason}</p>}
          {action.error && <ErrorBanner error={action.error} />}
        </ConfirmDialog>
      )}
    </>
  );
}

// --- execution history (admins) -------------------------------------------------------------

function Trail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const client = useWs();
  const trail = useLoad((signal) => client.get<Record<string, unknown>>(`/agent/runs/${runId}/trail`, undefined, signal), client.base + "trail" + runId);
  return (
    <div className="cr-drawer" role="dialog" aria-modal="true" aria-label="Execution history">
      <div className="title-row">
        <h3>Execution history</h3>
        <button type="button" className="button button--ghost button--small" onClick={onClose}>Close</button>
      </div>
      {trail.error && <ErrorBanner error={trail.error} />}
      {!trail.data ? <Loading /> : (
        <>
          <h4>Tool calls (parameters redacted)</h4>
          <ol className="small">
            {((trail.data.steps as Row[]) ?? []).map((s) => (
              <li key={s.id}>
                <strong>{String(s.tool)}</strong> <Pill value={s.status} /> <RiskPill risk={String(s.risk)} />
                {s.duration_ms ? ` · ${Math.round(Number(s.duration_ms))} ms` : ""}
                {s.error ? <div className="cr-bad">{String(s.error)}</div> : null}
                <details><summary>parameters & output</summary><Json value={{ params: s.params, output: s.output }} /></details>
              </li>
            ))}
          </ol>
          <h4>Approvals</h4>
          <Json value={trail.data.approvals} />
          <h4>Audit log</h4>
          <ul className="small">
            {((trail.data.audit as Row[]) ?? []).map((a) => <li key={a.id}>{fmt(a.created_at)} · <code>{String(a.action)}</code> · {String(a.summary ?? "")}</li>)}
          </ul>
          <h4>Credit ledger</h4>
          <Json value={trail.data.credits} />
        </>
      )}
    </div>
  );
}

// --- the canvas ------------------------------------------------------------------------------

export function ResearchCanvas({ runId, onRunChange }: { runId: string; onRunChange?: (run: AgentRun) => void }) {
  const client = useWs();
  const { current } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [live, setLive] = useState<AgentRun | null>(null);
  const [editing, setEditing] = useState(false);
  const [showTrail, setShowTrail] = useState(false);
  const action = useAction();
  const polling = live && !TERMINAL.includes(live.status) && live.status !== "planned" && live.status !== "awaiting_approval";
  const loaded = useLoad((signal) => client.get<AgentRun>(`/agent/runs/${runId}`, undefined, signal), `${client.base}${runId}:${tick}`, polling ? 2500 : undefined);
  useEffect(() => {
    if (loaded.data) setLive(loaded.data);
  }, [loaded.data]);
  useEffect(() => setLive(null), [runId]);
  const sources = useLoad(
    (signal) => client.get<{ items: Row[] }>(`/agent/runs/${runId}/results`, { view: "sources" }, signal),
    `${client.base}${runId}:sources:${live?.status}:${live?.plan.map((p) => p.status).join()}`,
  );

  const update = (run: AgentRun) => {
    setLive(run);
    onRunChange?.(run);
    setTick((t) => t + 1);
  };

  if (loaded.error && !live) return <ErrorBanner error={loaded.error} onRetry={loaded.refresh} />;
  if (!live) return <Loading label="Loading the run…" />;
  const run = live;
  const isAdmin = ["owner", "admin"].includes(current?.role ?? "");
  const lines = run.progress?.lines ?? [];
  const pending = (run.approvals ?? []).filter((a) => a.status === "pending");
  const used = creditsText(run.result?.credits_used);

  return (
    <div className="cr-canvas" aria-live="polite">
      <Section id="request" title="Request" actions={<><Pill value={run.status} />{isAdmin && <button type="button" className="button button--ghost button--small" onClick={() => setShowTrail(true)}>Execution history</button>}</>}>
        <p className="cr-request">{run.request}</p>
        <p className="muted small">
          Mode: {run.mode} · planner: {run.planner ?? "rules"} · {fmt(run.created_at)}
          {(run.intent?.aliases_applied ?? []).map((a) => ` · memory: “${a.alias}” → ${a.expands_to.join(", ")}`).join("")}
        </p>
        {(run.intent?.notes ?? []).length > 0 && <ul className="small muted">{run.intent!.notes!.map((n, i) => <li key={i}>{n}</li>)}</ul>}
      </Section>

      <Section
        id="plan"
        title="Plan"
        actions={
          <>
            {run.status === "planned" && <button type="button" className="button button--ghost button--small" onClick={() => setEditing((e) => !e)}>{editing ? "Close editor" : "Edit plan"}</button>}
            {["planned", "failed"].includes(run.status) && (
              <button type="button" className="button button--primary button--small" disabled={action.busy} onClick={() => action.run(async () => update(await client.post<AgentRun>(`/agent/runs/${run.id}/run`)))}>Approve &amp; Run</button>
            )}
            {!TERMINAL.includes(run.status) && (
              <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => update(await client.post<AgentRun>(`/agent/runs/${run.id}/cancel`)))}>Cancel</button>
            )}
          </>
        }
      >
        {action.error && <ErrorBanner error={action.error} />}
        {editing ? <PlanEditor run={run} onClose={() => setEditing(false)} onSaved={(r) => { setEditing(false); update(r); }} /> : <PlanCards plan={run.plan} />}
        <EstimatePanel run={run} />
      </Section>

      <div className="grid-2">
        <Section id="progress" title="Live progress">
          {lines.length === 0 ? <p className="muted small">{run.status === "planned" ? "Nothing has run yet. Review the plan, then Approve & Run." : run.progress?.message}</p> : (
            <ul className="cr-progress">
              {lines.map((l, i) => <li key={i}><span className={`cr-icon cr-icon--${l.ok === false ? "wait" : "ok"}`}>{l.ok === false ? "⏳" : "✓"}</span> {l.text}</li>)}
            </ul>
          )}
          {run.progress?.counts && <p className="small muted">{Object.entries(run.progress.counts).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>}
        </Section>
        <Section id="sources" title="Sources">
          {!sources.data ? <Loading /> : (
            <ul className="small cr-sources">
              {sources.data.items.map((s, i) => (
                <li key={i}><StepIcon status={String(s.status)} /> <code>{String(s.tool)}</code> <RiskPill risk={String(s.risk)} /> {s.detail ? String(s.detail) : ""}{creditsText(s.credits as Record<string, number>) ? <span className="cr-warn"> · {creditsText(s.credits as Record<string, number>)} credits</span> : ""}</li>
              ))}
            </ul>
          )}
        </Section>
      </div>

      <Section id="actions" title={`Actions${pending.length ? ` (${pending.length} awaiting approval)` : ""}`}>
        <Approvals run={run} onChange={update} />
      </Section>

      {(run.summary || TERMINAL.includes(run.status) || run.status === "awaiting_approval") && (
        <Section id="summary" title="What happened">
          <dl className="details">
            <dt>What it did</dt><dd>{run.plan.filter((p) => p.status === "done").map((p) => p.title || p.tool).join(" → ") || "—"}</dd>
            <dt>Sources used</dt><dd>{[...new Set(run.plan.filter((p) => p.status === "done").map((p) => p.tool))].join(", ") || "—"}</dd>
            <dt>What it cost</dt><dd className={used ? "cr-warn" : ""}>{used || "no paid credits"}</dd>
            <dt>What it found</dt><dd>{run.summary ?? "—"}</dd>
            <dt>Still needs approval</dt><dd>{pending.length ? pending.map((a) => a.action).join("; ") : "nothing"}</dd>
          </dl>
          {run.error && <p className="cr-bad small">{run.error}</p>}
        </Section>
      )}

      {run.status !== "planned" && (
        <Section id="results" title="Results & evidence">
          <Results run={run} onNewRun={update} />
        </Section>
      )}

      {showTrail && <Trail runId={run.id} onClose={() => setShowTrail(false)} />}
    </div>
  );
}
