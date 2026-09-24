// Settings → AI (per-workspace provider, model, enabled, budget, fallbacks, allowed actions,
// write-only keys, explicit live test, usage and cost) and proactive insight rules.
// A saved key is never sent back to the browser: the API returns only whether one exists.

import { useEffect, useState } from "react";
import { ErrorBanner, Loading } from "../../components/Feedback";
import { DataTable, Json, Pill, fmt, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";

interface AiStatus {
  configured: boolean;
  provider: string;
  model: string | null;
  enabled: boolean;
  key_present: boolean;
  key_source: "workspace" | "server" | null;
  external_allowed: boolean;
  active: boolean;
  reason: string | null;
  max_budget_usd: number | null;
  spent_this_month_usd: number;
  allowed_actions: string[];
  actions: Record<string, string>;
  providers: Record<string, { label: string; default_model: string | null; key_present: boolean }>;
}

interface AiConfig {
  workspace: {
    provider?: string | null;
    model?: string | null;
    enabled: boolean;
    max_budget_usd: number | null;
    allowed_actions: string[];
    fallbacks: { provider: string; model?: string | null }[];
  };
  status: AiStatus;
  platform_default: string;
  in_use: Record<string, unknown>;
  external_allowed: boolean;
  providers: string[];
}

interface Usage {
  recent: { id: string; created_at: string; provider: string; model: string; purpose: string; success: boolean; prompt_tokens: number | null; completion_tokens: number | null; estimated_cost_usd: number | null; request_id: string | null }[];
  totals: Record<string, { calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number }>;
  spent_this_month_usd: number;
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

function StatusBanner({ status }: { status: AiStatus }) {
  if (!status.configured) {
    return <div className="alert alert--info" role="status"><strong>AI provider not configured.</strong>&nbsp;The Control Room plans with deterministic rules. Choose a provider and save a key to use a real model.</div>;
  }
  if (status.active) {
    return <div className="alert alert--info" role="status">AI active: <strong>{status.provider}</strong>{status.model ? ` · ${status.model}` : ""} (key from {status.key_source === "workspace" ? "this workspace" : "the server"}).</div>;
  }
  return <div className="alert alert--info" role="status"><strong>AI not in use:</strong>&nbsp;{status.reason ?? "unavailable"}. Rules are used instead.</div>;
}

export function AiProviderSettings() {
  const client = useWs();
  const { current } = useWorkspace();
  const admin = ["owner", "admin"].includes(current?.role ?? "");
  const config = useLoad((s) => client.get<AiConfig>("/agent/ai-config", undefined, s), client.base + "aicfg");
  const usage = useLoad((s) => client.get<Usage>("/agent/ai/usage", { limit: 25 }, s), client.base + "aiusage");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [budget, setBudget] = useState("");
  const [actions, setActions] = useState<string[]>([]);
  const [fallbacks, setFallbacks] = useState<{ provider: string; model: string }[]>([]);
  const [keyProvider, setKeyProvider] = useState("claude");
  const [keyValue, setKeyValue] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [savedHint, setSavedHint] = useState<string | null>(null);
  const [test, setTest] = useState<Record<string, unknown> | null>(null);
  const action = useAction();

  useEffect(() => {
    if (!config.data) return;
    const w = config.data.workspace;
    setProvider(w.provider ?? "");
    setModel(w.model ?? "");
    setEnabled(w.enabled !== false);
    setBudget(w.max_budget_usd === null || w.max_budget_usd === undefined ? "" : String(w.max_budget_usd));
    setActions(w.allowed_actions ?? []);
    setFallbacks(w.fallbacks.map((f) => ({ provider: f.provider, model: f.model ?? "" })));
    if (w.provider && w.provider !== "rules") setKeyProvider(w.provider);
  }, [config.data]);

  if (config.error) return <ErrorBanner error={config.error} />;
  if (!config.data) return <Loading />;
  const status = config.data.status;
  const realProviders = config.data.providers.filter((p) => p !== "rules");

  const save = () =>
    action.run(async () => {
      await client.put("/agent/ai-config", {
        provider: provider || null,
        model: model || null,
        enabled,
        max_budget_usd: budget === "" ? null : Number(budget),
        allowed_actions: actions,
        fallbacks: fallbacks.filter((f) => f.provider).map((f) => ({ provider: f.provider, model: f.model || null })),
      });
      config.refresh();
    });

  const saveKey = () =>
    action.run(async () => {
      const body: Record<string, string> = { provider: keyProvider, api_key: keyValue };
      if (keyProvider === "openai_compatible" && baseUrl) body.base_url = baseUrl;
      const saved = await client.post<{ secret_hint?: string }>("/agent/ai/key", body);
      setKeyValue(""); // the key is never kept in the page after saving
      setSavedHint(saved.secret_hint ?? "saved");
      config.refresh();
    });

  const runTest = () =>
    action.run(async () => {
      setTest(await client.post<Record<string, unknown>>("/agent/ai/test", {}));
      usage.refresh();
    });

  return (
    <div className="card pad">
      <h3>AI provider</h3>
      <StatusBanner status={status} />
      <p className="muted small">
        The model only proposes: every tool call it suggests is validated by the server (workspace, role, schema,
        credits) and anything that changes data or spends credits still needs your approval. External providers are
        used only when “Allow external AI providers” above is on.
      </p>
      <div className="form-grid">
        <label className="check">
          <input type="checkbox" checked={enabled} disabled={!admin} onChange={(e) => setEnabled(e.target.checked)} /> AI enabled for this workspace
        </label>
        <label className="field">
          <span className="field__label">Provider</span>
          <select className="input" value={provider} disabled={!admin} onChange={(e) => setProvider(e.target.value)}>
            <option value="">Platform default ({config.data.platform_default})</option>
            {config.data.providers.map((p) => <option key={p} value={p}>{p === "rules" ? "rules (no AI)" : status.providers[p]?.label ?? p}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Model</span>
          <input className="input" value={model} disabled={!admin} onChange={(e) => setModel(e.target.value)} placeholder={status.providers[provider]?.default_model ?? "provider default"} />
        </label>
        <label className="field">
          <span className="field__label">Monthly budget (USD)</span>
          <input className="input" type="number" min={0} step="0.01" value={budget} disabled={!admin} onChange={(e) => setBudget(e.target.value)} placeholder="no limit" />
          <span className="field__hint">Spent this month: ${status.spent_this_month_usd.toFixed(4)} (estimated). At the limit, rules are used.</span>
        </label>
      </div>
      <h4>Allowed AI actions</h4>
      <div className="form-grid">
        {Object.entries(status.actions).map(([key, label]) => (
          <label key={key} className="check">
            <input type="checkbox" disabled={!admin} checked={actions.includes(key)} onChange={(e) => setActions((a) => (e.target.checked ? [...a, key] : a.filter((x) => x !== key)))} />
            {label}
          </label>
        ))}
      </div>
      <h4>Fallbacks (in order)</h4>
      {fallbacks.length === 0 && <p className="muted small">None — if the provider is unavailable, deterministic rules are used.</p>}
      {fallbacks.map((f, i) => (
        <div key={i} className="field-row">
          <select className="input" aria-label={`Fallback ${i + 1} provider`} value={f.provider} disabled={!admin} onChange={(e) => setFallbacks((l) => l.map((x, k) => (k === i ? { ...x, provider: e.target.value } : x)))}>
            {realProviders.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input className="input" aria-label={`Fallback ${i + 1} model`} value={f.model} disabled={!admin} placeholder="model (optional)" onChange={(e) => setFallbacks((l) => l.map((x, k) => (k === i ? { ...x, model: e.target.value } : x)))} />
          <button type="button" className="button button--ghost button--small" disabled={!admin} onClick={() => setFallbacks((l) => l.filter((_, k) => k !== i))}>Remove</button>
        </div>
      ))}
      <div className="form__actions">
        <button type="button" className="button button--ghost button--small" disabled={!admin} onClick={() => setFallbacks((l) => [...l, { provider: realProviders.find((p) => p !== provider) ?? "claude", model: "" }])}>Add fallback</button>
        <button type="button" className="button button--primary" disabled={!admin || action.busy} onClick={save}>Save AI settings</button>
      </div>

      <h4>API key</h4>
      <p className="muted small">Keys are encrypted at rest and never shown again after saving. Without a workspace key, the server's environment variable is used if one is set.</p>
      <table className="table">
        <thead><tr><th>Provider</th><th>Key</th></tr></thead>
        <tbody>
          {realProviders.map((p) => (
            <tr key={p}><td>{status.providers[p]?.label ?? p}</td><td>{status.providers[p]?.key_present ? <Pill value="configured" /> : <Pill value="not_configured" />}</td></tr>
          ))}
        </tbody>
      </table>
      <div className="field-row">
        <label className="field">
          <span className="field__label">Provider</span>
          <select className="input" value={keyProvider} disabled={!admin} onChange={(e) => setKeyProvider(e.target.value)}>
            {realProviders.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="field__label">API key</span>
          <input className="input" type="password" autoComplete="new-password" value={keyValue} disabled={!admin} onChange={(e) => setKeyValue(e.target.value)} placeholder="paste to save or replace" />
        </label>
        {keyProvider === "openai_compatible" && (
          <label className="field">
            <span className="field__label">Base URL (https)</span>
            <input className="input" value={baseUrl} disabled={!admin} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" />
          </label>
        )}
      </div>
      <div className="form__actions">
        <button type="button" className="button button--primary" disabled={!admin || action.busy || keyValue.length < 8} onClick={saveKey}>Save key</button>
        {savedHint && <span className="muted small">Saved (ending {savedHint}). The key will not be shown again.</span>}
        <button type="button" className="button button--ghost" disabled={!admin || action.busy || !status.active} onClick={runTest} title="Makes one tiny real request to the provider">Test connection</button>
      </div>
      {test && <Json value={test} />}
      {!admin && <p className="muted small">Only workspace admins can change AI settings or keys.</p>}
      {action.error && <ErrorBanner error={action.error} />}

      <h4>Usage and cost</h4>
      {usage.data ? (
        <>
          <DataTable
            rows={Object.entries(usage.data.totals).map(([k, t]) => ({ id: k, model: k, ...t }))}
            empty="No AI calls yet."
            columns={[
              { key: "model", label: "Provider : model" },
              { key: "calls", label: "Calls" },
              { key: "prompt_tokens", label: "Prompt tokens" },
              { key: "completion_tokens", label: "Completion tokens" },
              { key: "cost_usd", label: "Est. cost (USD)", render: (r) => `$${Number(r.cost_usd).toFixed(4)}` },
            ]}
          />
          <details className="small">
            <summary>Recent calls</summary>
            <DataTable
              rows={usage.data.recent}
              empty="No AI calls yet."
              columns={[
                { key: "created_at", label: "When", render: (r) => fmt(r.created_at) },
                { key: "purpose", label: "Action" },
                { key: "model", label: "Model" },
                { key: "success", label: "OK", render: (r) => (r.success ? "yes" : "no") },
                { key: "prompt_tokens", label: "In" },
                { key: "completion_tokens", label: "Out" },
                { key: "estimated_cost_usd", label: "USD", render: (r) => (r.estimated_cost_usd == null ? "—" : `$${Number(r.estimated_cost_usd).toFixed(5)}`) },
                { key: "request_id", label: "Request id", className: "mono small" },
              ]}
            />
          </details>
        </>
      ) : <Loading />}
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
