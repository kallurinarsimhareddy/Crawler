import { Link, useNavigate } from "react-router-dom";
import type { Job } from "../api/types";
import { JOB_TYPE_LABELS, formatDateTime, shortId } from "../lib/format";
import { StatusBadge } from "./StatusBadge";

interface Props {
  jobs: Job[];
  compact?: boolean;
}

export function JobsTable({ jobs, compact = false }: Props) {
  const navigate = useNavigate();

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Job ID</th>
            <th scope="col">Type</th>
            <th scope="col">Target</th>
            <th scope="col">Status</th>
            <th scope="col">Created</th>
            {!compact && <th scope="col">Completed</th>}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id} className="table__row" onClick={() => navigate(`/jobs/${job.job_id}`)}>
              <td data-label="Job ID">
                <Link to={`/jobs/${job.job_id}`} className="mono" onClick={(event) => event.stopPropagation()} title={job.job_id}>
                  {shortId(job.job_id)}
                </Link>
              </td>
              <td data-label="Type">{JOB_TYPE_LABELS[job.type]}</td>
              <td data-label="Target" className="table__target" title={job.target}>
                {job.target}
              </td>
              <td data-label="Status">
                <StatusBadge status={job.status} />
              </td>
              <td data-label="Created" className="tabular">
                {formatDateTime(job.created_at)}
              </td>
              {!compact && (
                <td data-label="Completed" className="tabular">
                  {formatDateTime(job.completed_at)}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
