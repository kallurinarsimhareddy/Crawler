import { api } from "../api/client";
import type { Status } from "../api/types";
import { usePolling } from "../hooks/usePolling";
import { formatSeconds, formatDateTime } from "../lib/format";

/**
 * Whether anything is around to run crawls.
 *
 * The worker is a separate process, and while it is hosted on someone's laptop
 * it is often simply not running. A queued crawl then sits there with no
 * explanation, which reads as a bug. This says plainly that the worker is
 * offline and what to do about it.
 */
export function useWorkerStatus() {
  return usePolling<Status>((signal) => api.status(signal), 10000, "status");
}

function Dot({ online }: { online: boolean }) {
  return <span className={`worker__dot worker__dot--${online ? "on" : "off"}`} aria-hidden="true" />;
}

/** The full banner: shown on the dashboard and above the new-crawl form. */
export function WorkerBanner({ status, error }: { status: Status | null; error?: Error | null }) {
  // A failed /status poll is not a worker verdict: say nothing rather than
  // claim the worker is offline when it may be the API that is unreachable.
  if (error || !status) return null;

  const { worker, queue, database, redis } = status;
  const waiting = queue.ready + queue.delayed;
  const broken = database.status === "down" || redis.status === "down";

  if (broken) {
    const which = [database.status === "down" && "database", redis.status === "down" && "queue"]
      .filter(Boolean)
      .join(" and ");
    return (
      <div className="alert alert--error worker-banner" role="alert">
        <div>
          <strong>SANA GTM is degraded.</strong>{" "}
          <span>The {which} cannot be reached, so new crawls cannot be accepted right now.</span>
          {database.detail && <div className="worker-banner__detail">{database.detail}</div>}
          {redis.detail && <div className="worker-banner__detail">{redis.detail}</div>}
        </div>
      </div>
    );
  }

  if (worker.online) {
    // Everything is working. Stay out of the way unless work is piling up.
    if (waiting === 0) return null;
    return (
      <div className="alert alert--info worker-banner" role="status">
        <div>
          <Dot online />
          <strong>Worker online.</strong>{" "}
          <span>
            {waiting} crawl{waiting === 1 ? "" : "s"} waiting to start.
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="alert alert--warning worker-banner" role="status">
      <div>
        <Dot online={false} />
        <strong>Crawler worker offline.</strong>{" "}
        <span>Start the worker to process new crawls.</span>
        <div className="worker-banner__detail">
          {waiting > 0 ? (
            <>
              {waiting} crawl{waiting === 1 ? "" : "s"} queued and waiting. They will start on their own
              once a worker is running — nothing is lost.
            </>
          ) : (
            <>You can still create crawls; they will queue until a worker is running.</>
          )}
        </div>
        <div className="worker-banner__detail">
          Run <code>run-worker.bat</code> from the CareerCrawler-cloud folder.
          {worker.last_heartbeat && (
            <>
              {" "}
              Last seen {formatSeconds(worker.seconds_since_heartbeat)} ago (
              {formatDateTime(worker.last_heartbeat)}).
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** The one-line version for the sidebar. */
export function WorkerPill({ status, error }: { status: Status | null; error?: Error | null }) {
  if (error || !status) return null;
  const { worker, queue } = status;
  const waiting = queue.ready + queue.delayed;
  const title = [
    worker.message,
    worker.last_heartbeat ? `Last heartbeat: ${formatDateTime(worker.last_heartbeat)}` : null,
    `Database: ${status.database.status}`,
    `Queue: ${status.redis.status}`,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div className={`worker worker--${worker.online ? "on" : "off"}`} title={title}>
      <Dot online={worker.online} />
      <span>{worker.online ? "Worker online" : "Worker offline"}</span>
      {waiting > 0 && <span className="worker__count tabular">{waiting}</span>}
    </div>
  );
}
