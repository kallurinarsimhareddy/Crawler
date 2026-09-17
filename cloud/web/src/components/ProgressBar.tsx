import type { JobProgress, JobStatus } from "../api/types";
import { phaseLabel } from "../lib/format";

interface Props {
  progress: JobProgress;
  status: JobStatus;
  cancelRequested?: boolean;
}

export function ProgressBar({ progress, status, cancelRequested = false }: Props) {
  const { completed, total } = progress;
  const known = total !== null && total > 0;
  const percent = known ? Math.min(100, Math.round((completed / total) * 100)) : status === "completed" ? 100 : 0;
  const indeterminate = status === "running" && !known;
  const label = cancelRequested && status === "running" ? "Cancelling…" : progress.message ?? phaseLabel(progress.current_phase);

  return (
    <div className="progress">
      <div
        className={`progress__track${indeterminate ? " progress__track--indeterminate" : ""}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : percent}
      >
        <div className={`progress__fill progress__fill--${status}`} style={{ width: indeterminate ? undefined : `${percent}%` }} />
      </div>
      <div className="progress__meta">
        <span>{label}</span>
        <span className="tabular">{known ? `${completed} / ${total} · ${percent}%` : null}</span>
      </div>
    </div>
  );
}
