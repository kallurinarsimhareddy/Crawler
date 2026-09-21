// Mirrors cloud/shared/schemas.py. Keep the two in step.

export const JOB_TYPES = ["single_company", "bulk_companies", "weekly_crawl", "discovery"] as const;
export type JobType = (typeof JOB_TYPES)[number];

export const JOB_STATUSES = ["queued", "running", "completed", "failed", "cancelled"] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set(["completed", "failed", "cancelled"]);

export type TargetStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface CompanyTarget {
  website: string | null;
  company_name: string | null;
}

export interface CompanyInput {
  website?: string;
  company_name?: string;
}

export interface JobProgress {
  completed: number;
  total: number | null;
  message: string | null;
  failed: number;
  jobs_found: number;
  current_company: string | null;
  current_phase: string | null;
}

export interface Job {
  job_id: string;
  type: JobType;
  status: JobStatus;
  target: string;
  targets: CompanyTarget[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  progress: JobProgress;
  cancel_requested: boolean;
  attempts: number;
  max_attempts: number;
  elapsed_seconds: number | null;
  runnable: boolean;
}

export interface JobList {
  jobs: Job[];
  total: number;
  counts: Record<JobStatus, number>;
}

export interface JobCreated {
  job_id: string;
  status: JobStatus;
}

export interface TargetRecord {
  position: number;
  website: string | null;
  company_name: string | null;
  status: TargetStatus;
  platform: string | null;
  outcome: string | null;
  jobs_found: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobEvent {
  kind: string;
  created_at: string;
  attempt: number | null;
  message: string | null;
}

export type ResultKind = "summary_json" | "jobs_csv" | "jobs_xlsx" | "crawl_log";

export interface ResultFile {
  result_id: string;
  kind: ResultKind;
  filename: string;
  content_type: string;
  size_bytes: number;
  row_count: number | null;
  created_at: string;
  download_url: string;
}

export interface Health {
  status: "ok";
  service: string;
  version: string;
  environment: string;
  runner: string;
  storage: string;
  queue: string | null;
  auth: string | null;
}

export type ComponentState = "ok" | "down" | "disabled";

export interface ComponentStatus {
  status: ComponentState;
  backend: string | null;
  detail: string | null;
  latency_ms: number | null;
}

export interface QueueDepth {
  ready: number;
  delayed: number;
  in_flight: number;
}

export interface WorkerStatus {
  online: boolean;
  count: number;
  last_heartbeat: string | null;
  seconds_since_heartbeat: number | null;
  stale_after_seconds: number;
  message: string;
}

/** `/status`: the operator view. Needs a signed-in caller, unlike `/health`. */
export interface Status {
  status: "ok" | "degraded";
  service: string;
  version: string;
  environment: string;
  checked_at: string;
  api: ComponentStatus;
  database: ComponentStatus;
  redis: ComponentStatus;
  queue: QueueDepth;
  worker: WorkerStatus;
}

export interface Me {
  user_id: string;
  email: string | null;
  auth_mode: string;
}

export interface DevSession {
  access_token: string;
  expires_in: number;
  user_id: string;
  email: string;
}

export type JobCreateRequest =
  | ({ type: "single_company" } & CompanyInput)
  | ({ type: "discovery" } & CompanyInput)
  | { type: "bulk_companies"; companies: CompanyInput[] }
  | { type: "weekly_crawl" };
