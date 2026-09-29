// Score explanations: every score as its factors (weight, observed value, points,
// reason), the evidence records behind it, when it was computed and by which
// rule set. Nothing here is an opaque AI score.

import { useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading } from "../../components/Feedback";
import { factorFill, formatCell, stagePosition, type ScoreFactor } from "../logic/reports";
import { Pill, Score, fmt, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import "../styles/analytics.css";

interface Evidence {
  type: string;
  id: string | null;
  summary: string;
  observed_at: string | null;
}

interface ScorePart {
  score: number;
  label: string | null;
  factors: ScoreFactor[];
  evidence: Evidence[];
  computed_at: string;
  model: string;
}

interface Explanation {
  entity_type: "company" | "contact";
  entity_id: string;
  model: string;
  computed_at: string;
  persisted: boolean;
  last_saved_at: string | null;
  how: Record<string, string>;
  scores: Record<string, ScorePart>;
}

const KIND_LABELS: Record<string, string> = {
  account: "Account fit",
  hiring: "Hiring",
  technology: "Technology",
  opportunity: "Opportunity",
  buying_stage: "Buying stage",
  contact: "Contact",
};

const EVIDENCE_LINKS: Record<string, (id: string) => string> = {
  opportunity: (id) => `/opportunities/${id}`,
  contact: (id) => `/contacts/${id}`,
  company: (id) => `/companies/${id}`,
};

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value as Record<string, unknown>).map(([k, v]) => `${k.replace(/_/g, " ")}: ${fmt(v)}`).join(", ");
  }
  return formatCell("value", value);
}

function FactorTable({ factors }: { factors: ScoreFactor[] }) {
  return (
    <div className="table-wrap">
      <table className="table score-factors">
        <thead>
          <tr>
            <th>Factor</th>
            <th>Observed</th>
            <th className="tabular">Points</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {factors.map((f) => (
            <tr key={f.name}>
              <td className="nowrap">{f.name.replace(/_/g, " ")}</td>
              <td className="small">{valueText(f.value)}</td>
              <td className="tabular nowrap">
                <span className="factor-bar" title={`${f.points} of ${f.weight}`}>
                  <span className="factor-bar__fill" style={{ width: `${factorFill(f)}%` }} />
                </span>
                {f.points} / {f.weight}
              </td>
              <td className="small muted">{f.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return <p className="muted small">No supporting records — the factors above explain the score from missing data.</p>;
  return (
    <ul className="evidence-list">
      {evidence.map((e, i) => {
        const link = e.id && EVIDENCE_LINKS[e.type] ? EVIDENCE_LINKS[e.type](e.id) : null;
        return (
          <li key={`${e.type}${e.id ?? i}`}>
            <Pill value={e.type} />
            <span className="evidence-list__text">{link ? <Link className="link" to={link}>{e.summary}</Link> : e.summary}</span>
            {e.observed_at && <span className="muted small nowrap">{new Date(e.observed_at).toLocaleDateString()}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function History({ entityType, entityId, kind }: { entityType: string; entityId: string; kind: string }) {
  const client = useWs();
  const { data, error, loading } = useLoad(
    (s) => client.get<{ items: Record<string, unknown>[] }>(`/scores/${entityType}/${entityId}/history`, { kind, limit: 10 }, s),
    client.base + entityType + entityId + kind + "history",
  );
  if (error) return <ErrorBanner error={error} />;
  if (loading || !data) return <Loading />;
  if (!data.items.length) return <p className="muted small">No saved history yet. Recompute to save a snapshot; later changes are recorded automatically.</p>;
  return (
    <ul className="score-history">
      {data.items.map((h) => (
        <li key={String(h.id)}>
          <Score value={h.score} />
          {h.label ? <Pill value={h.label} /> : null}
          <span className="muted small">{new Date(String(h.computed_at)).toLocaleString()} · {String(h.model)}</span>
        </li>
      ))}
    </ul>
  );
}

/** The explanation for one company or contact, with recompute and history. */
export function ScorePanel({ entityType, entityId, onRecomputed }: { entityType: "company" | "contact"; entityId: string; onRecomputed?: () => void }) {
  const client = useWs();
  const { current } = useWorkspace();
  const canWrite = current ? current.role !== "viewer" : false;
  const [kind, setKind] = useState(entityType === "company" ? "opportunity" : "contact");
  const [showHistory, setShowHistory] = useState(false);
  const action = useAction();
  const { data, error, loading, refresh } = useLoad(
    (s) => client.get<Explanation>(`/scores/${entityType}/${entityId}`, undefined, s),
    client.base + entityType + entityId + "explain",
  );
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (loading || !data) return <Loading label="Explaining scores…" />;
  const kinds = Object.keys(data.scores);
  if (!kinds.length) return <EmptyState icon="chart" title="No scores" description="This record has nothing to score yet." />;
  const part = data.scores[kind] ?? data.scores[kinds[0]];
  const active = data.scores[kind] ? kind : kinds[0];
  return (
    <div className="score-panel">
      <div className="score-panel__head">
        <div>
          <h3>Why these scores</h3>
          <p className="muted small">
            Arithmetic over stored records ({data.model}). Computed {new Date(data.computed_at).toLocaleString()}
            {data.last_saved_at ? ` · last saved ${new Date(data.last_saved_at).toLocaleString()}` : " · not saved yet"}.
          </p>
        </div>
        <div className="actions">
          <button type="button" className="button button--ghost button--small" onClick={() => setShowHistory((v) => !v)}>
            {showHistory ? "Hide history" : "History"}
          </button>
          {canWrite && (
            <button
              type="button"
              className="button button--ghost button--small"
              disabled={action.busy}
              onClick={() => action.run(async () => {
                await client.post(`/scores/${entityType}/${entityId}`);
                refresh();
                onRecomputed?.();
              })}
            >
              Recompute and save
            </button>
          )}
        </div>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {kinds.length > 1 && (
        <div className="score-cards">
          {kinds.map((k) => (
            <button key={k} type="button" className={`score-card${k === active ? " score-card--active" : ""}`} onClick={() => setKind(k)} aria-pressed={k === active}>
              <span className="score-card__label">{KIND_LABELS[k] ?? k}</span>
              <Score value={data.scores[k].score} />
              {k === "buying_stage" && data.scores[k].label && (
                <span className="small muted">{String(data.scores[k].label).replace(/_/g, " ")} · stage {stagePosition(data.scores[k].label)} of 6</span>
              )}
            </button>
          ))}
        </div>
      )}
      <p className="small muted score-panel__rule">{data.how[active] ?? ""}</p>
      <h4 className="score-panel__sub">{KIND_LABELS[active] ?? active}: {part.score} / 100{part.label ? ` · ${part.label.replace(/_/g, " ")}` : ""}</h4>
      <FactorTable factors={part.factors} />
      <h4 className="score-panel__sub">Evidence</h4>
      <EvidenceList evidence={part.evidence} />
      {showHistory && (
        <>
          <h4 className="score-panel__sub">History</h4>
          <History entityType={entityType} entityId={entityId} kind={active} />
        </>
      )}
    </div>
  );
}
