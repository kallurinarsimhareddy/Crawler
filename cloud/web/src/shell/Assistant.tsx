// "Ask SANA GTM AI" from any page: a side panel over the same agent conversation
// as the AI workspace (/ai), so a question asked here continues there with its
// plan, canvas and approvals. Asking only plans; running and approvals stay in
// the AI workspace, exactly as before.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ErrorBanner } from "../components/Feedback";
import { EXAMPLES, remember, remembered } from "../platform/controlroom/ControlRoom";
import type { AskResponse, Session } from "../platform/controlroom/types";
import { JobMonitorUpdate, isJobMonitorUpdate } from "../platform/pages/jobsShared";
import { useAction, useLoad } from "../platform/ui";
import { useWorkspace } from "../platform/workspace";
import { Icon } from "./Icon";

export const PROMPTS = [
  "Find manufacturing companies using SAP.",
  "Show companies hiring SAP managers.",
  "Build a prospect list from these URLs.",
  "Create a campaign for these accounts.",
  "Research this company.",
];

interface AssistantState {
  open: boolean;
  show: (prefill?: string, send?: boolean) => void;
  hide: () => void;
}

const AssistantContext = createContext<AssistantState | null>(null);

export function useAssistant(): AssistantState {
  const value = useContext(AssistantContext);
  if (!value) throw new Error("useAssistant must be used inside AssistantProvider");
  return value;
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [request, setRequest] = useState<{ text: string; send: boolean; n: number } | null>(null);
  const show = useCallback((prefill?: string, send = false) => {
    setOpen(true);
    if (prefill !== undefined) setRequest((r) => ({ text: prefill, send, n: (r?.n ?? 0) + 1 }));
  }, []);
  const hide = useCallback(() => setOpen(false), []);
  const value = useMemo(() => ({ open, show, hide }), [open, show, hide]);
  return (
    <AssistantContext.Provider value={value}>
      {children}
      {open && <AssistantPanel request={request} onClose={hide} />}
    </AssistantContext.Provider>
  );
}

function AssistantPanel({ request, onClose }: { request: { text: string; send: boolean; n: number } | null; onClose: () => void }) {
  const { current, client } = useWorkspace();
  const navigate = useNavigate();
  const wsId = current?.id ?? "";
  const [text, setText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(() => remembered(wsId));
  const [reload, setReload] = useState(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const action = useAction();
  const handled = useRef(0);

  const session = useLoad<Session | null>(
    async (signal) => (client && sessionId ? await client.get<Session>(`/agent/sessions/${sessionId}`, undefined, signal).catch(() => null) : null),
    `${wsId}:${sessionId}:${reload}`,
  );
  const messages = session.data?.messages ?? [];

  const ask = useCallback(
    (message: string) =>
      action.run(async () => {
        if (!client || !message.trim()) return;
        const turn = await client.post<AskResponse>("/agent/ask", { text: message.trim(), session_id: sessionId, mode: "auto", execute: false });
        setSessionId(turn.session.id);
        remember(wsId, turn.session.id);
        setText("");
        setReload((n) => n + 1);
      }),
    [action, client, sessionId, wsId],
  );

  useEffect(() => {
    if (!request || request.n === handled.current) return;
    handled.current = request.n;
    if (request.send && request.text.trim()) void ask(request.text);
    else setText(request.text);
  }, [request, ask]);

  useEffect(() => {
    input.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    void end.current?.scrollIntoView({ block: "nearest" });
  }, [messages.length]);

  const openWorkspace = () => {
    onClose();
    navigate("/ai");
  };

  return (
    <>
      <div className="assist__scrim" onClick={onClose} aria-hidden="true" />
      <aside className="assist" role="dialog" aria-modal="true" aria-labelledby="assist-title">
        <header className="assist__head">
          <div className="assist__title" id="assist-title">
            <Icon name="sparkles" /> Ask SANA GTM AI
          </div>
          <div className="actions">
            <button type="button" className="button button--ghost button--small" onClick={openWorkspace}>Open AI workspace</button>
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Close">
              <Icon name="close" />
            </button>
          </div>
        </header>
        <div className="assist__body" role="log" aria-label="Conversation">
          {!client ? (
            <p className="muted small">Create a workspace first — the assistant works inside a workspace.</p>
          ) : messages.length === 0 ? (
            <div className="assist__intro">
              <p>Ask in plain language. I plan first and show you the plan, the sources and the credit cost — nothing that changes your CRM, spends credits or reaches out happens without your approval.</p>
              <div className="assist__prompts">
                {[...PROMPTS, ...EXAMPLES.slice(0, 1)].map((p) => (
                  <button key={p} type="button" className="assist__prompt" onClick={() => setText(p)}>
                    <Icon name="sparkles" size={14} /> {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m) => (
              <div key={m.id} className={`assist__msg assist__msg--${m.role}`}>
                <div className="assist__who">{m.role === "user" ? "You" : "SANA GTM AI"}</div>
                <div className="assist__text">{m.content}</div>
                {isJobMonitorUpdate(m.data) && <JobMonitorUpdate data={m.data} onNavigate={onClose} />}
                {m.run_id && (
                  <button type="button" className="link link-button small" onClick={openWorkspace}>
                    Review the plan in the AI workspace →
                  </button>
                )}
              </div>
            ))
          )}
          {action.busy && <div className="assist__msg assist__msg--assistant muted small">Planning…</div>}
          <div ref={end} />
        </div>
        {action.error && <ErrorBanner error={action.error} />}
        <form
          className="assist__form"
          onSubmit={(e) => {
            e.preventDefault();
            void ask(text);
          }}
        >
          <label htmlFor="assist-input" className="sr-only">Your request</label>
          <textarea
            id="assist-input"
            ref={input}
            className="input textarea"
            rows={2}
            value={text}
            placeholder="Find manufacturing companies using SAP…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void ask(text);
              }
            }}
          />
          <button type="submit" className="button button--primary" disabled={!client || action.busy || !text.trim()} aria-label="Ask">
            <Icon name="send" size={16} />
          </button>
        </form>
        <p className="assist__foot muted small">Enter to ask · Shift+Enter for a new line · Esc to close</p>
      </aside>
    </>
  );
}
