// The company page's job summary: open / new / recently closed jobs from monitored
// sources and imports, weekly hiring activity, and the full job history.

import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner, Loading } from "../../components/Feedback";
import { Stat, Tabs, fmtDate, useLoad } from "../ui";
import { useWs } from "../workspace";
import { MiniJobs, type Job } from "./jobsShared";

interface Activity {
  counts: { open: number; new: number; recently_closed: number; total: number };
  open_jobs: Job[];
  new_jobs: Job[];
  recently_closed: Job[];
  activity: { week: string; new: number; closed: number }[];
  history: Job[];
}

function WeeklyActivity({ weeks }: { weeks: Activity["activity"] }) {
  if (weeks.length === 0) return <p className="muted small">No hiring activity recorded yet.</p>;
  const max = Math.max(1, ...weeks.map((w) => Math.max(w.new ?? 0, w.closed ?? 0)));
  return (
    <ul className="bars jm-weeks" aria-label="New and closed jobs per week">
      {weeks.map((w) => (
        <li key={w.week} className="jm-week">
          <span className="bars__label tabular">{fmtDate(w.week)}</span>
          <span className="jm-week__bars">
            <span className="bars__track" title={`${w.new ?? 0} new`}>
              <span className="bars__fill jm-fill--new" style={{ width: `${((w.new ?? 0) / max) * 100}%` }} />
            </span>
            <span className="bars__track" title={`${w.closed ?? 0} closed`}>
              <span className="bars__fill jm-fill--closed" style={{ width: `${((w.closed ?? 0) / max) * 100}%` }} />
            </span>
          </span>
          <span className="bars__value tabular small">+{(w.new ?? 0).toLocaleString()} / −{(w.closed ?? 0).toLocaleString()}</span>
        </li>
      ))}
    </ul>
  );
}

export function CompanyJobActivity({ companyId }: { companyId: string }) {
  const client = useWs();
  const [view, setView] = useState("open");
  const { data, error, loading, refresh } = useLoad(
    (signal) => client.get<Activity>(`/companies/${encodeURIComponent(companyId)}/job-activity`, undefined, signal),
    client.base + "job-activity" + companyId,
  );
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  const c = data.counts ?? { open: 0, new: 0, recently_closed: 0, total: 0 };
  return (
    <div className="jm-activity">
      <div className="stats">
        <Stat label="Open" value={(c.open ?? 0).toLocaleString()} />
        <Stat label="New in 30 days" value={(c.new ?? 0).toLocaleString()} />
        <Stat label="Recently Closed" value={(c.recently_closed ?? 0).toLocaleString()} />
        <Stat label="All jobs" value={(c.total ?? 0).toLocaleString()} hint={<Link className="link" to={`/jobs?company_id=${encodeURIComponent(companyId)}`}>Open in Jobs</Link>} />
      </div>
      <Tabs
        active={view}
        onChange={setView}
        tabs={[
          { key: "open", label: "Open Jobs", count: data.open_jobs?.length ?? 0 },
          { key: "new", label: "New Jobs", count: data.new_jobs?.length ?? 0 },
          { key: "closed", label: "Recently Closed Jobs", count: data.recently_closed?.length ?? 0 },
          { key: "activity", label: "Hiring Activity" },
          { key: "history", label: "Job History", count: data.history?.length ?? 0 },
        ]}
      />
      {view === "open" && <MiniJobs rows={data.open_jobs ?? []} empty="No open jobs from monitored sources or imports." />}
      {view === "new" && <MiniJobs rows={data.new_jobs ?? []} empty="No new jobs in the last 30 days." />}
      {view === "closed" && (
        <MiniJobs
          rows={data.recently_closed ?? []}
          empty="No jobs closed recently."
          extra={[{ key: "closed_at", label: "Closed", render: (r) => (r.closed_at ? fmtDate(r.closed_at) : <span className="muted">—</span>) }]}
        />
      )}
      {view === "activity" && <WeeklyActivity weeks={data.activity ?? []} />}
      {view === "history" && <MiniJobs rows={data.history ?? []} empty="No job history for this company yet." />}
    </div>
  );
}
