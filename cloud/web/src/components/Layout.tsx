import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { usePolling } from "../hooks/usePolling";
import { WorkerPill, useWorkerStatus } from "./WorkerStatus";
import { WorkspaceSwitcher } from "../platform/Shell";

const NAV_GROUPS: { title: string; items: { to: string; label: string; end?: boolean }[] }[] = [
  {
    title: "Intelligence",
    items: [
      { to: "/", label: "Dashboard", end: true },
      { to: "/companies", label: "Companies" },
      { to: "/contacts", label: "Contacts" },
      { to: "/postings", label: "Jobs" },
      { to: "/hiring", label: "Hiring Intelligence" },
      { to: "/discovery", label: "Discovery" },
      { to: "/scraper", label: "Scraper" },
      { to: "/research", label: "Research Agent" },
    ],
  },
  {
    title: "CRM",
    items: [
      { to: "/opportunities", label: "Opportunities" },
      { to: "/tasks", label: "Tasks" },
      { to: "/activities", label: "Activities" },
      { to: "/lists", label: "Lists" },
      { to: "/segments", label: "Segments" },
    ],
  },
  {
    title: "GTM",
    items: [
      { to: "/campaigns", label: "Campaigns" },
      { to: "/sequences", label: "Sequences" },
      { to: "/templates", label: "Templates" },
      { to: "/suppressions", label: "Suppression" },
      { to: "/workflows", label: "Workflows" },
      { to: "/monitors", label: "Monitors" },
    ],
  },
  {
    title: "Data",
    items: [
      { to: "/imports", label: "Imports" },
      { to: "/exports", label: "Exports" },
      { to: "/sources", label: "Sources" },
      { to: "/credits", label: "Credits" },
      { to: "/analytics", label: "Analytics" },
    ],
  },
  {
    title: "System",
    items: [
      { to: "/jobs", label: "Crawls" },
      { to: "/background", label: "Background jobs" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

function ApiStatus() {
  const { data, error } = usePolling((signal) => api.health(signal), 15000, "health");
  const state = error ? "down" : data ? "up" : "unknown";
  const label = error ? "API unreachable" : data ? `API ${data.version} · ${data.runner}` : "Checking API…";
  return (
    <div
      className={`api-status api-status--${state}`}
      title={data ? `${data.environment} · storage: ${data.storage} · queue: ${data.queue ?? "—"} · auth: ${data.auth ?? "—"}` : undefined}
    >
      <span className="api-status__dot" aria-hidden="true" />
      {label}
    </div>
  );
}

function UserMenu() {
  const { session, signOut, mode } = useAuth();
  if (!session) return null;
  return (
    <div className="user">
      <div className="user__who">
        <span className="user__email" title={session.email ?? session.userId}>
          {session.email ?? "Signed in"}
        </span>
        {mode === "dev" && <span className="tag">dev</span>}
      </div>
      <button type="button" className="button button--ghost button--small" onClick={() => void signOut()}>
        Sign out
      </button>
    </div>
  );
}

function WorkerIndicator() {
  const { data, error } = useWorkerStatus();
  return <WorkerPill status={data} error={error} />;
}

export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMenuOpen(false), [location.pathname]);

  const staging = import.meta.env.VITE_DEPLOY_ENV === "staging";
  return (
    <div className={`shell${staging ? " shell--staging" : ""}`}>
      {staging && (
        <div className="env-banner" role="note">
          STAGING — test environment. Data may be deleted at any time.
        </div>
      )}
      <header className="sidebar">
        <div className="sidebar__top">
          <NavLink to="/" className="brand">
            <img src="/favicon.svg" alt="" width={28} height={28} />
            <span>CareerCrawler</span>
          </NavLink>
          <button
            type="button"
            className="menu-toggle"
            aria-expanded={menuOpen}
            aria-controls="primary-nav"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span className="sr-only">Menu</span>
            <span aria-hidden="true">{menuOpen ? "✕" : "☰"}</span>
          </button>
        </div>
        <nav id="primary-nav" className={`nav${menuOpen ? " nav--open" : ""}`}>
          {NAV_GROUPS.map((group) => (
            <div key={group.title} className="nav__group">
              <div className="nav__heading">{group.title}</div>
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `nav__link${isActive ? " nav__link--active" : ""}`}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar__footer">
          <WorkspaceSwitcher />
          <UserMenu />
          <WorkerIndicator />
          <ApiStatus />
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
