// Pure helpers for the Internal Data workflow and the scraper → GTM bridge.
// No JSX and no runtime imports, so `npm test` (node --test) runs them directly.

export type ColumnStatus = "confident" | "ambiguous" | "unmapped";

export interface ReviewColumn {
  column: string;
  suggestion: string | null;
  confidence: number;
  status: ColumnStatus;
  reasons: string[];
  alternatives: string[];
  type_hint?: string | null;
  files_present?: number | null;
  samples?: string[];
}

/** A column's decision: a target field, null ("not mapped"), or undefined (not decided yet). */
export type Decisions = Record<string, string | null | undefined>;

/** Confident suggestions are pre-filled for the person to confirm; ambiguous ones stay undecided. */
export function initialDecisions(columns: ReviewColumn[], current: Record<string, string | null> = {}): Decisions {
  const out: Decisions = {};
  for (const c of columns) {
    if (c.column in current) out[c.column] = current[c.column];
    else if (c.status === "confident") out[c.column] = c.suggestion;
    else if (c.status === "unmapped") out[c.column] = null;
    else out[c.column] = undefined;
  }
  return out;
}

/** Ambiguous columns without an explicit choice: the mapping cannot be saved while any remain. */
export function undecided(columns: ReviewColumn[], decisions: Decisions): string[] {
  return columns.filter((c) => c.status === "ambiguous" && decisions[c.column] === undefined).map((c) => c.column);
}

/** The explicit mapping sent to the API: every decided column, including "not mapped" (null). */
export function buildMapping(decisions: Decisions): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const [column, target] of Object.entries(decisions)) {
    if (target !== undefined) out[column] = target;
  }
  return out;
}

/** Two columns in the mapping pointing at one single-valued field (allowed across files, flagged for review). */
export function sharedTargets(decisions: Decisions, listFields: string[] = ["technologies", "tags", "aliases", "sic_codes", "naics_codes"]): Record<string, string[]> {
  const byTarget: Record<string, string[]> = {};
  for (const [column, target] of Object.entries(decisions)) {
    if (!target) continue;
    const field = target.split(".", 2)[1] ?? target;
    if (listFields.includes(field)) continue;
    (byTarget[target] ??= []).push(column);
  }
  return Object.fromEntries(Object.entries(byTarget).filter(([, cols]) => cols.length > 1));
}

export interface SchemaFile {
  file_id: string;
  filename: string;
  present: string[];
}

export interface SchemaColumn {
  key: string;
  column: string;
  files_present: number;
  files_total: number;
  type_hint: string;
  variants: string[];
}

/** Rows for the column × file matrix: which file has which column (matched case/punctuation-insensitively). */
export function schemaMatrix(columns: SchemaColumn[], files: SchemaFile[]): { column: string; hint: string; cells: boolean[]; coverage: string }[] {
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  const present = files.map((f) => new Set(f.present.map(norm)));
  return columns.map((c) => ({
    column: c.column,
    hint: c.type_hint,
    cells: present.map((set) => set.has(c.key)),
    coverage: `${c.files_present}/${c.files_total}`,
  }));
}

export type ConflictChoice = "keep" | "take_new" | { value: string };

/** The resolve payload: only fields the person decided. A manual value must not be blank. */
export function conflictPayload(choices: Record<string, ConflictChoice | undefined>): { decisions: Record<string, ConflictChoice>; invalid: string[] } {
  const decisions: Record<string, ConflictChoice> = {};
  const invalid: string[] = [];
  for (const [field, choice] of Object.entries(choices)) {
    if (choice === undefined) continue;
    if (typeof choice === "object" && !choice.value.trim()) {
      invalid.push(field);
      continue;
    }
    decisions[field] = choice;
  }
  return { decisions, invalid };
}

/** One line describing a bridge preview. */
export function previewLine(summary: Record<string, number>): string {
  const n = (k: string) => summary[k] ?? 0;
  return `${n("companies")} companies (${n("companies_existing")} in CRM, ${n("companies_new")} new) · ${n("contacts")} emails (${n("contacts_existing")} known) · ${n("duplicates_removed")} duplicates removed`;
}

/** Where to go after a bridge action, from its result. */
export function resultLink(action: string, result: Record<string, unknown>): { to: string; label: string } | null {
  const id = (key: string) => {
    const v = result[key] as Record<string, unknown> | null | undefined;
    return v && typeof v === "object" && typeof v.id === "string" ? v.id : null;
  };
  if (action === "add-to-list" && id("list")) return { to: `/lists/${id("list")}`, label: "Open list" };
  if (action === "create-campaign" && id("campaign")) return { to: `/campaigns/${id("campaign")}`, label: "Open draft campaign" };
  if (action === "validate-emails" && id("job")) return { to: `/email-validation/${id("job")}`, label: "Open validation job" };
  if (action === "validate-emails" && id("task")) return { to: "/background", label: "Open background job" };
  if (action === "research-these" && id("run")) return { to: `/research/${id("run")}`, label: "Review research plan" };
  if (action === "create-crm-proposal" && typeof result.review === "string") return { to: result.review, label: "Review proposals" };
  return null;
}
