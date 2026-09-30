// Internal Data: 12–30 CSV/XLSX files as one batch → schema comparison → explicit
// mapping (ambiguous columns must be decided) → merge with provenance → review of
// values that disagree with the CRM → import history.

import { useRef, useState, type DragEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { fileSize } from "../logic/format";
import {
  buildMapping,
  conflictPayload,
  initialDecisions,
  schemaMatrix,
  sharedTargets,
  undecided,
  type ConflictChoice,
  type Decisions,
  type ReviewColumn,
  type SchemaColumn,
  type SchemaFile,
} from "../logic/internalData";
import "../styles/internalData.css";
import { DataTable, PageHeader, Pill, Stat, Tabs, fmt, fmtDate, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

const TARGETS = [
  { value: "companies_and_contacts", label: "Companies and contacts" },
  { value: "companies", label: "Companies" },
  { value: "contacts", label: "Contacts" },
];
const ACCEPT = ".csv,.tsv,.txt,.xlsx,.xlsm";

export function InternalData() {
  const [params, setParams] = useSearchParams();
  const batchId = params.get("batch");
  const open = (id: string | null) => setParams(id ? { batch: id } : {});
  return batchId ? <BatchView batchId={batchId} onBack={() => open(null)} /> : <BatchHome onOpen={open} />;
}

// --- uploading --------------------------------------------------------------------------

function FilePicker({ files, onChange }: { files: File[]; onChange: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const add = (list: FileList | null) => {
    if (!list) return;
    const next = [...files];
    for (const f of Array.from(list)) if (!next.some((x) => x.name === f.name && x.size === f.size)) next.push(f);
    onChange(next.slice(0, 30));
  };
  const drop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    add(e.dataTransfer.files);
  };
  return (
    <div>
      <div
        className={`id-drop${over ? " id-drop--over" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
      >
        <p><strong>Drop 1–30 CSV or XLSX files here</strong>, or click to browse</p>
        <p className="small">Up to 50 MB each. Files that cannot be read are reported by name; the rest still import.</p>
        <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      </div>
      {files.length > 0 && (
        <ul className="id-files">
          {files.map((f) => (
            <li key={f.name + f.size}>
              <span>{f.name}</span>
              <span className="muted">
                {fileSize(f.size)}{" "}
                <button type="button" className="button button--ghost button--small" onClick={() => onChange(files.filter((x) => x !== f))}>Remove</button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UploadResult({ result }: { result: Row | null }) {
  if (!result) return null;
  const failed = (result.failed as Row[] | undefined) ?? [];
  const added = (result.added as Row[] | undefined) ?? [];
  return (
    <div className={`alert ${failed.length ? "alert--warning" : "alert--info"}`} role="status">
      {added.length} file(s) added.
      {failed.map((f) => <div key={String(f.filename)}>{String(f.filename)}: {String(f.error)}</div>)}
    </div>
  );
}

// --- home: new batch + history ------------------------------------------------------------

function BatchHome({ onOpen }: { onOpen: (id: string) => void }) {
  const client = useWs();
  const [name, setName] = useState("");
  const [target, setTarget] = useState("companies_and_contacts");
  const [files, setFiles] = useState<File[]>([]);
  const action = useAction();
  const history = useLoad((signal) => client.get<{ items: Row[]; total: number }>("/internal-data/batches", { limit: 50 }, signal), client.base + "internal-history");
  const create = () =>
    action.run(async () => {
      const batch = await client.post<Row>("/internal-data/batches", { name: name.trim() || `Internal data ${new Date().toLocaleDateString()}`, target });
      if (files.length) {
        await client.upload(`/internal-data/batches/${batch.id}/files`, files);
        await client.post(`/internal-data/batches/${batch.id}/schema`);
      }
      onOpen(batch.id);
    });
  return (
    <div className="page">
      <PageHeader
        title="Internal Data"
        subtitle="Bring in your own spreadsheets as one batch: compare their columns, map them explicitly, merge with provenance, and review anything that disagrees with the CRM."
      />
      {action.error && <ErrorBanner error={action.error} />}
      <div className="card pad form">
        <h3>New batch</h3>
        <div className="grid-2">
          <label className="field">
            <span className="field__label">Batch name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Q3 account lists" />
          </label>
          <label className="field">
            <span className="field__label">The files hold</span>
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
              {TARGETS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </label>
        </div>
        <FilePicker files={files} onChange={setFiles} />
        <div className="form__actions">
          <button type="button" className="button button--primary" disabled={action.busy} onClick={() => void create()}>
            {files.length ? `Create batch with ${files.length} file(s)` : "Create empty batch"}
          </button>
        </div>
      </div>
      <h3>Import history</h3>
      {history.error && <ErrorBanner error={history.error} onRetry={history.refresh} />}
      {history.loading && !history.data ? <Loading /> : (
        <div className="card">
          <DataTable
            rows={(history.data?.items ?? []) as Row[]}
            empty={{ title: "No internal data batches yet", description: "Create a batch above to import your first files." }}
            columns={[
              { key: "name", label: "Batch", render: (r) => <button type="button" className="link--button" onClick={() => onOpen(r.id)}>{String(r.name)}</button> },
              { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
              { key: "file_count", label: "Files", className: "tabular" },
              { key: "row_count", label: "Rows", className: "tabular" },
              { key: "conflict_count", label: "Conflicts", className: "tabular", render: (r) => (Number(r.conflict_count) > 0 ? <Pill value="NEEDS_REVIEW" /> : "0") },
              { key: "created_at", label: "Created", render: (r) => fmtDate(r.created_at) },
            ]}
          />
        </div>
      )}
    </div>
  );
}

// --- one batch ------------------------------------------------------------------------------

type TabKey = "files" | "schema" | "mapping" | "merge";

function BatchView({ batchId, onBack }: { batchId: string; onBack: () => void }) {
  const client = useWs();
  const [tab, setTab] = useState<TabKey>("schema");
  const [tick, setTick] = useState(0);
  const overview = useLoad(
    (signal) => client.get<{ batch: Row; files: Row[]; row_status: Record<string, number> }>(`/internal-data/batches/${batchId}`, undefined, signal),
    `${client.base}internal/${batchId}/${tick}`,
  );
  const refresh = () => setTick((n) => n + 1);
  if (overview.error) return <div className="page"><ErrorBanner error={overview.error} onRetry={overview.refresh} /></div>;
  if (!overview.data) return <div className="page"><Loading /></div>;
  const { batch, files, row_status } = overview.data;
  const stats = (batch.stats ?? {}) as Row;
  return (
    <div className="page">
      <PageHeader
        title={String(batch.name)}
        crumbTitle={String(batch.name)}
        subtitle={`${TARGETS.find((t) => t.value === batch.target)?.label ?? String(batch.target)} · created ${fmtDate(batch.created_at)}`}
        actions={
          <>
            <Pill value={batch.status} />
            <button type="button" className="button button--ghost button--small" onClick={onBack}>All batches</button>
          </>
        }
      />
      <div className="stats">
        <Stat label="Files" value={fmt(batch.file_count)} hint="12–30 per batch supported" />
        <Stat label="Rows" value={fmt(batch.row_count)} />
        <Stat label="Merged" value={fmt(stats.merged ?? row_status.merged ?? 0)} />
        <Stat label="Conflicts to review" value={fmt(batch.conflict_count ?? 0)} />
      </div>
      <div className="card">
        <div className="card__header">
          <Tabs
            tabs={[
              { key: "files", label: "Files", count: files.length },
              { key: "schema", label: "Schema comparison" },
              { key: "mapping", label: "Mapping" },
              { key: "merge", label: "Merge & conflicts", count: Number(batch.conflict_count ?? 0) || undefined },
            ]}
            active={tab}
            onChange={(k) => setTab(k as TabKey)}
          />
        </div>
        {tab === "files" && <FilesTab batch={batch} files={files} onChanged={refresh} />}
        {tab === "schema" && <SchemaTab batch={batch} onChanged={refresh} />}
        {tab === "mapping" && <MappingTab batch={batch} files={files} onSaved={() => { refresh(); setTab("merge"); }} />}
        {tab === "merge" && <MergeTab batch={batch} rowStatus={row_status} onChanged={refresh} />}
      </div>
    </div>
  );
}

function FilesTab({ batch, files, onChanged }: { batch: Row; files: Row[]; onChanged: () => void }) {
  const client = useWs();
  const [picked, setPicked] = useState<File[]>([]);
  const [result, setResult] = useState<Row | null>(null);
  const action = useAction();
  const locked = ["merging", "merged"].includes(String(batch.status));
  return (
    <div className="pad">
      {action.error && <ErrorBanner error={action.error} />}
      <DataTable
        rows={files}
        empty="No files yet"
        columns={[
          { key: "filename", label: "File" },
          { key: "format", label: "Format" },
          { key: "row_count", label: "Rows", className: "tabular" },
          { key: "columns", label: "Columns", render: (r) => fmt((r.columns as unknown[] | undefined)?.length ?? 0) },
          { key: "size_bytes", label: "Size", render: (r) => fileSize(Number(r.size_bytes ?? 0)) },
          { key: "status", label: "Status", render: (r) => <Pill value={r.status} /> },
          { key: "problems", label: "Problems", className: "small", render: (r) => fmt(r.problems) },
        ]}
      />
      {!locked && (
        <>
          <h4>Add files</h4>
          <FilePicker files={picked} onChange={setPicked} />
          <div className="form__actions">
            <button
              type="button"
              className="button button--primary"
              disabled={action.busy || picked.length === 0}
              onClick={() => void action.run(async () => {
                setResult(await client.upload<Row>(`/internal-data/batches/${batch.id}/files`, picked));
                setPicked([]);
                onChanged();
              })}
            >
              Upload {picked.length || ""} file(s)
            </button>
          </div>
          <UploadResult result={result} />
        </>
      )}
    </div>
  );
}

interface SchemaReport {
  file_count: number;
  column_count: number;
  common_columns: string[];
  columns: (SchemaColumn & { type_conflict?: boolean; samples?: string[] })[];
  files: (SchemaFile & { missing: string[]; extra: string[]; rows: number; status: string })[];
  layouts: { signature: string; files: string[] }[];
  note: string;
  compared_at?: string;
}

function SchemaTab({ batch, onChanged }: { batch: Row; onChanged: () => void }) {
  const client = useWs();
  const action = useAction();
  const stored = batch.schema_report as SchemaReport | undefined;
  const [report, setReport] = useState<SchemaReport | null>(stored && stored.columns ? stored : null);
  const compare = () => action.run(async () => { setReport(await client.post<SchemaReport>(`/internal-data/batches/${batch.id}/schema`)); onChanged(); });
  return (
    <div className="pad">
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button type="button" className="button button--primary" disabled={action.busy} onClick={() => void compare()}>{report ? "Compare again" : "Compare file schemas"}</button>
        {report?.compared_at && <span className="muted small">Compared {fmt(report.compared_at)}</span>}
      </div>
      {!report ? <p className="muted">Compare the files to see which columns each one has.</p> : (
        <>
          <p>{report.note}. {report.column_count} distinct column(s); in every file: {report.common_columns.length ? report.common_columns.join(", ") : "none"}.</p>
          <p className="small muted">{report.layouts.length} distinct layout(s): {report.layouts.map((l) => `${l.files.length} file(s)`).join(" · ")}</p>
          <div className="table-wrap">
            <table className="id-matrix">
              <thead>
                <tr>
                  <th>Column</th>
                  <th>Type</th>
                  <th>Files</th>
                  {report.files.map((f) => <th key={f.file_id} title={f.filename}><span className="id-matrix__file">{f.filename}</span></th>)}
                </tr>
              </thead>
              <tbody>
                {schemaMatrix(report.columns, report.files).map((row, i) => (
                  <tr key={row.column}>
                    <td title={(report.columns[i].samples ?? []).join(" | ")}>{row.column}</td>
                    <td>{row.hint}{report.columns[i].type_conflict ? " *" : ""}</td>
                    <td className="tabular">{row.coverage}</td>
                    {row.cells.map((has, j) => <td key={j} className={has ? "id-cell--yes" : "id-cell--no"}>{has ? "✓" : "–"}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small muted">* values in this column look like different types in different files.</p>
          <DataTable
            rows={report.files.map((f) => ({ ...f, id: f.file_id }) as unknown as Row)}
            columns={[
              { key: "filename", label: "File" },
              { key: "rows", label: "Rows", className: "tabular" },
              { key: "missing", label: "Missing columns", className: "small", render: (r) => fmt(r.missing) },
              { key: "extra", label: "Columns not in every file", className: "small", render: (r) => fmt(r.extra) },
            ]}
          />
        </>
      )}
    </div>
  );
}

interface Review {
  columns: ReviewColumn[];
  requires_decision: string[];
  valid_targets: string[];
  current_mapping: Record<string, string | null>;
  note: string;
}

const UNDECIDED = "__undecided__";
const NONE = "__none__";

function MappingTab({ batch, files, onSaved }: { batch: Row; files: Row[]; onSaved: () => void }) {
  const client = useWs();
  const action = useAction();
  const [decisions, setDecisions] = useState<Decisions | null>(null);
  const [overrides, setOverrides] = useState<{ fileId: string; column: string; target: string }[]>([]);
  const [draft, setDraft] = useState({ fileId: "", column: "", target: "" });
  const review = useLoad(async (signal) => {
    const data = await client.get<Review>(`/internal-data/batches/${batch.id}/mapping-review`, undefined, signal);
    setDecisions(initialDecisions(data.columns, data.current_mapping ?? {}));
    return data;
  }, `${client.base}mapping/${batch.id}`);
  if (review.error) return <div className="pad"><ErrorBanner error={review.error} onRetry={review.refresh} /></div>;
  if (!review.data || !decisions) return <Loading />;
  const data = review.data;
  const missing = undecided(data.columns, decisions);
  const shared = sharedTargets(decisions);
  const locked = ["merging", "merged"].includes(String(batch.status));
  const save = () =>
    action.run(async () => {
      const fileMappings: Record<string, Record<string, string | null>> = {};
      for (const o of overrides) (fileMappings[o.fileId] ??= {})[o.column] = o.target === NONE ? null : o.target;
      await client.put(`/internal-data/batches/${batch.id}/mapping`, { mapping: buildMapping(decisions), file_mappings: fileMappings });
      onSaved();
    });
  const fileColumns = (files.find((f) => f.id === draft.fileId)?.columns as string[] | undefined) ?? [];
  return (
    <div className="pad">
      <p className="small muted">{data.note}</p>
      {action.error && <ErrorBanner error={action.error} />}
      {missing.length > 0 && (
        <div className="alert alert--warning" role="status">
          {missing.length} ambiguous column(s) need your decision before the mapping can be saved: {missing.join(", ")}.
        </div>
      )}
      {Object.keys(shared).length > 0 && (
        <div className="alert alert--info" role="status">
          Several columns map to one field: {Object.entries(shared).map(([t, cols]) => `${t} ← ${cols.join(", ")}`).join("; ")}. That is fine when they come from different files; one file may not map two columns to the same field.
        </div>
      )}
      <div className="id-map">
        {data.columns.map((c) => {
          const value = decisions[c.column];
          const selectValue = value === undefined ? UNDECIDED : value === null ? NONE : value;
          return (
            <div key={c.column} className={`id-map__row${c.status === "ambiguous" ? " id-map__row--ambiguous" : ""}${value === undefined ? " id-map__row--undecided" : ""}`}>
              <div>
                <strong>{c.column}</strong>
                <div className="small muted">{c.type_hint ?? "—"}{c.files_present ? ` · in ${c.files_present} file(s)` : ""}</div>
              </div>
              <Pill value={c.status === "ambiguous" ? "NEEDS_REVIEW" : c.status === "confident" ? "ok" : "not_configured"} />
              <select
                className="input input--small"
                aria-label={`Field for ${c.column}`}
                value={selectValue}
                disabled={locked}
                onChange={(e) => setDecisions({ ...decisions, [c.column]: e.target.value === NONE ? null : e.target.value === UNDECIDED ? undefined : e.target.value })}
              >
                {value === undefined && <option value={UNDECIDED}>Choose…</option>}
                <option value={NONE}>— not mapped (kept as original) —</option>
                {c.suggestion && <option value={c.suggestion}>{c.suggestion} (suggested)</option>}
                {c.alternatives.map((a) => <option key={a} value={a}>{a}</option>)}
                {data.valid_targets.filter((t) => t !== c.suggestion && !c.alternatives.includes(t)).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <div>
                {c.reasons.length > 0 && <ul className="id-map__reasons">{c.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
                {(c.samples ?? []).length > 0 && <div className="small muted">e.g. {(c.samples ?? []).slice(0, 3).join(" · ")}</div>}
              </div>
            </div>
          );
        })}
      </div>
      <h4>Per-file overrides</h4>
      <p className="small muted">When one column name means different things in different files, map it for a single file here.</p>
      {overrides.length > 0 && (
        <ul className="id-files">
          {overrides.map((o, i) => (
            <li key={`${o.fileId}${o.column}`}>
              <span>{String(files.find((f) => f.id === o.fileId)?.filename ?? o.fileId)}: {o.column} → {o.target === NONE ? "not mapped" : o.target}</span>
              <button type="button" className="button button--ghost button--small" onClick={() => setOverrides(overrides.filter((_, j) => j !== i))}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {!locked && (
        <div className="form__actions">
          <select className="input input--small" aria-label="File" value={draft.fileId} onChange={(e) => setDraft({ fileId: e.target.value, column: "", target: "" })}>
            <option value="">File…</option>
            {files.map((f) => <option key={f.id} value={f.id}>{String(f.filename)}</option>)}
          </select>
          <select className="input input--small" aria-label="Column" value={draft.column} disabled={!draft.fileId} onChange={(e) => setDraft({ ...draft, column: e.target.value })}>
            <option value="">Column…</option>
            {fileColumns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select className="input input--small" aria-label="Field" value={draft.target} disabled={!draft.column} onChange={(e) => setDraft({ ...draft, target: e.target.value })}>
            <option value="">Field…</option>
            <option value={NONE}>— not mapped —</option>
            {data.valid_targets.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <button type="button" className="button button--ghost button--small" disabled={!draft.target} onClick={() => { setOverrides([...overrides.filter((o) => !(o.fileId === draft.fileId && o.column === draft.column)), draft]); setDraft({ fileId: "", column: "", target: "" }); }}>
            Add override
          </button>
        </div>
      )}
      <div className="form__actions">
        <button type="button" className="button button--primary" disabled={locked || action.busy || missing.length > 0} onClick={() => void save()}>
          Save mapping
        </button>
        {locked && <span className="muted small">This batch is {String(batch.status)}; the mapping can no longer change.</span>}
      </div>
    </div>
  );
}

function MergeTab({ batch, rowStatus, onChanged }: { batch: Row; rowStatus: Record<string, number>; onChanged: () => void }) {
  const client = useWs();
  const action = useAction();
  const [task, setTask] = useState<Row | null>(null);
  const [tick, setTick] = useState(0);
  const conflicts = useLoad((signal) => client.get<{ items: Row[]; total: number }>(`/internal-data/batches/${batch.id}/conflicts`, { limit: 50 }, signal), `${client.base}conflicts/${batch.id}/${tick}/${String(batch.status)}`);
  const merging = String(batch.status) === "merging";
  return (
    <div className="pad">
      {action.error && <ErrorBanner error={action.error} />}
      <div className="chips">
        {Object.entries(rowStatus).map(([k, v]) => <span key={k} className="chip">{k.replace(/_/g, " ")}: {v}</span>)}
        {Object.keys(rowStatus).length === 0 && <span className="muted small">No rows merged yet.</span>}
      </div>
      <div className="form__actions">
        <button
          type="button"
          className="button button--primary"
          disabled={action.busy || String(batch.status) !== "mapped"}
          onClick={() => void action.run(async () => { setTask(await client.post<Row>(`/internal-data/batches/${batch.id}/merge`)); onChanged(); })}
        >
          Merge into the CRM
        </button>
        <button type="button" className="button button--ghost button--small" onClick={() => { onChanged(); setTick((n) => n + 1); }}>Refresh</button>
        {String(batch.status) !== "mapped" && !merging && <span className="muted small">Save an explicit mapping first (status: {String(batch.status)}).</span>}
        {(merging || task) && <span className="muted small">Merging runs in the background. <Link className="link" to="/background">Background jobs</Link></span>}
      </div>
      <p className="small muted">Matching rows fill blank CRM fields. Where a stored value differs, the stored value is kept and the row waits here for your decision.</p>
      {conflicts.error && <ErrorBanner error={conflicts.error} />}
      {(conflicts.data?.items ?? []).map((row) => <ConflictRow key={row.id} batchId={String(batch.id)} row={row} onResolved={() => { setTick((n) => n + 1); onChanged(); }} />)}
      {conflicts.data && conflicts.data.total === 0 && <p className="muted">No conflicts to review.</p>}
    </div>
  );
}

function ConflictRow({ batchId, row, onResolved }: { batchId: string; row: Row; onResolved: () => void }) {
  const client = useWs();
  const action = useAction();
  const [choices, setChoices] = useState<Record<string, ConflictChoice | undefined>>({});
  const items = ((row.conflicts as Row[] | undefined) ?? []).filter((c) => !c.resolution);
  const { decisions, invalid } = conflictPayload(choices);
  const set = (field: string, choice: ConflictChoice | undefined) => setChoices({ ...choices, [field]: choice });
  return (
    <div className="id-conflict">
      <div className="small muted">
        Row {fmt(row.row_number)}
        {row.company_id ? <> · <Link className="link" to={`/companies/${String(row.company_id)}`}>company</Link></> : null}
        {row.contact_id ? <> · <Link className="link" to={`/contacts/${String(row.contact_id)}`}>contact</Link></> : null}
      </div>
      {items.map((c) => {
        const field = String(c.field);
        const choice = choices[field];
        return (
          <div key={field} className="id-conflict__field">
            <strong>{field}</strong>
            <span>CRM: {fmt(c.existing)}</span>
            <span>File: {fmt(c.incoming)}</span>
            <div className="id-choice">
              <button type="button" className={`button button--small ${choice === "keep" ? "button--primary" : "button--ghost"}`} onClick={() => set(field, "keep")}>Keep existing</button>
              <button type="button" className={`button button--small ${choice === "take_new" ? "button--primary" : "button--ghost"}`} onClick={() => set(field, "take_new")}>Take new</button>
              <input
                className="input input--small"
                placeholder="Other value"
                aria-label={`Other value for ${field}`}
                value={typeof choice === "object" ? choice.value : ""}
                onChange={(e) => set(field, e.target.value ? { value: e.target.value } : undefined)}
              />
            </div>
          </div>
        );
      })}
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button
          type="button"
          className="button button--primary button--small"
          disabled={action.busy || Object.keys(decisions).length === 0 || invalid.length > 0}
          onClick={() => void action.run(async () => { await client.post(`/internal-data/batches/${batchId}/rows/${row.id}/resolve`, { decisions }); onResolved(); })}
        >
          Save {Object.keys(decisions).length} decision(s)
        </button>
      </div>
    </div>
  );
}
