// The information architecture: primary navigation groups, and which section a
// route belongs to (for breadcrumbs and the active state). Every existing route
// stays reachable; routes that are not in the sidebar are tabs of a section.

import type { IconName } from "./Icon";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  /** Other route prefixes that belong to this item (for the active state and breadcrumbs). */
  also?: string[];
  /** Words the command palette also matches. */
  keywords?: string;
}

export interface NavGroup {
  key: string;
  title: string;
  icon: IconName;
  /** A group with one item and no children renders as a plain link. */
  to?: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  { key: "home", title: "Home", icon: "home", to: "/", items: [] },
  {
    key: "crm",
    title: "CRM",
    icon: "building",
    items: [
      { to: "/companies", label: "Companies", icon: "building", also: ["/segments", "/provenance"], keywords: "accounts organizations" },
      { to: "/contacts", label: "Contacts", icon: "users", keywords: "people leads" },
      { to: "/opportunities", label: "Deals", icon: "deal", keywords: "opportunities pipeline" },
      { to: "/tasks", label: "Tasks", icon: "check", keywords: "to-do follow-ups" },
      { to: "/activities", label: "Activities", icon: "activity", keywords: "timeline calls meetings notes" },
    ],
  },
  {
    key: "gtm",
    title: "GTM",
    icon: "target",
    items: [
      { to: "/prospecting", label: "Prospecting", icon: "search", also: ["/discovery"], keywords: "discovery find companies saved searches" },
      { to: "/lists", label: "Lists", icon: "list", keywords: "prospect lists" },
      { to: "/campaigns", label: "Campaigns", icon: "megaphone", also: ["/suppressions"], keywords: "outreach suppression" },
      { to: "/sequences", label: "Sequences", icon: "repeat", keywords: "cadence steps follow-up" },
      { to: "/email-validation", label: "Email Validation", icon: "mailcheck", keywords: "verify emails bounce check csv xlsx upload emaillistverify" },
      { to: "/templates", label: "Templates", icon: "file", keywords: "email templates" },
    ],
  },
  {
    key: "intel",
    title: "Intelligence",
    icon: "spark",
    items: [
      { to: "/hiring", label: "Hiring Intelligence", icon: "trend", also: ["/postings"], keywords: "jobs postings trends technology" },
      { to: "/signals", label: "Signals", icon: "signal", keywords: "hiring signals" },
      { to: "/research", label: "Research Agent", icon: "bot", also: ["/ai"], keywords: "ai control room research" },
      { to: "/scraper", label: "AI Scraper", icon: "scraper", keywords: "extract crawl websites urls" },
    ],
  },
  {
    key: "automation",
    title: "Automation",
    icon: "workflow",
    items: [
      { to: "/workflows", label: "Workflows", icon: "workflow", keywords: "automation builder" },
      { to: "/monitors", label: "Monitors", icon: "eye", keywords: "watch changes alerts" },
    ],
  },
  { key: "analytics", title: "Analytics", icon: "chart", to: "/analytics", items: [{ to: "/analytics", label: "Analytics", icon: "chart", also: ["/dashboard"] }] },
  {
    key: "admin",
    title: "Admin",
    icon: "settings",
    items: [
      { to: "/imports", label: "Imports", icon: "upload", keywords: "upload files csv xlsx" },
      { to: "/internal-data", label: "Internal Data", icon: "layers", keywords: "multi-file batch schema mapping dedupe merge conflicts" },
      { to: "/exports", label: "Exports", icon: "download", keywords: "download csv xlsx" },
      { to: "/sources", label: "Sources", icon: "database", keywords: "providers connectors job boards enrichment linkedin indeed dice usajobs adzuna" },
      { to: "/settings/sending", label: "Email & Sending", icon: "mail", keywords: "mailboxes gmail google microsoft 365 outlook smtp sender connect" },
      { to: "/settings/integrations", label: "Integrations", icon: "plug", keywords: "slack webhooks calendar google workspace microsoft" },
      { to: "/settings/users", label: "Users & Permissions", icon: "shield", keywords: "team members roles invite admin manager read-only" },
      { to: "/settings/audit", label: "Audit Log", icon: "history", keywords: "history who changed activity log security" },
      { to: "/settings", label: "Settings", icon: "settings", also: ["/credits", "/background", "/jobs", "/new", "/ai/memory"], keywords: "credits background jobs crawls ai memory workspace" },
    ],
  },
];

