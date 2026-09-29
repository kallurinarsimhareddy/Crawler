// Scraper / research results → GTM. Read-only preview first, then reviewable
// actions: Add to List, Validate Emails, Create Campaign (draft), Create CRM
// Proposal, Research These. Nothing here creates CRM records or sends email.

import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import type { Row } from "../api";
import { previewLine, resultLink } from "../logic/internalData";
import "../styles/internalData.css";
import { DataTable, Pill, useAction, useLoad } from "../ui";
import { useWs } from "../workspace";

interface Preview {
  label: string;
  summary: Record<string, number>;
  companies: Row[];
  contacts: Row[];
  note: string;
}

const ACTIONS: { key: string; label: string; hint: string; body?: Record<string, unknown> }[] = [
  { key: "add-to-list", label: "Add to List", hint: "Companies already in the CRM go into a new prospect list.", body: { entity_type: "companies" } },
  { key: "validate-emails", label: "Validate Emails", hint: "Every email becomes an email validation job. The CRM is not changed." },
  { key: "create-campaign", label: "Create Campaign", hint: "A draft campaign (sending off) with the prospect list as audience." },
  { key: "create-crm-proposal", label: "Create CRM Proposal", hint: "Proposals for companies and contacts; nothing is applied until you approve." },
  { key: "research-these", label: "Research These", hint: "A research plan over these companies; it runs only after you approve it." },
];

export function GtmActions({ sourceType, sourceId }: { sourceType: "scrape" | "research"; sourceId: string }) {
  const client = useWs();
  const base = `/gtm-bridge/${sourceType}/${encodeURIComponent(sourceId)}`;
  const preview = useLoad((signal) => client.get<Preview>(base, undefined, signal), client.base + base);
  const action = useAction();
  const [results, setResults] = useState<{ key: string; result: Row }[]>([]);
  const [show, setShow] = useState<"companies" | "contacts" | null>(null);

  const run = (key: string, body?: Record<string, unknown>) =>
    action.run(async () => {
      const result = await client.post<Row>(`${base}/${key}`, body ?? {});
      setResults((r) => [{ key, result }, ...r.filter((x) => x.key !== key)]);
    });

  if (preview.error) return <div className="pad"><ErrorBanner error={preview.error} onRetry={preview.refresh} /></div>;
  if (!preview.data) return <Loading />;
  const data = preview.data;
  return (
    <div className="pad bridge">
      <p className="small muted">{data.note}</p>
      <p><strong>{previewLine(data.summary)}</strong></p>
      <div className="bridge__actions">
        {ACTIONS.map((a) => (
          <button key={a.key} type="button" className="button button--ghost button--small" title={a.hint} disabled={action.busy} onClick={() => void run(a.key, a.body)}>
            {a.label}
          </button>
        ))}
        <button type="button" className="button button--ghost button--small" onClick={() => setShow(show === "companies" ? null : "companies")}>
          {show === "companies" ? "Hide" : "Show"} companies
        </button>
        <button type="button" className="button button--ghost button--small" onClick={() => setShow(show === "contacts" ? null : "contacts")}>
          {show === "contacts" ? "Hide" : "Show"} emails
        </button>
      </div>
      {action.error && <ErrorBanner error={action.error} />}
      {results.map(({ key, result }) => {
        const link = resultLink(key, result);
        const label = ACTIONS.find((a) => a.key === key)?.label ?? key;
        const detail = String(result.hint ?? result.note ?? result.detail ?? "");
        return (
          <div key={key} className="alert alert--info bridge__result" role="status">
            <strong>{label}:</strong>{" "}
            {key === "add-to-list" && `${String(result.added ?? 0)} added, ${String(result.not_in_crm ?? 0)} not in the CRM yet. `}
            {key === "validate-emails" && `${String(result.emails ?? 0)} email(s) queued for validation. `}
            {key === "create-crm-proposal" && `${String(result.created ?? 0)} proposal(s) created. `}
            {detail}
            {link && <> <Link className="link" to={link.to}>{link.label}</Link></>}
          </div>
        );
      })}
      {show === "companies" && (
        <DataTable
          rows={data.companies.map((c, i) => ({ ...c, id: String(c.key ?? i) }) as Row)}
          columns={[
            { key: "name", label: "Company" },
            { key: "domain", label: "Domain", className: "mono small" },
            { key: "match", label: "CRM", render: (r) => <Pill value={r.match} /> },
            { key: "company_id", label: "Record", render: (r) => (r.company_id ? <Link className="link" to={`/companies/${String(r.company_id)}`}>open</Link> : "—") },
          ]}
          empty="No companies in these results"
        />
      )}
      {show === "contacts" && (
        <DataTable
          rows={data.contacts.map((c, i) => ({ ...c, id: String(c.email ?? i) }) as Row)}
          columns={[
            { key: "email", label: "Email", className: "mono small" },
            { key: "full_name", label: "Name" },
            { key: "match", label: "CRM", render: (r) => <Pill value={r.match} /> },
          ]}
          empty="No email addresses in these results"
        />
      )}
    </div>
  );
}
