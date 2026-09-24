// The platform API (cloud/intel): workspace-scoped REST under /api/v1/w/{workspace}.
// Every call goes through the shared client, so the access token travels as a
// header and never appears in a URL — downloads included.

import { ApiError, describeError, request, send } from "../api/client";

export type Row = Record<string, unknown> & { id: string };

export interface PageOf<T = Row> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  role: string;
  ai_external_allowed?: boolean;
}

export type Query = Record<string, string | number | boolean | undefined | null>;

function qs(query: Query = {}, path = ""): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  if (!text) return "";
  return path.includes("?") ? `&${text}` : `?${text}`;
}

export const platform = {
  workspaces: () => request<{ items: Workspace[] }>("/api/v1/workspaces"),
  createWorkspace: (name: string) =>
    request<Workspace>("/api/v1/workspaces", { method: "POST", body: JSON.stringify({ name }) }),
};

/** A client bound to one workspace. Paths are relative to /api/v1/w/{id}. */
export function ws(workspaceId: string) {
  const base = `/api/v1/w/${encodeURIComponent(workspaceId)}`;
  return {
    base,
    get: <T = unknown>(path: string, query?: Query, signal?: AbortSignal) =>
      request<T>(`${base}${path}${qs(query, path)}`, { signal }),
    list: <T = Row>(path: string, query?: Query, signal?: AbortSignal) =>
      request<PageOf<T>>(`${base}${path}${qs(query, path)}`, { signal }),
    post: <T = unknown>(path: string, body?: unknown, idempotencyKey?: string) =>
      request<T>(`${base}${path}`, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
        headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      }),
    put: <T = unknown>(path: string, body: unknown) =>
      request<T>(`${base}${path}`, { method: "PUT", body: JSON.stringify(body) }),
    patch: <T = unknown>(path: string, changes: Record<string, unknown>, expectedVersion?: number) =>
      request<T>(`${base}${path}`, {
        method: "PATCH",
        body: JSON.stringify({ changes, expected_version: expectedVersion }),
      }),
    del: (path: string) => request<null>(`${base}${path}`, { method: "DELETE" }),
    upload: <T = unknown>(path: string, files: File[], fields: Record<string, string> = {}) => {
      const form = new FormData();
      for (const file of files) form.append("files", file, file.name);
      for (const [key, value] of Object.entries(fields)) form.append(key, value);
      return request<T>(`${base}${path}`, { method: "POST", body: form });
    },
    async download(path: string, filename: string): Promise<void> {
      const response = await send(`${base}${path}`, {});
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
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  };
}

export type WsClient = ReturnType<typeof ws>;

export function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
