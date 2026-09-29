// A section: one sticky header (breadcrumbs, title, primary actions) and
// contextual tabs kept in the URL (?tab=…), so every tab can be linked and
// the back button works. Pages shown inside a tab drop their own title.

import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmbeddedContext, PageHeader } from "./ui";

export interface SectionTab {
  key: string;
  label: string;
  render: () => ReactNode;
}

export function SectionTabs({ tabs, active, defaultKey }: { tabs: { key: string; label: string }[]; active: string; defaultKey: string }) {
  const [params] = useSearchParams();
  const href = (key: string) => {
    const next = new URLSearchParams(params);
    next.delete("create");
    if (key === defaultKey) next.delete("tab");
    else next.set("tab", key);
    const text = next.toString();
    return text ? `?${text}` : "?";
  };
  return (
    <nav className="ctabs" aria-label="Section">
      {tabs.map((tab) => (
        <Link key={tab.key} to={{ search: href(tab.key) }} replace className={`ctab${tab.key === active ? " ctab--active" : ""}`} aria-current={tab.key === active ? "page" : undefined}>
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}

export function useSectionTab(tabs: { key: string }[], defaultKey?: string): string {
  const [params] = useSearchParams();
  const wanted = params.get("tab");
  return tabs.find((t) => t.key === wanted)?.key ?? defaultKey ?? tabs[0].key;
}

export function SectionPage({ title, subtitle, actions, tabs, defaultTab }: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode | ((active: string) => ReactNode);
  tabs: SectionTab[];
  defaultTab?: string;
}) {
  const defaultKey = defaultTab ?? tabs[0].key;
  const active = useSectionTab(tabs, defaultKey);
  const tab = tabs.find((t) => t.key === active) ?? tabs[0];
  return (
    <div className="page page--section">
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={typeof actions === "function" ? actions(active) : actions}
        tabs={<SectionTabs tabs={tabs} active={active} defaultKey={defaultKey} />}
      />
      <EmbeddedContext.Provider value>
        <div className="section__body" key={tab.key}>
          {tab.render()}
        </div>
      </EmbeddedContext.Provider>
    </div>
  );
}
