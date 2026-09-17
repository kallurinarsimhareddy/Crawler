import type {
  DevSession,
  Health,
  Job,
  JobCreated,
  JobCreateRequest,
  JobEvent,
  JobList,
  JobStatus,
  Me,
  ResultFile,
  TargetRecord,
} from "./types";

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

// The auth provider registers how to get the current access token and what to
// do when the API says it is no longer valid. The client never stores tokens.
let tokenProvider: () => Promise<string | null> = async () => null;
let onUnauthorized: () => void = () => {};

export function configureAuth(provider: () => Promise<string | null>, unauthorized: () => void): void {
  tokenProvider = provider;
  onUnauthorized = unauthorized;
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

const TYPE_SEGMENTS = new Set(["single_company", "bulk_companies", "weekly_crawl", "discovery"]);

// FastAPI answers 422 with a list of issues and everything else with a string.
// Turn either into one sentence a person can act on.
function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return (detail as ValidationIssue[])
      .map((issue) => {
        const field = (issue.loc ?? []).filter((part) => part !== "body" && !TYPE_SEGMENTS.has(String(part))).join(" › ");
        const msg = (issue.msg ?? "is invalid").replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  return `Request failed (${status})`;
}

async function send(path: string, init: RequestInit = {}, authenticated = true): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (authenticated) {
    const token = await tokenProvider();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, { ...init, headers: { ...headers, ...(init.headers as Record<string, string>) } });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError("Cannot reach the CareerCloud API. Is it running?", 0);
  }
  if (response.status === 401 && authenticated) onUnauthorized();
  return response;
}

async function request<T>(path: string, init: RequestInit = {}, authenticated = true): Promise<T> {
  const response = await send(path, init, authenticated);
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
  health: (signal?: AbortSignal) => request<Health>("/api/v1/health", { signal }, false),

  me: (signal?: AbortSignal) => request<Me>("/api/v1/me", { signal }),

  devSession: (email: string) =>
    request<DevSession>("/api/v1/auth/dev-session", { method: "POST", body: JSON.stringify({ email }) }, false),

  listJobs: (params: { status?: JobStatus; limit?: number; offset?: number } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<JobList>(`/api/v1/jobs${suffix}`, { signal });
  },

  getJob: (jobId: string, signal?: AbortSignal) => request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, { signal }),

  listTargets: (jobId: string, signal?: AbortSignal) =>
    request<{ targets: TargetRecord[] }>(`/api/v1/jobs/${encodeURIComponent(jobId)}/targets`, { signal }),

  listEvents: (jobId: string, signal?: AbortSignal) =>
    request<{ events: JobEvent[] }>(`/api/v1/jobs/${encodeURIComponent(jobId)}/events`, { signal }),

  listResults: (jobId: string, signal?: AbortSignal) =>
    request<{ results: ResultFile[] }>(`/api/v1/jobs/${encodeURIComponent(jobId)}/results`, { signal }),

  createJob: (payload: JobCreateRequest) =>
    request<JobCreated>("/api/v1/jobs", { method: "POST", body: JSON.stringify(payload) }),

  cancelJob: (jobId: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),

  /** Downloads go through fetch so the access token is sent as a header, never in a URL. */
  async downloadResult(jobId: string, result: ResultFile): Promise<void> {
    const response = await send(result.download_url, {});
    if (!response.ok) {
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      throw new ApiError(describeError(response.status, body), response.status);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${jobId}-${result.filename}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};