function matches(pathname: string, prefix: string): boolean {
  if (prefix === "/") return pathname === "/";
  return pathname === prefix || pathname.startsWith(prefix + "/");
}

/** The sidebar item (and its group) that owns a route, if any. */
export function locate(pathname: string): { group: NavGroup; item: NavItem | null } | null {
  let best: { group: NavGroup; item: NavItem | null; length: number } | null = null;
  for (const group of NAV) {
    if (group.to && group.items.length === 0 && matches(pathname, group.to)) return { group, item: null };
    for (const item of group.items) {
      for (const prefix of [item.to, ...(item.also ?? [])]) {
        if (matches(pathname, prefix) && (!best || prefix.length > best.length)) best = { group, item, length: prefix.length };
      }
    }
  }
  return best ? { group: best.group, item: best.item } : null;
}

/** Every destination, flattened, for the command palette. */
export function destinations(): { to: string; label: string; section: string; icon: IconName; keywords: string }[] {
  return NAV.flatMap((group) =>
    group.items.length === 0
      ? [{ to: group.to ?? "/", label: group.title, section: "", icon: group.icon, keywords: "" }]
      : group.items.map((item) => ({ to: item.to, label: item.label, section: group.items.length > 1 ? group.title : "", icon: item.icon, keywords: item.keywords ?? "" })),
  ).concat([
    { to: "/ai", label: "AI workspace (Control Room)", section: "Intelligence", icon: "bot", keywords: "ask ai plan run canvas" },
    { to: "/dashboard", label: "Workspace overview", section: "Analytics", icon: "chart", keywords: "dashboard" },
    { to: "/postings", label: "Jobs", section: "Intelligence", icon: "briefcase", keywords: "postings openings" },
    { to: "/segments", label: "Segments", section: "CRM", icon: "filter", keywords: "saved filters" },
    { to: "/suppressions", label: "Suppression list", section: "GTM", icon: "block", keywords: "unsubscribe bounce do not contact blocklist compliance" },
    { to: "/notifications", label: "Notifications", section: "", icon: "bell", keywords: "alerts inbox" },
    { to: "/analytics?tab=campaigns", label: "Campaign analytics", section: "Analytics", icon: "chart", keywords: "attribution performance funnel" },
    { to: "/credits", label: "Credits", section: "Admin", icon: "coin", keywords: "billing usage providers" },
    { to: "/background", label: "Background jobs", section: "Admin", icon: "clock", keywords: "tasks queue" },
    { to: "/ai/memory", label: "AI memory", section: "Admin", icon: "bot", keywords: "aliases preferences" },
    { to: "/jobs", label: "Crawls", section: "Admin", icon: "scraper", keywords: "careers crawler" },
    // Task shortcuts: common jobs by what people want to do, not by page name.
    { to: "/email-validation", label: "Validate a file of emails", section: "Do", icon: "mailcheck", keywords: "upload csv xlsx verify clean list" },
    { to: "/internal-data", label: "Import a batch of files", section: "Do", icon: "layers", keywords: "internal data multi-file merge" },
    { to: "/settings/sending", label: "Connect a sending mailbox", section: "Do", icon: "mail", keywords: "gmail outlook smtp sender" },
    { to: "/settings/users", label: "Invite a teammate", section: "Do", icon: "users", keywords: "add user member role" },
    { to: "/scraper", label: "Scrape a website", section: "Do", icon: "scraper", keywords: "extract jobs contacts companies ai" },
    { to: "/sequences", label: "Build a follow-up sequence", section: "Do", icon: "repeat", keywords: "cadence day 1 day 3 steps" },
  ]);
}
