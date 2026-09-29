// Pure helpers shared by pages and covered by `npm test` (node --test). Logic
// modules under platform/logic/ must stay free of JSX and runtime imports so
// Node can run them directly with type stripping.

/** "12.5%" for a 0..1 ratio; "—" when there is nothing to divide. */
export function percent(part: number, whole: number, digits = 1): string {
  if (!whole || !Number.isFinite(part) || !Number.isFinite(whole)) return "—";
  return `${((part / whole) * 100).toFixed(digits)}%`;
}

/** Human file size: 999 B, 1.2 KB, 3.4 MB. */
export function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
