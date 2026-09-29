// Primary navigation: a few top-level entries, each group collapsible. Which groups
// are open (and whether the sidebar is collapsed to icons) is remembered per browser.

import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import { NAV, locate, type NavGroup } from "./nav";

const GROUPS_KEY = "sanagtm.nav.groups";

function readGroups(): Record<string, boolean> {
  try {
    return JSON.parse(window.localStorage.getItem(GROUPS_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

function writeGroups(value: Record<string, boolean>): void {
  try {
    window.localStorage.setItem(GROUPS_KEY, JSON.stringify(value));
  } catch {
    // storage blocked: groups simply reset on reload
  }
}

function linkClass({ isActive }: { isActive: boolean }): string {
  return `snav__link${isActive ? " snav__link--active" : ""}`;
}

export function NavigationGroup({ group, open, active, rail, onToggle }: { group: NavGroup; open: boolean; active: boolean; rail: boolean; onToggle: () => void }) {
  const location = useLocation();
  const owner = locate(location.pathname);
  if (group.to && (group.items.length <= 1)) {
    const isActive = owner?.group.key === group.key;
    return (
      <NavLink to={group.to} end={group.to === "/"} className={() => linkClass({ isActive })} title={rail ? group.title : undefined}>
        <Icon name={group.icon} />
        <span className="snav__label">{group.title}</span>
      </NavLink>
    );
  }
  if (rail) {
    // Collapsed to icons: a group icon opens the group's first page.
    return (
      <NavLink to={group.items[0].to} className={() => linkClass({ isActive: active })} title={`${group.title}: ${group.items.map((i) => i.label).join(", ")}`}>
        <Icon name={group.icon} />
        <span className="snav__label">{group.title}</span>
      </NavLink>
    );
  }
  const id = `snav-${group.key}`;
  return (
    <div className={`snav__group${active ? " snav__group--active" : ""}`}>
      <button type="button" className="snav__heading" aria-expanded={open} aria-controls={id} onClick={onToggle} title={rail ? group.title : undefined}>
        <Icon name={group.icon} />
        <span className="snav__label">{group.title}</span>
        <Icon name="chevron" size={14} className={`snav__chev${open ? " snav__chev--open" : ""}`} />
      </button>
      {open && (
        <div id={id} className="snav__items">
          {group.items.map((item) => {
            const isActive = owner?.item?.to === item.to;
            return (
              <NavLink key={item.to} to={item.to} className={() => `snav__sublink${isActive ? " snav__sublink--active" : ""}`}>
                {item.label}
              </NavLink>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function Sidebar({ rail, mobileOpen, onNavigate }: { rail: boolean; mobileOpen: boolean; onNavigate: () => void }) {
  const location = useLocation();
  const owner = locate(location.pathname);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(() => readGroups());

  // The section you are in is always visible, without overwriting your saved choices.
  const isOpen = (key: string) => openGroups[key] ?? owner?.group.key === key;

  useEffect(() => onNavigate(), [location.pathname, onNavigate]);

  const toggle = (key: string) =>
    setOpenGroups((current) => {
      const next = { ...current, [key]: !(current[key] ?? owner?.group.key === key) };
      writeGroups(next);
      return next;
    });

  return (
    <nav className={`snav${rail ? " snav--rail" : ""}${mobileOpen ? " snav--open" : ""}`} aria-label="Primary">
      <div className="snav__scroll">
        {NAV.map((group) => (
          <NavigationGroup
            key={group.key}
            group={group}
            rail={rail}
            open={isOpen(group.key)}
            active={owner?.group.key === group.key}
            onToggle={() => toggle(group.key)}
          />
        ))}
      </div>
    </nav>
  );
}
