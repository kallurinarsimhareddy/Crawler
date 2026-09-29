// The AI Control Room: the primary workspace. "What do you want me to do?" →
// plan (Research) or plan-and-run (Run) → conversation + Research Canvas.

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorBanner } from "../../components/Feedback";
import type { Row } from "../api";
import { Pill, fmtDate, useAction, useLoad } from "../ui";
import { useWorkspace, useWs } from "../workspace";
import { ResearchCanvas } from "./Canvas";
import { ASK_EVENT, type AgentRun, type AskResponse, type ModeInfo, type Session } from "./types";

const EXAMPLES = [
  "Find US manufacturing companies using SAP with new hiring activity.",
  "Find 500 companies using RPG/AS400, remove companies already in our CRM, find missing IT leaders, validate available emails, and prepare an outreach list.",
  "Monitor these 1,000 companies and tell me when hiring increases.",
  "Find companies with new Oracle ERP implementation roles.",
  "Find 500 US manufacturing companies with SAP, Oracle, JD Edwards or Infor hiring. Remove companies already in my CRM. Find missing IT/HR/VP contacts using my authorized sources. Validate available emails. Rank the opportunities and prepare a Cox-Little campaign list.",
];

const FOLLOW_UPS = ["Remove companies already in our CRM", "Find missing IT leaders", "Validate available emails", "Keep the top 50", "Export as XLSX", "Run it"];

const SESSION_KEY = "careercrawler.agent.session";

function remembered(ws: string): string | null {
  try {
    return window.localStorage.getItem(`${SESSION_KEY}.${ws}`);
  } catch {
    return null;
  }
}

function remember(ws: string, id: string | null): void {
  try {
    if (id) window.localStorage.setItem(`${SESSION_KEY}.${ws}`, id);
    else window.localStorage.removeItem(`${SESSION_KEY}.${ws}`);
  } catch {
    // storage blocked: the conversation just is not remembered across reloads
  }
}

function Conversation({ session, onPickRun, activeRun }: { session: Session | null; onPickRun: (id: string) => void; activeRun: string | null }) {
  const end = useRef<HTMLDivElement>(null);
  const count = session?.messages?.length ?? 0;
  useEffect(() => {
    // Newer browsers return a Promise from scrollIntoView; an effect must not return it.
    void end.current?.scrollIntoView({ block: "nearest" });
  }, [count]);
  if (!session || count === 0) return null;
  return (
    <div className="cr-chat" aria-label="Conversation" role="log">
      {session.messages!.map((m) => (
        <div key={m.id} className={`cr-msg cr-msg--${m.role}`}>
          <div className="cr-msg__who">{m.role === "user" ? "You" : "SANA GTM AI"}</div>
          <div className="cr-msg__body">{m.content}</div>
          {m.run_id && (
            <button type="button" className={`button button--ghost button--small${m.run_id === activeRun ? " cr-active" : ""}`} onClick={() => onPickRun(m.run_id!)}>
              {m.run_id === activeRun ? "Shown below" : "Open in canvas"}
            </button>
          )}
        </div>
      ))}
      <div ref={end} />
    </div>
  );
}

