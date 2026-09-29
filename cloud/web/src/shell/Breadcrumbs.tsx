// Breadcrumbs derived from the navigation: Section › Page › Record.

import { Link, useLocation } from "react-router-dom";
import { Icon } from "./Icon";
import { locate } from "./nav";

export interface Crumb {
  label: string;
  to?: string;
}

export function useCrumbs(title?: string): Crumb[] {
  const { pathname } = useLocation();
  const owner = locate(pathname);
  if (!owner || pathname === "/") return [];
  const crumbs: Crumb[] = [{ label: "Home", to: "/" }];
  const { group, item } = owner;
  if (!item) return crumbs;
  if (group.items.length > 1) crumbs.push({ label: group.title });
  const atItem = pathname === item.to;
  crumbs.push({ label: item.label, to: atItem ? undefined : item.to });
  if (!atItem && title && title !== item.label) crumbs.push({ label: title });
  return crumbs;
}

export function Breadcrumbs({ crumbs }: { crumbs: Crumb[] }) {
  if (crumbs.length < 2) return null;
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <ol>
        {crumbs.map((crumb, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={`${crumb.label}${i}`}>
              {crumb.to && !last ? <Link to={crumb.to}>{crumb.label}</Link> : <span aria-current={last ? "page" : undefined}>{crumb.label}</span>}
              {!last && <Icon name="chevron" size={12} className="crumbs__sep" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
