// The header on every page: brand, workspace, global search / command palette,
// Ask SANA GTM AI, notifications and the user menu.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { useWorkerStatus } from "../components/WorkerStatus";
import { usePolling } from "../hooks/usePolling";
import type { Row } from "../platform/api";
import { WorkspaceSwitcher } from "../platform/Shell";
import { useLoad } from "../platform/ui";
import { useWorkspace } from "../platform/workspace";
import { useAssistant } from "./Assistant";
import { Icon } from "./Icon";

function Dropdown({ label, button, children, align = "right" }: { label: string; button: ReactNode; children: (close: () => void) => ReactNode; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => root.current && !root.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="dropdown" ref={root}>
      <button type="button" className="iconbtn" aria-label={label} aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {button}
      </button>
      {open && <div className={`dropdown__menu dropdown__menu--${align}`} role="menu">{children(() => setOpen(false))}</div>}
    </div>
  );
}

function StatusLine() {
  const health = usePolling((signal) => api.health(signal), 30000, "health");
  const worker = useWorkerStatus();
  const apiUp = Boolean(health.data) && !health.error;
  const workerUp = Boolean(worker.data?.worker.online);
  return (
    <div className="statusline">
      <span className={`statusdot statusdot--${apiUp ? "on" : health.error ? "off" : "unknown"}`} /> API {apiUp ? `${health.data?.version ?? ""} online` : health.error ? "unreachable" : "checking…"}
      <span className={`statusdot statusdot--${workerUp ? "on" : worker.data ? "off" : "unknown"}`} /> Worker {workerUp ? "online" : worker.data ? "offline" : "—"}
    </div>
  );
}

function Notifications() {
  const { client } = useWorkspace();
  const navigate = useNavigate();
  const approvals = useLoad<{ items: Row[] }>(
    async (signal) => (client ? await client.get<{ items: Row[] }>("/agent/approvals", undefined, signal).catch(() => ({ items: [] })) : { items: [] }),
    `appr:${client?.base ?? ""}`,
    30000,
  );
  const pending = approvals.data?.items ?? [];
  return (
    <Dropdown
      label={`Notifications${pending.length ? ` (${pending.length} waiting)` : ""}`}
      button={
        <>
          <Icon name="bell" />
          {pending.length > 0 && <span className="iconbtn__badge">{pending.length}</span>}
        </>
      }
    >
      {(close) => (
        <div className="notif">
          <div className="dropdown__title">Notifications</div>
          {pending.length === 0 ? (
            <p className="muted small notif__empty">You're all caught up. Approvals for AI plans that change your CRM, spend credits or reach out will appear here.</p>
          ) : (
            pending.slice(0, 6).map((a) => (
              <button key={a.id} type="button" className="dropdown__item" onClick={() => { close(); navigate("/ai"); }}>
                <span className="notif__dot" />
                <span>
                  <strong>Approval needed</strong>
                  <span className="muted small block">{String(a.reason ?? a.action ?? "An AI plan step is waiting for you")}</span>
                </span>
              </button>
            ))
          )}
          <div className="dropdown__sep" />
          <StatusLine />
        </div>
      )}
    </Dropdown>
  );
}

function UserMenu() {
  const { session, signOut, mode } = useAuth();
  const { current } = useWorkspace();
  if (!session) return null;
  const who = session.email ?? "Signed in";
  return (
    <Dropdown label="Account" button={<span className="avatar" aria-hidden="true">{who.slice(0, 1).toUpperCase()}</span>}>
      {(close) => (
        <>
          <div className="dropdown__title">
            <span className="truncate block" title={who}>{who}</span>
            <span className="muted small">{current ? `${current.name} · ${current.role}` : "No workspace"}{mode === "dev" ? " · dev" : ""}</span>
          </div>
          <Link className="dropdown__item" to="/settings" onClick={close}><Icon name="settings" size={16} /> Settings</Link>
          <Link className="dropdown__item" to="/credits" onClick={close}><Icon name="coin" size={16} /> Credits</Link>
          <Link className="dropdown__item" to="/background" onClick={close}><Icon name="clock" size={16} /> Background jobs</Link>
          <div className="dropdown__sep" />
          <button type="button" className="dropdown__item" onClick={() => void signOut()}><Icon name="logout" size={16} /> Sign out</button>
        </>
      )}
    </Dropdown>
  );
}

export function Topbar({ onMenu, onRail, rail, onSearch }: { onMenu: () => void; onRail: () => void; rail: boolean; onSearch: () => void }) {
  const assistant = useAssistant();
  return (
    <header className="topbar">
      <div className="topbar__left">
        <button type="button" className="iconbtn topbar__menu" onClick={onMenu} aria-label="Open navigation">
          <Icon name="menu" />
        </button>
        <button type="button" className="iconbtn topbar__rail" onClick={onRail} aria-label={rail ? "Expand sidebar" : "Collapse sidebar"} title={rail ? "Expand sidebar" : "Collapse sidebar"}>
          <Icon name="panel" />
        </button>
        <Link to="/" className="brand" aria-label="SANA GTM home">
          <img src="/favicon.svg" alt="" width={24} height={24} />
          <span className="brand__name">SANA GTM</span>
        </Link>
        <span className="topbar__divider" aria-hidden="true" />
        <WorkspaceSwitcher />
      </div>
      <button type="button" className="searchbtn" onClick={onSearch}>
        <Icon name="search" size={16} />
        <span className="searchbtn__text">Search or ask anything…</span>
        <kbd className="kbd">Ctrl K</kbd>
      </button>
      <div className="topbar__right">
        <button type="button" className="button button--primary button--small askbtn" onClick={() => assistant.show()} title="Ask SANA GTM AI (Ctrl+K)">
          <Icon name="sparkles" size={16} /> <span className="askbtn__text">Ask SANA GTM AI</span>
        </button>
        <Notifications />
        <UserMenu />
      </div>
    </header>
  );
}
