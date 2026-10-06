// Shared Account Builder controls: a company picker (search CRM companies by name — never a raw
// id) and a list picker (an existing company list or a new one).

import { useEffect, useState, type ReactNode } from "react";
import type { Row } from "../api";
import { useLoad } from "../ui";
import { useWs } from "../workspace";
import type { CrmRef } from "../logic/accounts";

export function CompanyPicker({ value, onChange, suggestions = [], autoFocus = false }: {
  value: CrmRef | null;
  onChange: (company: CrmRef | null) => void;
  suggestions?: CrmRef[];
  autoFocus?: boolean;
}) {
  const client = useWs();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(q.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [q]);
  const found = useLoad(
    (signal) => (debounced.length >= 2
      ? client.list<Row>("/companies", { q: debounced, limit: 8 }, signal)
      : Promise.resolve({ items: [], total: 0 } as never)),
    `${client.base}|company-picker|${debounced}`,
  );
  const options: CrmRef[] = debounced.length >= 2
    ? ((found.data as { items?: Row[] } | null)?.items ?? []).map((r) => ({ id: String(r.id), name: String(r.name ?? ""), domain: (r.domain as string) ?? null }))
    : suggestions;
  if (value) {
    return (
      <div className="ab-picked">
        <strong>{value.name}</strong>{value.domain ? <span className="muted small"> · {value.domain}</span> : null}
        <button type="button" className="link-button small" onClick={() => onChange(null)}>Change</button>
      </div>
    );
  }
  return (
    <div className="ab-picker">
      <input className="input" autoFocus={autoFocus} placeholder="Search CRM companies by name…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search CRM companies" />
      {options.length > 0 ? (
        <ul className="ab-picker__list" role="listbox">
          {options.map((c) => (
            <li key={c.id}>
              <button type="button" className="ab-picker__option" onClick={() => onChange(c)}>
                <strong>{c.name}</strong>{c.domain ? <span className="muted small"> · {c.domain}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : debounced.length >= 2 && !found.loading ? <p className="muted small">No CRM company matches “{debounced}”.</p> : null}
    </div>
  );
}

export interface ListChoice {
  list_id?: string;
  new_list?: string;
}

export function ListPicker({ value, onChange }: { value: ListChoice; onChange: (choice: ListChoice) => void }) {
  const client = useWs();
  const lists = useLoad((signal) => client.list<Row>("/lists", { entity_type: "companies", limit: 100 }, signal), `${client.base}|company-lists`);
  const items = ((lists.data as { items?: Row[] } | null)?.items ?? []).filter((l) => l.entity_type === "companies");
  const creating = value.new_list !== undefined;
  return (
    <div className="ab-listpicker">
      <label className="field">
        <span className="field__label">Add to list</span>
        <select className="input" value={creating ? "__new__" : value.list_id ?? ""} onChange={(e) => {
          const v = e.target.value;
          onChange(v === "__new__" ? { new_list: "" } : v ? { list_id: v } : {});
        }}>
          <option value="">— no list —</option>
          {items.map((l) => <option key={String(l.id)} value={String(l.id)}>{String(l.name)} ({Number(l.member_count ?? 0).toLocaleString()})</option>)}
          <option value="__new__">+ New list…</option>
        </select>
      </label>
      {creating && (
        <label className="field">
          <span className="field__label">New list name</span>
          <input className="input" placeholder="e.g. Q4 SAP Migration Targets" value={value.new_list ?? ""} onChange={(e) => onChange({ new_list: e.target.value })} />
        </label>
      )}
    </div>
  );
}

export function listChoiceReady(choice: ListChoice): boolean {
  return Boolean(choice.list_id || (choice.new_list !== undefined && choice.new_list.trim()));
}

/** A dialog over a dimmed page: click outside or press Escape to close. */
export function Modal({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="admin-dialog" role="dialog" aria-modal="true" aria-label={label}>
      <button type="button" className="admin-dialog__scrim" aria-label="Close" onClick={onClose} />
      <div className="card pad form admin-dialog__panel ab-dialog">{children}</div>
    </div>
  );
}
