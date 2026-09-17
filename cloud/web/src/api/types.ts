// Mirrors cloud/shared/schemas.py. Keep the two in step.

export const JOB_TYPES = ["single_company", "bulk_companies", "weekly_crawl", "discovery"] as const;
export type JobType = (typeof JOB_TYPES)[number];

export const JOB_STATUSES = ["queued", "running", "completed", "failed", "cancelled"] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set(["completed", "failed", "cancelled"]);

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

export interface Health {
  status: "ok";
  service: string;
  version: string;
  environment: string;
  runner: string;
  storage: string;
}

export type JobCreateRequest =
  | ({ type: "single_company" } & CompanyInput)
  | ({ type: "discovery" } & CompanyInput)
  | { type: "bulk_companies"; companies: CompanyInput[] }
  | { type: "weekly_crawl" };
