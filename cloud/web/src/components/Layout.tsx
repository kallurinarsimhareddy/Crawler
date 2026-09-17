import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/new", label: "New Crawl", end: false },
  { to: "/jobs", label: "Jobs", end: false },
];

function ApiStatus() {
  const { data, error } = usePolling((signal) => api.health(signal), 15000, "health");
  const state = error ? "down" : data ? "up" : "unknown";
  const label = error ? "API unreachable" : data ? `API ${data.version} · ${data.runner} runner` : "Checking API…";
  return (
    <div className={`api-status api-status--${state}`} title={data ? `${data.environment} · storage: ${data.storage}` : undefined}>
      <span className="api-status__dot" aria-hidden="true" />
      {label}
    </div>
  );
}

export function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMenuOpen(false), [location.pathname]);

  return (
    <div className="shell">
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
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `nav__link${isActive ? " nav__link--active" : ""}`}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar__footer">
          <ApiStatus />
        </div>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
