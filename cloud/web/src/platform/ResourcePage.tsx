// A config-driven page for resources that need list + filter + create and
// nothing more specialised: tasks, activities, campaigns, sequences, lists,
// segments, templates, workflows, monitors, suppressions, exports…

import { useState, type FormEvent } from "react";
import { ErrorBanner } from "../components/Feedback";
import { newIdempotencyKey, type Row } from "./api";
import { PageHeader, ResourceList, useAction, type Column, type FilterDef } from "./ui";
import { useWs } from "./workspace";

export interface FieldDef {
  key: string;
  label: string;
  type?: "text" | "textarea" | "number" | "select" | "date" | "tags" | "json" | "checkbox";
  options?: string[];
  required?: boolean;
  placeholder?: string;
  hint?: string;
}

export interface ResourceConfig {
  title: string;
  subtitle?: string;
  path: string;
  columns: Column[];
  filters?: FilterDef[];
  create?: FieldDef[];
  createLabel?: string;
  link?: (row: Row) => string;
  empty?: string;
}

function coerce(field: FieldDef, raw: string | boolean): unknown {
  if (field.type === "checkbox") return Boolean(raw);
  const text = String(raw).trim();
  if (text === "") return undefined;
  switch (field.type) {
    case "number":
      return Number(text);
    case "tags":
      return text.split(",").map((t) => t.trim()).filter(Boolean);
    case "json":
      return JSON.parse(text);
    default:
      return text;
  }
}

export function CreateForm({ fields, path, label = "Create", onCreated }: { fields: FieldDef[]; path: string; label?: string; onCreated: (row: Row) => void }) {
  const client = useWs();
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const action = useAction();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const body: Record<string, unknown> = {};
    try {
      for (const field of fields) {
        const value = coerce(field, values[field.key] ?? (field.type === "checkbox" ? false : ""));
        if (value !== undefined) body[field.key] = value;
      }
    } catch {
      action.clear();
      return;
    }
    const row = await action.run(() => client.post<Row>(path, body, newIdempotencyKey()));
    if (row) {
      setValues({});
      onCreated(row);
    }
  };

  return (
    <form className="card form form--inline" onSubmit={submit}>
      <div className="form-grid">
        {fields.map((field) => (
          <label key={field.key} className={`field${field.type === "textarea" || field.type === "json" ? " field--wide" : ""}`}>
            <span className="field__label">
              {field.label}
              {field.required && <span aria-hidden="true"> *</span>}
            </span>
            {field.type === "select" ? (
              <select className="input" required={field.required} value={String(values[field.key] ?? "")} onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}>
                <option value="">—</option>
                {field.options?.map((o) => (
                  <option key={o} value={o}>
                    {o.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            ) : field.type === "textarea" || field.type === "json" ? (
              <textarea className="input textarea" rows={3} required={field.required} placeholder={field.placeholder} value={String(values[field.key] ?? "")} onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))} />
            ) : field.type === "checkbox" ? (
              <input type="checkbox" checked={Boolean(values[field.key])} onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.checked }))} />
            ) : (
              <input
                className="input"
                type={field.type === "number" ? "number" : field.type === "date" ? "date" : "text"}
                required={field.required}
                placeholder={field.placeholder}
                value={String(values[field.key] ?? "")}
                onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
              />
            )}
            {field.hint && <span className="field__hint">{field.hint}</span>}
          </label>
        ))}
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      <div className="form__actions">
        <button className="button button--primary" type="submit" disabled={action.busy}>
          {action.busy ? "Saving…" : label}
        </button>
      </div>
    </form>
  );
}

export function ResourcePage({ config }: { config: ResourceConfig }) {
  const client = useWs();
  const [showCreate, setShowCreate] = useState(false);
  const [reload, setReload] = useState(0);
  return (
    <div className="page">
      <PageHeader
        title={config.title}
        subtitle={config.subtitle}
        actions={
          config.create && (
            <button className="button button--primary" onClick={() => setShowCreate((s) => !s)}>
              {showCreate ? "Close" : config.createLabel ?? "New"}
            </button>
          )
        }
      />
      {showCreate && config.create && (
        <CreateForm
          fields={config.create}
          path={config.path}
          onCreated={() => {
            setShowCreate(false);
            setReload((n) => n + 1);
          }}
        />
      )}
      <ResourceList
        load={(query, signal) => client.list(config.path, query, signal)}
        columns={config.columns}
        filters={config.filters}
        link={config.link}
        empty={config.empty}
        reloadKey={`${client.base}:${reload}`}
      />
    </div>
  );
}
