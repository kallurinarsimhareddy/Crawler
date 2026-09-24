// Workspace gating and the generic record view.

import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorBanner, Loading } from "../components/Feedback";
import type { Row } from "./api";
import { KeyValues, PageHeader, Pill, fmt, useLoad } from "./ui";
import { useWorkspace, useWs } from "./workspace";

/** Children render only once a workspace exists and is selected. */
export function RequireWorkspace({ children }: { children: ReactNode }) {
  const { current, loading, error, create, reload } = useWorkspace();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<Error | null>(null);
  if (loading && !current) return <div className="page"><Loading label="Loading workspaces…" /></div>;
  if (error && !current) return <div className="page"><ErrorBanner error={error} onRetry={reload} /></div>;
  if (!current) {
    return (
      <div className="page page--narrow">
        <PageHeader title="Create your workspace" subtitle="Everything — companies, contacts, campaigns, credentials and credits — belongs to a workspace and is never visible to other workspaces." />
        <form
          className="card form"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setCreateError(null);
            try {
              await create(name.trim());
            } catch (err) {
              setCreateError(err as Error);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="field">
            <span className="field__label">Workspace name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="RiseIT GTM" required />
          </label>
          {createError && <ErrorBanner error={createError} />}
          <div className="form__actions"><button className="button button--primary" disabled={busy || !name.trim()}>Create workspace</button></div>
        </form>
      </div>
    );
  }
  return <>{children}</>;
}

export function WorkspaceSwitcher() {
  const { workspaces, current, select } = useWorkspace();
  if (!current) return null;
  return (
    <label className="ws-switch">
      <span className="sr-only">Workspace</span>
      <select className="input input--small" value={current.id} onChange={(e) => select(e.target.value)}>
        {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
      </select>
    </label>
  );
}

/** Any record by API path, shown as a key/value sheet. */
export function RecordView({ path, back, backLabel }: { path: string; back: string; backLabel: string }) {
  const { id = "" } = useParams();
  const client = useWs();
  const { data, error, loading } = useLoad((signal) => client.get<Row>(`${path}/${id}`, undefined, signal), client.base + path + id);
  if (error) return <div className="page"><ErrorBanner error={error} /></div>;
  if (loading || !data) return <div className="page"><Loading /></div>;
  const title = String(data.title ?? data.name ?? data.full_name ?? id);
  return (
    <div className="page">
      <Link to={back} className="back">← {backLabel}</Link>
      <PageHeader title={title} actions={data.status ? <Pill value={data.status} /> : undefined} />
      <div className="card pad">
        <KeyValues items={Object.entries(data).filter(([k]) => k !== "workspace_id").map(([k, v]) => [k.replace(/_/g, " "), typeof v === "object" && v !== null ? <code className="small">{JSON.stringify(v)}</code> : fmt(v)])} />
      </div>
    </div>
  );
}
