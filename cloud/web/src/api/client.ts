import type { Health, Job, JobCreated, JobCreateRequest, JobList, JobStatus } from "./types";

const BASE_URL = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

// FastAPI answers 422 with a list of issues and everything else with a string.
// Turn either into one sentence a person can act on.
function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return (detail as ValidationIssue[])
      .map((issue) => {
        const field = (issue.loc ?? [])
          .filter((part) => part !== "body" && !["single_company", "bulk_companies", "weekly_crawl", "discovery"].includes(String(part)))
          .join(" › ");
        const msg = (issue.msg ?? "is invalid").replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  return `Request failed (${status})`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError("Cannot reach the CareerCloud API. Is it running?", 0);
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) throw new ApiError(describeError(response.status, body), response.status);
  return body as T;
}

export const api = {
  health: (signal?: AbortSignal) => request<Health>("/api/v1/health", { signal }),

  listJobs: (params: { status?: JobStatus; limit?: number; offset?: number } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<JobList>(`/api/v1/jobs${suffix}`, { signal });
  },

  getJob: (jobId: string, signal?: AbortSignal) =>
    request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, { signal }),

  createJob: (payload: JobCreateRequest) =>
    request<JobCreated>("/api/v1/jobs", { method: "POST", body: JSON.stringify(payload) }),

  cancelJob: (jobId: string) =>
    request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
};
