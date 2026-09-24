// Settings → AI provider (per-workspace provider, model, explicit fallbacks) and
// proactive insight rules. API keys never pass through the browser.

import { useEffect, useState } from "react";
import { ErrorBanner, Loading } from "../../components/Feedback";
import { Json, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";

interface AiConfig {
  workspace: { provider?: string | null; model?: string | null; fallbacks: { provider: string; model?: string | null }[] };
  platform_default: string;
  in_use: Record<string, unknown>;
  external_allowed: boolean;
  providers: string[];
}

interface Proactive {
  enabled: boolean;
  window_days: number;
  rules: Record<string, { enabled: boolean; min_companies: number; contains?: string }>;
}

const RULE_LABELS: Record<string, string> = {
  hiring_spikes: "Hiring spikes",
  erp_implementation: "ERP implementation hiring at accounts",
  missing_it_leadership: "Accounts with no IT leadership contact",
  ats_changes: "ATS / careers platform changes",
  technology_changes: "Technology changes",
};

export function AiProviderSettings() {
  const client = useWs();
  const { current } = useWorkspace();
  const admin = ["owner", "admin"].includes(current?.role ?? "");
  const config = useLoad((s) => client.get<AiConfig>("/agent/ai-config", undefined, s), client.base + "aicfg");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [fallbacks, setFallbacks] = useState<{ provider: string; model: string }[]>([]);
  const action = useAction();
  useEffect(() => {
    if (!config.data) return;
    setProvider(config.data.workspace.provider ?? "");
    setModel(config.data.workspace.model ?? "");
    setFallbacks(config.data.workspace.fallbacks.map((f) => ({ provider: f.provider, model: f.model ?? "" })));
  }, [config.data]);
  if (config.error) return <ErrorBanner error={config.error} />;
  if (!config.data) return <Loading />;
  const options = config.data.providers;
  const save = () =>
    action.run(async () => {
      await client.put("/agent/ai-config", {
        provider: provider || null, model: model || null,
        fallbacks: fallbacks.filter((f) => f.provider).map((f) => ({ provider: f.provider, model: f.model || null })),
      });
      config.refresh();
    });
  return (
    <div className="card pad">
      <h3>AI provider</h3>
      <p className="muted small">
        Choose the model this workspace uses for planning and extraction. API keys stay on the server (environment
        variables) and are never sent to the browser. Fallback providers are used only if listed here, and only when
        the primary is unavailable. External providers are used only when “Allow external AI providers” above is on.
      </p>
      <div className="form-grid">
        <label className="field">
          <span className="field__label">Provider</span>
          <select className="input" value={provider} disabled={!admin} onChange={(e) => setProvider(e.target.value)}>
            <option value="">Platform default ({config.data.platform_default})</option>
            {options.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Model (optional)</span>
          <input className="input" value={model} disabled={!admin} onChange={(e) => setModel(e.target.value)} placeholder="provider default" />
        </label>
      </div>
      <h4>Fallbacks (in order)</h4>
      {fallbacks.length === 0 && <p className="muted small">None — if the provider is unavailable, deterministic rules are used.</p>}
      {fallbacks.map((f, i) => (
        <div key={i} className="field-row">
          <select className="input" aria-label={`Fallback ${i + 1} provider`} value={f.provider} disabled={!admin} onChange={(e) => setFallbacks((l) => l.map((x, k) => (k === i ? { ...x, provider: e.target.value } : x)))}>
            {options.filter((p) => p !== "rules").map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input className="input" aria-label={`Fallback ${i + 1} model`} value={f.model} disabled={!admin} placeholder="model (optional)" onChange={(e) => setFallbacks((l) => l.map((x, k) => (k === i ? { ...x, model: e.target.value } : x)))} />
          <button type="button" className="button button--ghost button--small" disabled={!admin} onClick={() => setFallbacks((l) => l.filter((_, k) => k !== i))}>Remove</button>
        </div>
      ))}
      <div className="form__actions">
        <button type="button" className="button button--ghost button--small" disabled={!admin} onClick={() => setFallbacks((l) => [...l, { provider: "claude", model: "" }])}>Add fallback</button>
        <button type="button" className="button button--primary" disabled={!admin || action.busy} onClick={save}>Save AI provider</button>
      </div>
      {!admin && <p className="muted small">Only workspace admins can change the AI provider.</p>}
      {action.error && <ErrorBanner error={action.error} />}
      <details className="small"><summary>What is in use now</summary><Json value={config.data.in_use} /></details>
    </div>
  );
}

export function ProactiveSettings() {
  const client = useWs();
  const { current } = useWorkspace();
  const admin = ["owner", "admin"].includes(current?.role ?? "");
  const settings = useLoad((s) => client.get<Proactive>("/agent/insights/settings", undefined, s), client.base + "proactive");
  const [draft, setDraft] = useState<Proactive | null>(null);
  const action = useAction();
  useEffect(() => {
    if (settings.data) setDraft(settings.data);
  }, [settings.data]);
  if (settings.error) return <ErrorBanner error={settings.error} />;
  if (!draft) return <Loading />;
  return (
    <div className="card pad">
      <h3>Proactive AI</h3>
      <p className="muted small">CareerCrawler AI checks your data hourly and surfaces meaningful changes in the Control Room. Insights never change CRM data.</p>
      <label className="check">
        <input type="checkbox" checked={draft.enabled} disabled={!admin} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        Notifications on
      </label>
      <label className="field">
        <span className="field__label">Look-back window (days)</span>
        <input className="input" type="number" min={1} max={90} value={draft.window_days} disabled={!admin} onChange={(e) => setDraft({ ...draft, window_days: Number(e.target.value) || 7 })} />
      </label>
      <table className="table">
        <thead><tr><th>Rule</th><th>On</th><th>Minimum companies</th></tr></thead>
        <tbody>
          {Object.entries(draft.rules).map(([key, rule]) => (
            <tr key={key}>
              <td>{RULE_LABELS[key] ?? key}</td>
              <td><input type="checkbox" aria-label={`${RULE_LABELS[key] ?? key} enabled`} checked={rule.enabled} disabled={!admin} onChange={(e) => setDraft({ ...draft, rules: { ...draft.rules, [key]: { ...rule, enabled: e.target.checked } } })} /></td>
              <td><input className="input input--small" type="number" min={1} aria-label={`${RULE_LABELS[key] ?? key} threshold`} value={rule.min_companies} disabled={!admin} onChange={(e) => setDraft({ ...draft, rules: { ...draft.rules, [key]: { ...rule, min_companies: Number(e.target.value) || 1 } } })} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="form__actions">
        <button type="button" className="button button--primary" disabled={!admin || action.busy} onClick={() => action.run(async () => { await client.put("/agent/insights/settings", draft); settings.refresh(); })}>Save proactive settings</button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
    </div>
  );
}
