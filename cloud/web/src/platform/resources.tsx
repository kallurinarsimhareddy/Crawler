// Page configurations for the config-driven resources.

import type { ResourceConfig } from "./ResourcePage";
import { Pill, Score, Tags, fmtDate } from "./ui";

export const TASKS: ResourceConfig = {
  title: "Tasks",
  subtitle: "Follow-ups and work items across companies, contacts and opportunities.",
  path: "/crm-tasks",
  columns: [
    { key: "title", label: "Task" },
    { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
    { key: "priority", label: "Priority", render: (r) => <Pill value={r.priority} /> },
    { key: "due_at", label: "Due", render: (r) => fmtDate(r.due_at) },
    { key: "source", label: "Source" },
  ],
  filters: [
    { key: "status", label: "Status", options: ["open", "in_progress", "done", "cancelled"] },
    { key: "priority", label: "Priority", options: ["low", "normal", "high", "urgent"] },
  ],
  create: [
    { key: "title", label: "Title", required: true },
    { key: "priority", label: "Priority", type: "select", options: ["low", "normal", "high", "urgent"] },
    { key: "due_at", label: "Due date", type: "date" },
    { key: "company_id", label: "Company id", placeholder: "co_…" },
    { key: "description", label: "Description", type: "textarea" },
  ],
  createLabel: "New task",
  emptyHint: "Create follow-ups for companies, contacts and deals so nothing slips.",
};

export const ACTIVITIES: ResourceConfig = {
  title: "Activities",
  subtitle: "The timeline of everything that happened: imports, calls, emails, stage changes, signals.",
  path: "/activities",
  columns: [
    { key: "summary", label: "Activity" },
    { key: "kind", label: "Kind", render: (r) => <Pill value={r.kind} /> },
    { key: "occurred_at", label: "When", render: (r) => fmtDate(r.occurred_at) },
    { key: "company_id", label: "Company", className: "mono small" },
  ],
  filters: [{ key: "kind", label: "Kind", placeholder: "kind (e.g. call)" }],
  create: [
    { key: "kind", label: "Kind", required: true, placeholder: "call, meeting, email, note…" },
    { key: "summary", label: "Summary", required: true },
    { key: "company_id", label: "Company id" },
    { key: "contact_id", label: "Contact id" },
  ],
  createLabel: "Log activity",
  emptyHint: "Log calls, meetings, emails and notes to build a timeline for every account.",
};

export const CAMPAIGNS: ResourceConfig = {
  title: "Campaigns",
  subtitle: "Private GTM campaigns for this workspace. Nothing is ever sent without an explicit, approved campaign action.",
  path: "/campaigns",
  columns: [
    { key: "name", label: "Campaign" },
    { key: "brand", label: "Brand" },
    { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
    { key: "focus_keywords", label: "Focus", render: (r) => <Tags values={r.focus_keywords} /> },
    { key: "signal_types", label: "Signals", render: (r) => <Tags values={r.signal_types} /> },
    { key: "sending_enabled", label: "Sending", render: (r) => (r.sending_enabled ? "Enabled" : "Off") },
  ],
  create: [
    { key: "key", label: "Key", required: true, placeholder: "short-unique-key" },
    { key: "name", label: "Name", required: true },
    { key: "brand", label: "Brand" },
    { key: "focus_keywords", label: "Focus keywords", type: "tags", placeholder: "comma separated" },
    { key: "technologies", label: "Technologies", type: "tags" },
    { key: "signal_types", label: "Signal types", type: "tags" },
    { key: "target_titles", label: "Target titles", type: "tags" },
  ],
  createLabel: "New campaign",
  emptyHint: "Group target accounts around a message, then add sequences and templates.",
};

export const SEQUENCES: ResourceConfig = {
  title: "Sequences",
  subtitle: "Multi-step outreach. Enrollments wait for approval; development and staging never send email.",
  path: "/sequences",
  columns: [
    { key: "name", label: "Sequence" },
    { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
    { key: "campaign_id", label: "Campaign", className: "mono small" },
    { key: "stop_on_reply", label: "Stops on reply" },
  ],
  create: [
    { key: "name", label: "Name", required: true },
    { key: "campaign_id", label: "Campaign id" },
    { key: "description", label: "Description", type: "textarea" },
  ],
  createLabel: "New sequence",
  emptyHint: "Build multi-step outreach; every enrollment waits for your approval.",
};

export const TEMPLATES: ResourceConfig = {
  title: "Email templates",
  subtitle: "Use {{company.name}}, {{contact.first_name}}, {{signal.summary}}… Missing variables block sending.",
  path: "/templates",
  columns: [
    { key: "name", label: "Template" },
    { key: "subject", label: "Subject" },
    { key: "variables", label: "Variables", render: (r) => <Tags values={r.variables} /> },
  ],
  create: [
    { key: "name", label: "Name", required: true },
    { key: "subject", label: "Subject", required: true },
    { key: "body", label: "Body", type: "textarea", required: true },
  ],
  createLabel: "New template",
  emptyHint: "Write reusable emails with {{company.name}} and {{contact.first_name}} variables.",
};

export const LISTS: ResourceConfig = {
  title: "Lists",
  subtitle: "Static lists of companies, contacts, jobs or opportunities.",
  path: "/lists",
  columns: [
    { key: "name", label: "List" },
    { key: "entity_type", label: "Of", render: (r) => <Pill value={r.entity_type} /> },
    { key: "member_count", label: "Members" },
    { key: "source", label: "Source" },
    { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
  ],
  create: [
    { key: "name", label: "Name", required: true },
    { key: "entity_type", label: "Entity", type: "select", options: ["companies", "contacts", "job_postings", "opportunities"], required: true },
    { key: "description", label: "Description", type: "textarea" },
  ],
  createLabel: "New list",
  emptyHint: "Save companies, contacts or jobs into a list to research, enrich or target them.",
  link: (r) => `/lists/${r.id}`,
};

export const SEGMENTS: ResourceConfig = {
  title: "Segments",
  subtitle: "Saved filters that stay current as data changes.",
  path: "/segments",
  columns: [
    { key: "name", label: "Segment" },
    { key: "entity_type", label: "Of", render: (r) => <Pill value={r.entity_type} /> },
    { key: "filters", label: "Filters", render: (r) => <code className="small">{JSON.stringify(r.filters)}</code> },
  ],
  create: [
    { key: "name", label: "Name", required: true },
    { key: "entity_type", label: "Entity", type: "select", options: ["companies", "contacts", "job_postings", "opportunities"], required: true },
    { key: "filters", label: "Filters (JSON)", type: "json", placeholder: '{"industry": "Manufacturing", "account_score__gte": 60}' },
  ],
  createLabel: "New segment",
  emptyHint: "Save a filter once; the segment stays current as your data changes.",
};

// Workflows are edited in the visual automation builder (controlroom/Automation.tsx).

export const MONITORS: ResourceConfig = {
  title: "Monitors",
  subtitle: "Daily, weekly or monthly change detection for companies and lists.",
  path: "/monitors",
  columns: [
    { key: "name", label: "Monitor" },
    { key: "target_type", label: "Target", render: (r) => <Pill value={r.target_type} /> },
    { key: "frequency", label: "Frequency" },
    { key: "enabled", label: "Enabled" },
    { key: "next_run_at", label: "Next run", render: (r) => fmtDate(r.next_run_at) },
  ],
  create: [
    { key: "name", label: "Name", required: true },
    { key: "target_type", label: "Target type", type: "select", options: ["company", "list", "segment"], required: true },
    { key: "target_id", label: "Target id", required: true },
    { key: "frequency", label: "Frequency", type: "select", options: ["daily", "weekly", "monthly"], required: true },
  ],
  createLabel: "New monitor",
  emptyHint: "Track changes across companies, lists or searches.",
};

export const SUPPRESSIONS: ResourceConfig = {
  title: "Suppression list",
  subtitle: "Addresses and domains that must never be contacted. Bounces and unsubscribes are added automatically.",
  path: "/suppressions",
  columns: [
    { key: "value", label: "Value" },
    { key: "kind", label: "Kind", render: (r) => <Pill value={r.kind} /> },
    { key: "reason", label: "Reason", render: (r) => <Pill value={r.reason} /> },
    { key: "created_at", label: "Added", render: (r) => fmtDate(r.created_at) },
  ],
  create: [
    { key: "value", label: "Email or domain", required: true },
    { key: "kind", label: "Kind", type: "select", options: ["email", "domain"], required: true },
    { key: "reason", label: "Reason", type: "select", options: ["manual", "unsubscribe", "bounce", "complaint", "legal", "customer"], required: true },
  ],
  createLabel: "Suppress",
  emptyHint: "Add emails or domains that must never be contacted.",
};

export const OPPORTUNITY_COLUMNS = [
  { key: "title", label: "Opportunity" },
  { key: "status", label: "Status", render: (r: Record<string, unknown>) => <Pill value={r.status} /> },
  { key: "score", label: "Score", render: (r: Record<string, unknown>) => <Score value={r.score} /> },
  { key: "signal_types", label: "Signals", render: (r: Record<string, unknown>) => <Tags values={r.signal_types} /> },
  { key: "next_action", label: "Next action" },
];
