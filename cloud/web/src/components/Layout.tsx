import { useCallback, useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { AssistantProvider } from "../shell/Assistant";
import { CommandPalette } from "../shell/CommandPalette";
import { Sidebar } from "../shell/Sidebar";
import { Topbar } from "../shell/Topbar";

const RAIL_KEY = "sanagtm.nav.rail";

function readRail(): boolean {
  try {
    return window.localStorage.getItem(RAIL_KEY) === "1";
  } catch {
    return false;
  }
}

/** The application frame: top bar, collapsible sidebar, page, command palette and AI panel. */
export function Layout() {
  const [rail, setRail] = useState(readRail);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [palette, setPalette] = useState(false);

  const toggleRail = () =>
    setRail((value) => {
      try {
        window.localStorage.setItem(RAIL_KEY, value ? "0" : "1");
      } catch {
        // storage blocked: the choice lasts for this page only
      }
      return !value;
    });
  const closeMobile = useCallback(() => setMobileOpen(false), []);

  // Ctrl+K / Cmd+K opens the command palette from anywhere.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const staging = import.meta.env.VITE_DEPLOY_ENV === "staging";
  return (
    <AssistantProvider>
      <div className={`app${staging ? " app--staging" : ""}${rail ? " app--rail" : ""}`}>
        {staging && (
          <div className="env-banner" role="note">
            STAGING — test environment. Data may be deleted at any time.
          </div>
        )}
        <Topbar onMenu={() => setMobileOpen((o) => !o)} onRail={toggleRail} rail={rail} onSearch={() => setPalette(true)} />
        <Sidebar rail={rail && !mobileOpen} mobileOpen={mobileOpen} onNavigate={closeMobile} />
        {mobileOpen && <div className="snav__scrim" onClick={closeMobile} aria-hidden="true" />}
        <main className="main" id="main">
          <Outlet />
        </main>
        {palette && <CommandPalette onClose={() => setPalette(false)} />}
      </div>
    </AssistantProvider>
  );
}
