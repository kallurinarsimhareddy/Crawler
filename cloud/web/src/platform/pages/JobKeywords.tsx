// Relevance keywords: the keyword workbook that scores every job (HIGH / REVIEW /
// REJECT). Upload a workbook, activate a set, try it on a title + description,
// and re-score the jobs already stored.

import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { display } from "../logic/jobFields";
import { PageHeader, Pill, fmt, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";
import { Chips, Relevance, uploadOne } from "./jobsShared";

type KeywordSet = Row & {
  name?: string | null;
  filename?: string | null;
  active?: boolean;
  keyword_count?: number | null;
  categories?: unknown[] | null;
  problems?: unknown[] | null;
  thresholds?: Record<string, unknown> | null;
  created_at?: string | null;
};

interface ScoreResult {
  relevance_score?: number | null;
  relevance_class?: string | null;
  matched_keywords?: string[] | null;
  matched_categories?: string[] | null;
  relevance_reason?: string | null;
}

/** A category / problem entry as text: a string, or {name|category|message, count?}. */
function itemText(item: unknown): string {
  if (typeof item === "string" || typeof item === "number") return String(item).trim();
  if (item && typeof item === "object") {
    const o = item as Record<string, unknown>;
    const label = display(o.name ?? o.category ?? o.message ?? o.problem ?? o.text);
    const where = display(o.row ?? o.sheet) ? ` (${[o.sheet, o.row !== undefined ? `row ${display(o.row)}` : ""].map(display).filter(Boolean).join(", ")})` : "";
    const count = typeof o.count === "number" ? ` · ${o.count.toLocaleString()}` : typeof o.keyword_count === "number" ? ` · ${o.keyword_count.toLocaleString()}` : "";
    return label ? `${label}${count}${where}` : JSON.stringify(item);
  }
  return "";
}

function thresholdsText(t: KeywordSet["thresholds"]): string {
  if (!t || typeof t !== "object") return "";
  return Object.entries(t).map(([k, v]) => `${k.replace(/_/g, " ")}: ${display(v) || JSON.stringify(v)}`).join(" · ");
}

export function JobKeywordsPage() {
  const client = useWs();
  const [reload, setReload] = useState(0);
  const sets = useLoad((signal) => client.get<{ items: KeywordSet[]; total: number }>("/job-keyword-sets", undefined, signal), `${client.base}|keyword-sets|${reload}`);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [inputKey, setInputKey] = useState(0);
  const upload = useAction();
  const act = useAction();
  const [rescored, setRescored] = useState<number | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    void upload.run(async () => {
      await uploadOne(client, "/job-keyword-sets", file, name.trim() ? { name: name.trim() } : {});
      setFile(null);
      setName("");
      setInputKey((k) => k + 1);
      setReload((r) => r + 1);
    });
  };
  const activate = (id: string) =>
    act.run(async () => {
      await client.post(`/job-keyword-sets/${encodeURIComponent(id)}/activate`);
      setReload((r) => r + 1);
    });
  const rescore = () =>
    act.run(async () => {
      const result = await client.post<{ rescored?: number }>("/job-relevance/rescore");
      setRescored(typeof result?.rescored === "number" ? result.rescored : 0);
    });

  const items = sets.data?.items ?? [];
  const hasActive = items.some((s) => s.active);

  return (
    <div className="page">
      <Link to="/jobs" className="back">← Jobs</Link>
      <PageHeader
        title="Relevance keywords"
        crumbTitle="Relevance keywords"
        subtitle="The keyword workbook that scores each job 0–100 and classes it HIGH, REVIEW or REJECT. Only the active set is used."
        actions={
          <button type="button" className="button button--ghost" disabled={act.busy || !hasActive} onClick={() => void rescore()} title={hasActive ? undefined : "Activate a keyword set first"}>
            {act.busy ? "Working…" : "Re-score stored jobs"}
          </button>
        }
      />
      {rescored !== null && (
        <p className="alert alert--info" role="status">
          <span>Re-scored {rescored.toLocaleString()} stored job{rescored === 1 ? "" : "s"} with the active keyword set.</span>
          <Link className="link" to="/jobs?order=-relevance_score">See the most relevant jobs</Link>
        </p>
      )}
      {act.error && <ErrorBanner error={act.error} />}

      <form className="card pad form" onSubmit={submit}>
        <h3>Upload a keyword workbook</h3>
        <div className="form-grid">
          <label className="field field--wide">
            <span className="field__label">Workbook (XLSX or CSV)</span>
            <input key={inputKey} className="input" type="file" accept=".xlsx,.xls,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          <label className="field">
            <span className="field__label">Name (optional)</span>
            <input className="input" value={name} placeholder="From the file name" onChange={(e) => setName(e.target.value)} />
          </label>
        </div>
        {upload.error && <ErrorBanner error={upload.error} />}
        <div className="form__actions">
          <button type="submit" className="button button--primary" disabled={upload.busy || !file}>{upload.busy ? "Uploading…" : "Upload"}</button>
        </div>
      </form>

      <div className="card pad">
        <div className="title-row"><h3>Keyword sets</h3>{sets.data ? <span className="muted small">{sets.data.total.toLocaleString()} total</span> : null}</div>
        {sets.error && <ErrorBanner error={sets.error} onRetry={sets.refresh} />}
        {sets.loading && !sets.data ? <Loading /> : items.length === 0 ? (
          <EmptyState icon="upload" title="No keyword sets yet" description="Upload a keyword workbook to start scoring jobs." />
        ) : (
          <div className="jm-sets">
            {items.map((set) => {
              const categories = (set.categories ?? []).map(itemText).filter(Boolean);
              const problems = (set.problems ?? []).map(itemText).filter(Boolean);
              const thresholds = thresholdsText(set.thresholds);
              return (
                <section key={set.id} className={`jm-set${set.active ? " jm-set--active" : ""}`} aria-label={display(set.name) || set.id}>
                  <div className="jm-set__head">
                    <div className="min-w-0">
                      <strong>{display(set.name) || display(set.filename) || set.id}</strong>{" "}
                      {set.active ? <Pill value="active" /> : null}
                      <div className="muted small">
                        {[display(set.filename), typeof set.keyword_count === "number" ? `${set.keyword_count.toLocaleString()} keywords` : "", set.created_at ? `uploaded ${fmt(set.created_at)}` : ""].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                    {!set.active && (
                      <button type="button" className="button button--primary button--small" disabled={act.busy} onClick={() => void activate(set.id)}>Activate</button>
                    )}
                  </div>
                  <div className="small"><span className="muted">Categories: </span>{categories.length ? <Chips values={categories} /> : <span className="muted">—</span>}</div>
                  {thresholds && <div className="small"><span className="muted">Thresholds: </span>{thresholds}</div>}
                  {problems.length > 0 && (
                    <div className="small">
                      <span className="jm-problem">{problems.length} problem{problems.length === 1 ? "" : "s"} in the workbook:</span>
                      <ul className="jm-examples">{problems.slice(0, 20).map((p, i) => <li key={i}>{p}</li>)}</ul>
                      {problems.length > 20 && <span className="muted">…and {problems.length - 20} more</span>}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        )}
      </div>

      <TryIt hasActive={hasActive} />
    </div>
  );
}

function TryIt({ hasActive }: { hasActive: boolean }) {
  const client = useWs();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [term, setTerm] = useState("");
  const [result, setResult] = useState<ScoreResult | null>(null);
  const action = useAction();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim() && !description.trim()) return;
    void action.run(async () => {
      const body: Record<string, unknown> = { title: title.trim(), description };
      if (term.trim()) body.search_term = term.trim();
      setResult(await client.post<ScoreResult>("/job-relevance/score", body));
    });
  };
  return (
    <form className="card pad form jm-try" onSubmit={submit}>
      <h3>Try it</h3>
      <p className="muted small">Score a title and description with the active keyword set. Nothing is stored.{hasActive ? "" : " No set is active yet."}</p>
      <div className="form-grid">
        <label className="field">
          <span className="field__label">Job title</span>
          <input className="input" value={title} placeholder="SAP MM Consultant" onChange={(e) => { setTitle(e.target.value); setResult(null); }} />
        </label>
        <label className="field">
          <span className="field__label">Search term (optional)</span>
          <input className="input" value={term} placeholder="SAP" onChange={(e) => { setTerm(e.target.value); setResult(null); }} />
        </label>
        <label className="field field--wide">
          <span className="field__label">Description</span>
          <textarea className="input textarea" rows={5} value={description} onChange={(e) => { setDescription(e.target.value); setResult(null); }} />
        </label>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button type="submit" className="button button--primary" disabled={action.busy || (!title.trim() && !description.trim())}>{action.busy ? "Scoring…" : "Score"}</button>
      </div>
      {result && (
        <div className="jm-plan">
          <p><Relevance score={result.relevance_score} cls={result.relevance_class} /></p>
          {display(result.relevance_reason) && <p className="small jm-reason">{String(result.relevance_reason)}</p>}
          <div className="small"><span className="muted">Matched keywords: </span><Chips values={result.matched_keywords} /></div>
          <div className="small"><span className="muted">Matched categories: </span><Chips values={result.matched_categories} /></div>
        </div>
      )}
    </form>
  );
}
