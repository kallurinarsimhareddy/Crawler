// Ctrl+K / Cmd+K from anywhere: jump to a page, open a company or contact, or ask
// SANA GTM AI. Arrow keys move, Enter opens, Esc closes.

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Row } from "../platform/api";
import { useWorkspace } from "../platform/workspace";
import { PROMPTS, useAssistant } from "./Assistant";
import { Icon, type IconName } from "./Icon";
import { destinations } from "./nav";

interface Item {
  id: string;
  label: string;
  hint?: string;
  icon: IconName;
  group: string;
  run: () => void;
}

function useRecords(query: string) {
  const { client } = useWorkspace();
  const [rows, setRows] = useState<{ companies: Row[]; contacts: Row[] }>({ companies: [], contacts: [] });
  useEffect(() => {
    const q = query.trim();
    if (!client || q.length < 2) {
      setRows({ companies: [], contacts: [] });
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void Promise.all([
        client.list("/companies", { q, limit: 5 }, controller.signal).catch(() => null),
        client.list("/contacts", { q, limit: 5 }, controller.signal).catch(() => null),
      ]).then(([companies, contacts]) => {
        if (!controller.signal.aborted) setRows({ companies: companies?.items ?? [], contacts: contacts?.items ?? [] });
      });
    }, 180);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [client, query]);
  return rows;
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const assistant = useAssistant();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const records = useRecords(query);

  const go = (to: string) => () => {
    onClose();
    navigate(to);
  };
  const askAi = (text: string, send: boolean) => () => {
    onClose();
    assistant.show(text, send);
  };

  const items = useMemo<Item[]>(() => {
    const q = query.trim().toLowerCase();
    const out: Item[] = [];
    if (q) out.push({ id: "ask", label: `Ask SANA GTM AI: “${query.trim()}”`, icon: "sparkles", group: "AI", run: askAi(query.trim(), true) });
    const pages = destinations().filter((d) => !q || `${d.label} ${d.section} ${d.keywords}`.toLowerCase().includes(q));
    out.push(...pages.slice(0, q ? 10 : 8).map((d) => ({ id: `page:${d.to}:${d.label}`, label: d.label, hint: d.section, icon: d.icon, group: "Go to", run: go(d.to) })));
    out.push(...records.companies.map((r) => ({ id: `co:${r.id}`, label: String(r.name ?? r.domain ?? r.id), hint: String(r.domain ?? ""), icon: "building" as IconName, group: "Companies", run: go(`/companies/${r.id}`) })));
    out.push(...records.contacts.map((r) => ({ id: `ct:${r.id}`, label: String(r.full_name ?? r.email ?? r.id), hint: String(r.title ?? ""), icon: "users" as IconName, group: "Contacts", run: go(`/contacts/${r.id}`) })));
    if (!q) out.push(...PROMPTS.map((p) => ({ id: `ex:${p}`, label: p, icon: "sparkles" as IconName, group: "Ask SANA GTM AI", run: askAi(p, false) })));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, records]);

  useEffect(() => setCursor(0), [query]);
  useEffect(() => input.current?.focus(), []);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${cursor}"]`)?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(items.length - 1, c + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[cursor]?.run();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  let lastGroup = "";
  return (
    <div className="palette__scrim" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Search and ask" onMouseDown={(e) => e.stopPropagation()}>
        <div className="palette__input">
          <Icon name="search" />
          <input
            ref={input}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKey}
            placeholder="Search pages, companies and contacts — or ask SANA GTM AI…"
            aria-label="Search or ask"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={items[cursor] ? `palette-${cursor}` : undefined}
          />
          <kbd className="kbd">Esc</kbd>
        </div>
        <div className="palette__list" id="palette-list" role="listbox" ref={list}>
          {items.length === 0 && <p className="palette__empty muted small">No matches. Press Enter to ask SANA GTM AI.</p>}
          {items.map((item, i) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            return (
              <div key={item.id}>
                {header && <div className="palette__group">{header}</div>}
                <button
                  type="button"
                  id={`palette-${i}`}
                  data-index={i}
                  role="option"
                  aria-selected={i === cursor}
                  className={`palette__item${i === cursor ? " palette__item--active" : ""}`}
                  onMouseMove={() => setCursor(i)}
                  onClick={item.run}
                >
                  <Icon name={item.icon} size={16} />
                  <span className="palette__label">{item.label}</span>
                  {item.hint && <span className="palette__hint">{item.hint}</span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="palette__foot muted small">
          <span><kbd className="kbd">↑</kbd> <kbd className="kbd">↓</kbd> move</span>
          <span><kbd className="kbd">Enter</kbd> open</span>
          <span><kbd className="kbd">Ctrl</kbd> <kbd className="kbd">K</kbd> toggle</span>
        </div>
      </div>
    </div>
  );
}