function SidePanel({ onFill, onOpenRun, reload }: { onFill: (text: string) => void; onOpenRun: (id: string) => void; reload: number }) {
  const client = useWs();
  const saved = useLoad((s) => client.list("/agent/saved", undefined, s), client.base + "saved" + reload);
  const runs = useLoad((s) => client.list("/agent/runs", { limit: 8 }, s), client.base + "runs" + reload);
  const insights = useLoad((s) => client.list("/agent/insights", undefined, s), client.base + "insights" + reload);
  const approvals = useLoad((s) => client.get<{ items: Row[] }>("/agent/approvals", undefined, s), client.base + "appr" + reload, 15000);
  const action = useAction();
  const pending = approvals.data?.items ?? [];
  return (
    <aside className="cr-side" aria-label="Control room panel">
      <div className="card pad">
        <div className="title-row"><h3>Approvals</h3><span className={`tab__count${pending.length ? " cr-warn" : ""}`}>{pending.length}</span></div>
        {pending.length === 0 ? <p className="muted small">Nothing waiting.</p> : (
          <ul className="cr-list">
            {pending.slice(0, 6).map((a) => (
              <li key={a.id}><button type="button" className="link cr-linkbtn" onClick={() => onOpenRun(String(a.run_id))}>{String(a.action)}</button></li>
            ))}
          </ul>
        )}
      </div>
      <div className="card pad">
        <div className="title-row">
          <h3>Proactive insights</h3>
          <button type="button" className="button button--ghost button--small" disabled={action.busy} onClick={() => action.run(async () => { await client.post("/agent/insights/refresh"); insights.refresh(); })}>Refresh</button>
        </div>
        {action.error && <ErrorBanner error={action.error} />}
        {(insights.data?.items ?? []).length === 0 ? <p className="muted small">No new insights. The platform checks for hiring spikes, ERP implementation hiring, missing IT leaders and ATS changes.</p> : (
          <ul className="cr-list">
            {insights.data!.items.map((i) => (
              <li key={i.id} className={`cr-insight cr-insight--${String(i.severity)}`}>
                <strong>{String(i.title)}</strong>
                <div className="muted small">{String(i.detail ?? "")}</div>
                <div className="actions">
                  {i.suggested_request ? <button type="button" className="button button--ghost button--small" onClick={() => onFill(String(i.suggested_request))}>Investigate</button> : null}
                  <button type="button" className="button button--ghost button--small" onClick={() => action.run(async () => { await client.post(`/agent/insights/${i.id}/dismissed`); insights.refresh(); })}>Dismiss</button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="card pad">
        <h3>Saved requests</h3>
        {(saved.data?.items ?? []).length === 0 ? <p className="muted small">Save a request to reuse it.</p> : (
          <ul className="cr-list">
            {saved.data!.items.map((s) => (
              <li key={s.id}>
                <button type="button" className="link cr-linkbtn" onClick={() => onFill(String(s.request))}>{String(s.name)}</button>
                {s.kind === "view" && <span className="chip">view</span>}
                <button type="button" className="button button--ghost button--small" aria-label={`Delete ${String(s.name)}`} onClick={() => action.run(async () => { await client.del(`/agent/saved/${s.id}`); saved.refresh(); })}>×</button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="card pad">
        <h3>Recent runs</h3>
        {(runs.data?.items ?? []).length === 0 ? <p className="muted small">No runs yet.</p> : (
          <ul className="cr-list">
            {runs.data!.items.map((r) => (
              <li key={r.id}>
                <button type="button" className="link cr-linkbtn" onClick={() => onOpenRun(r.id)}>{String(r.request).slice(0, 80)}</button>
                <div className="small muted"><Pill value={r.status} /> {fmtDate(r.created_at)}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

export function ControlRoom() {
  const client = useWs();
  const { current } = useWorkspace();
  const wsId = current?.id ?? "";
  const [text, setText] = useState("");
  const [mode, setMode] = useState("auto");
  const [sessionId, setSessionId] = useState<string | null>(() => remembered(wsId));
  const [runId, setRunId] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const prompt = useRef<HTMLTextAreaElement>(null);
  const action = useAction();
  const modes = useLoad((s) => client.get<{ items: ModeInfo[] }>("/agent/modes", undefined, s), client.base + "modes");
  // The loaded value remembers which session id it belongs to, so a stale result
  // (e.g. the empty one from before the first message) is never mistaken for
  // "this session no longer exists".
  const loaded = useLoad<{ id: string | null; session: Session | null }>(
    async (s) => ({
      id: sessionId,
      session: sessionId
        ? await client.get<Session>(`/agent/sessions/${sessionId}`, undefined, s).catch(() => null)
        : null,
    }),
    `${client.base}session:${sessionId}:${reload}`,
  );
  const session = { data: loaded.data && loaded.data.id === sessionId ? loaded.data.session : null };

  useEffect(() => {
    setSessionId(remembered(wsId));
    setRunId(null);
  }, [wsId]);

  useEffect(() => {
    if (!loaded.data || loaded.data.id !== sessionId) return;
    const found = loaded.data.session;
    if (found && found.status === "archived") {
      setSessionId(null);
      remember(wsId, null);
      return;
    }
    if (found && !runId && found.last_run_id) setRunId(found.last_run_id);
    if (sessionId && found === null) {
      setSessionId(null);
      remember(wsId, null);
    }
  }, [loaded.data, runId, sessionId, wsId]);

  const focus = useCallback((prefill?: string) => {
    if (prefill) setText(prefill);
    window.setTimeout(() => prompt.current?.focus(), 0);
  }, []);

  useEffect(() => {
    const onAsk = (event: Event) => focus((event as CustomEvent<{ prefill?: string }>).detail?.prefill);
    window.addEventListener(ASK_EVENT, onAsk);
    focus();
    return () => window.removeEventListener(ASK_EVENT, onAsk);
  }, [focus]);

  const ask = (execute: boolean, message?: string) =>
    action.run(async () => {
      const body = { text: (message ?? text).trim(), session_id: sessionId, mode, execute };
      const turn = await client.post<AskResponse>("/agent/ask", body);
      setSessionId(turn.session.id);
      remember(wsId, turn.session.id);
      if (turn.run) setRunId(turn.run.id);
      setText("");
      setReload((n) => n + 1);
    });

  const clear = () =>
    action.run(async () => {
      if (sessionId) await client.post(`/agent/sessions/${sessionId}/clear`);
      setSessionId(null);
      remember(wsId, null);
      setRunId(null);
      setReload((n) => n + 1);
    });

  const save = () =>
    action.run(async () => {
      const name = window.prompt("Name this request", text.slice(0, 80));
      if (!name) return;
      await client.post("/agent/saved", { name, request: text, mode, kind: "request" });
      setReload((n) => n + 1);
    });

  const modeInfo = modes.data?.items.find((m) => m.key === mode);
  const inConversation = Boolean(session.data?.messages?.length);

  return (
    <div className="page cr-page">
      <div className="cr-layout">
        <div className="cr-main">
          <section className="cr-hero card pad" aria-labelledby="cr-ask-title">
            <h1 id="cr-ask-title" className="cr-hero__title">What do you want me to do?</h1>
            <p className="muted small">I plan first and show you the plan, the sources and the credit cost. Searches and analysis run on their own; anything that changes your CRM, spends credits or reaches out waits for your approval.</p>
            <Conversation session={session.data ?? null} onPickRun={setRunId} activeRun={runId} />
            <form
              className="cr-ask"
              onSubmit={(e) => {
                e.preventDefault();
                if (text.trim()) void ask(false);
              }}
            >
              <label htmlFor="cr-prompt" className="sr-only">Your request</label>
              <textarea
                id="cr-prompt"
                ref={prompt}
                className="input textarea cr-prompt"
                rows={inConversation ? 2 : 4}
                value={text}
                placeholder={inConversation ? "Follow up on these results… (e.g. Remove companies already in our CRM)" : EXAMPLES[0]}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && text.trim()) {
                    e.preventDefault();
                    void ask(true);
                  }
                }}
              />
              <div className="cr-ask__bar">
                <label className="cr-mode">
                  <span className="sr-only">Agent</span>
                  <select className="input input--small" value={mode} onChange={(e) => setMode(e.target.value)} aria-describedby="cr-mode-desc">
                    {(modes.data?.items ?? [{ key: "auto", title: "SANA GTM AI", description: "", tools: [] }]).map((m) => (
                      <option key={m.key} value={m.key}>{m.title}</option>
                    ))}
                  </select>
                </label>
                <span id="cr-mode-desc" className="muted small cr-mode__desc">{modeInfo?.description}</span>
                <div className="actions">
                  <button type="submit" className="button button--ghost" disabled={action.busy || !text.trim()} title="Plan only — nothing runs">Research</button>
                  <button type="button" className="button button--primary" disabled={action.busy || !text.trim()} onClick={() => ask(true)} title="Plan and run the safe steps (Ctrl+Enter)">
                    {action.busy ? "Working…" : "Run"}
                  </button>
                  <button type="button" className="button button--ghost button--small" disabled={action.busy || !text.trim()} onClick={save}>Save request</button>
                  <button type="button" className="button button--ghost button--small" disabled={action.busy || (!sessionId && !runId)} onClick={clear}>Clear history</button>
                </div>
              </div>
            </form>
            {action.error && <ErrorBanner error={action.error} />}
            <div className="chips cr-examples" aria-label="Examples">
              {(inConversation ? FOLLOW_UPS : EXAMPLES).map((example) => (
                <button key={example} type="button" className="chip cr-chipbtn" onClick={() => (inConversation ? ask(false, example) : setText(example))}>
                  {example.length > 90 ? `${example.slice(0, 88)}…` : example}
                </button>
              ))}
            </div>
          </section>
          {runId ? (
            <ResearchCanvas
              runId={runId}
              onRunChange={(run: AgentRun) => {
                if (run.id !== runId) setRunId(run.id);
                setReload((n) => n + 1);
              }}
            />
          ) : (
            <div className="card pad muted small">Ask a question to see its plan, sources, live progress, results, evidence and proposed actions here.</div>
          )}
        </div>
        <SidePanel onFill={(t) => focus(t)} onOpenRun={setRunId} reload={reload} />
      </div>
    </div>
  );
}
