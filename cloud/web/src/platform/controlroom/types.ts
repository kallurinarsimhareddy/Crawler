// Shapes returned by /api/v1/w/{ws}/agent/* (cloud/intel/api/routes_agent.py).

export type RunStatus = "planned" | "running" | "awaiting_approval" | "completed" | "failed" | "cancelled";

export interface PlanStep {
  id: string;
  position: number;
  tool: string;
  title?: string | null;
  why?: string | null;
  risk: string;
  requires_approval: boolean;
  status: string;
  credits: Record<string, number>;
  affected?: number | null;
  explain?: string | null;
  detail?: string | null;
  count?: number | null;
  params: Record<string, unknown>;
}

export interface Approval {
  id: string;
  run_id: string;
  step_id: string;
  action: string;
  reason?: string | null;
  risk: string;
  impact: { affected?: number; tool?: string };
  credits: Record<string, number>;
  status: "pending" | "approved" | "rejected" | "expired";
  decided_by?: string | null;
  decided_at?: string | null;
}

export interface ProgressLine {
  ok?: boolean;
  text: string;
}

export interface Estimate {
  counts?: Record<string, number>;
  credits?: Record<string, number>;
  explain?: string[];
  expected?: string;
  note?: string;
}

export interface AgentRun {
  id: string;
  session_id?: string | null;
  request: string;
  mode: string;
  status: RunStatus;
  planner?: string | null;
  intent?: { kind?: string; aliases_applied?: { alias: string; expands_to: string[] }[]; notes?: string[] };
  plan: PlanStep[];
  estimate?: Estimate;
  summary?: string | null;
  error?: string | null;
  progress?: { lines?: ProgressLine[]; message?: string; counts?: Record<string, number> };
  result?: { counts?: Record<string, number>; credits_used?: Record<string, number>; exports?: string[] };
  approvals?: Approval[];
  created_at?: string;
}

export interface ReasonCode {
  points: number;
  label: string;
  code: string;
  evidence?: Record<string, unknown>;
}

export interface ScoreCard {
  score?: number | null;
  reasons: ReasonCode[];
}

export interface EvidenceItem {
  step?: string;
  reason?: string;
  source?: string;
  evidence_url?: string;
  url?: string;
  job_posting_id?: string;
  signal_id?: string;
  detected_at?: string;
  observed_at?: string;
  first_seen?: string;
  [key: string]: unknown;
}

export interface CompanyResult {
  id: string;
  rank: number;
  entity_id: string;
  title: string;
  score?: number | null;
  reasons: ReasonCode[];
  evidence: EvidenceItem[];
  data: {
    name?: string;
    domain?: string;
    industry?: string;
    country?: string;
    state?: string;
    technologies?: string[];
    lifecycle?: string;
    scores?: Record<string, ScoreCard>;
    signals?: { id: string; type: string; summary?: string }[];
    jobs?: { id: string; title: string; url?: string; first_seen?: string }[];
    contact_gap?: Record<string, string>;
    contacts?: { id: string; name: string; title?: string; email?: string; email_status?: string }[];
    campaign?: { id?: string; key?: string; name?: string } | null;
  };
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  run_id?: string | null;
  created_at?: string;
}

export interface Session {
  id: string;
  title: string;
  mode: string;
  status: string;
  messages?: Message[];
  last_run_id?: string | null;
}

export interface AskResponse {
  session: Session;
  run: AgentRun | null;
  message: Message;
}

export interface ModeInfo {
  key: string;
  title: string;
  description: string;
  tools: string[];
}

export interface ToolInfo {
  name: string;
  description: string;
  risk: string;
  approval: string;
  input_schema: Record<string, unknown>;
  allowed_for_you?: boolean;
}

export const TERMINAL: RunStatus[] = ["completed", "failed", "cancelled"];

export function creditsText(credits: Record<string, number> | undefined): string {
  const entries = Object.entries(credits ?? {}).filter(([, v]) => v);
  return entries.length ? entries.map(([k, v]) => `${v.toLocaleString()} ${k}`).join(", ") : "";
}

/** Ask the prompt box on the Control Room to take focus (the sidebar button and Ctrl/Cmd+K use this). */
export const ASK_EVENT = "careercrawler:ask";

export function requestAskFocus(prefill?: string): void {
  window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { prefill } }));
}
