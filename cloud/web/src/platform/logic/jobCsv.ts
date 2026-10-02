// Pure helpers for the Jobs CSV upload / download screens (unit-tested).

import type { JobFilters } from "./jobFilters";

export type ExportScope = "current" | "all";

export interface ExportRecord {
  id: string;
  status?: string;
  scope?: string;
  total_rows?: number | null;
  progress_rows?: number | null;
  row_count?: number | null;
  filename?: string | null;
  available?: boolean;
  error?: string | null;
  created_at?: string;
  finished_at?: string | null;
  deduplicated?: boolean;
}

/** The filters an export sends: the Jobs page filters (without paging) plus the advanced tree. */
export function exportParams(filters: JobFilters, conditions?: unknown): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (key === "order" || key === "limit" || key === "offset") continue;
    if (value !== undefined && value !== null && value !== "") params[key] = value;
  }
  if (conditions) params.conditions = conditions;
  return params;
}

/** How many rows "Current results" holds: the visible page of the matching jobs. */
export function currentRowCount(total: number | null | undefined, page: { limit: number; offset: number }): number {
  if (typeof total !== "number" || total <= 0) return 0;
  return Math.max(0, Math.min(page.limit, total - page.offset));
}

export function exportPercent(record: ExportRecord | null | undefined): number {
  if (!record) return 0;
  if (record.status === "completed") return 100;
  const total = Number(record.total_rows ?? 0);
  const done = Number(record.progress_rows ?? 0);
  if (total <= 0) return 0;
  return Math.max(0, Math.min(99, Math.floor((done / total) * 100)));
}

export function exportFinished(record: ExportRecord | null | undefined): boolean {
  return Boolean(record && (record.status === "completed" || record.status === "failed"));
}

export function exportFilename(record: ExportRecord): string {
  return record.filename || `jobs-${record.scope ?? "export"}.csv`;
}

/** Import counters, in the order the summary shows them. */
export const IMPORT_COUNTERS: readonly [string, string][] = [
  ["rows", "Rows read"],
  ["new", "New jobs"],
  ["updated", "Updated jobs"],
  ["unchanged", "Unchanged"],
  ["duplicates", "Duplicates"],
  ["rejected", "Rejected"],
  ["errors", "Errors"],
];

export function importPercent(stats: Record<string, unknown> | null | undefined, rowCount: number, atRow: number,
                              status: string): number {
  if (status === "completed") return 100;
  const stored = Number(stats?.percent);
  if (Number.isFinite(stored) && stored > 0) return Math.min(99.9, stored);
  return rowCount > 0 ? Math.min(99.9, Math.round((atRow / rowCount) * 1000) / 10) : 0;
}
