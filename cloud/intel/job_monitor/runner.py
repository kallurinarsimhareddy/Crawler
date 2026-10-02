"""The ``job_monitor`` worker task: one monitor run, resumable page by page.

Incremental (daily): read the listing newest-first and stop once
``incremental_stop_pages`` pages in a row held only already-known jobs (or at
``max_pages_incremental``). It never closes anything.

Full sweep (weekly): read to the end of the listing. The end is believed only when
a re-read of the last page still shows no next page (a wedged or rate-limited page
looks exactly like the end). Only a COMPLETED full sweep — every page read, the end
confirmed, and a plausible number of jobs seen — runs the close phase: a missed job
older than the source's visible listing window EXPIRES (absence proves nothing
there); any other missed job counts a miss and CLOSES at ``close_after_missed``.
Then the gone checks fetch the missed jobs' own URLs (budgeted): a 410/404 CLOSES
the job with that evidence. Failed, partial or cancelled sweeps change nothing.

``job_lifecycle`` (daily, queued by the scheduler): STALE evaluation and the day's
summary, see :meth:`JobMonitorService.evaluate_lifecycle`.

Every page is stored before the checkpoint (next page URL + counters) is saved on
the run row, so a crashed or restarted worker resumes at the next unread page.
Re-reading a page is harmless (its jobs are already stored and become UNCHANGED),
and the final NEW / CHANGED / CLOSED counts are taken from the database, so they
stay exact across restarts.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Mapping

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.job_monitor.schema import normalize_job
from cloud.intel.job_monitor.service import FINISHED_RUN_STATES
from cloud.intel.job_monitor.strategies import PageResult, make_fetcher, strategy_for
from cloud.intel.scraper.models import Outcome

__all__ = ["run_job_monitor_task", "run_job_lifecycle_task", "MIN_SWEEP_SHARE"]

log = logging.getLogger(__name__)

#: A full sweep that read fewer than this share of the jobs the previous COMPLETED full
#: sweep read is treated as partial (a broken page layout must not close half the database).
MIN_SWEEP_SHARE = 0.5
#: Refusals are never retried or worked around; everything else is retried on the same cursor.
REFUSALS = {Outcome.BLOCKED, Outcome.CAPTCHA, Outcome.WAF, Outcome.LOGIN_REQUIRED, Outcome.ROBOTS, Outcome.UNSAFE,
            "DISABLED"}
#: Defaults for long runs (platform.config.extra overrides them; tests set them to zero).
PAGE_RETRIES = 5                                     # extra attempts on one cursor before the run stops
RETRY_DELAYS = (30.0, 60.0, 120.0, 240.0, 300.0)     # seconds between those attempts
END_CONFIRMATIONS = 3                                # fresh-session re-reads before "end of listing" is believed
END_DELAY = 30.0                                     # seconds between those re-reads
LIVE_COUNTS_EVERY = 25                               # pages between live count refreshes


def _setting(platform: Any, name: str, default: Any) -> Any:
    value = platform.config.extra.get(name)
    return default if value is None else value


def _finish(platform: Any, ctx: Ctx, monitor: Mapping[str, Any], run_id: str, status: str, *,
            stop_reason: str = None, error: str = None, closed: Dict[str, int] = None) -> Dict[str, Any]:
    store = platform.store
    service = platform.service("job_monitors")
    run = store.get(ctx, "job_monitor_runs", run_id)
    counts = service.run_counts(ctx, run_id)
    unchanged = max(0, run["found"] - counts["new_count"] - counts["changed_count"] - counts["reopened_count"])
    notes = list(run.get("notes") or [])
    if closed:
        notes.append(f"close phase: {closed['missed']:,} jobs missed this sweep, {closed['closed']:,} closed, "
                     f"{closed.get('expired', 0):,} expired (outside the source's listing window)")
        gone = closed.get("gone") or {}
        if gone:
            notes.append(f"gone checks: {gone.get('checked', 0):,} job URLs checked, {gone.get('closed', 0):,} gone "
                         f"(closed), {gone.get('live', 0):,} still live"
                         + (f" — stopped: {gone['stopped']}" if gone.get("stopped") else ""))
    finished = utcnow()
    duration = (finished - run["started_at"]).total_seconds() if run.get("started_at") else None
    run = store.update(ctx, "job_monitor_runs", run_id, {
        "status": status, "finished_at": finished, "stop_reason": (stop_reason or run.get("stop_reason") or None)
        and str(stop_reason or run.get("stop_reason"))[:500], "error": error and error[:2000],
        "unchanged_count": unchanged, "notes": notes[-50:], "duration_seconds": duration, **counts,
        **({"gone_checked_count": (closed.get("gone") or {}).get("checked", 0),
            "gone_closed_count": (closed.get("gone") or {}).get("closed", 0)} if closed else {})})
    now = utcnow()
    monitor_changes: Dict[str, Any] = {
        "last_run_id": run_id, "last_run_at": now, "last_run_status": status,
        "last_result": {k: run[k] for k in ("mode", "pages", "found", "new_count", "changed_count", "unchanged_count",
                                            "reopened_count", "closed_count", "expired_count", "rejected_count",
                                            "error_count")}
        | {"status": status, "stop_reason": run.get("stop_reason")},
        "total_jobs": store.count(ctx, "job_postings", {"source_monitor_id": monitor["id"]}),
    }
    if run["mode"] == "full":
        # Automatic sweeps only when they are switched on (a manual sweep never switches them on).
        auto = store.get(ctx, "job_source_monitors", monitor["id"]).get("next_full_sweep_at") is not None
        if status == "completed":
            monitor_changes["last_full_sweep_at"] = now
            if auto:
                monitor_changes["next_full_sweep_at"] = now + timedelta(days=int(monitor.get("full_sweep_days") or 7))
        elif auto:  # try the sweep again with tomorrow's run; nothing was closed
            monitor_changes["next_full_sweep_at"] = now + timedelta(days=1)
    store.update(ctx, "job_source_monitors", monitor["id"], monitor_changes)
    try:
        service.announce(ctx, store.get(ctx, "job_source_monitors", monitor["id"]), run)
    except Exception:  # noqa: BLE001 - the run's results are stored; a notification failure is logged
        log.exception("could not announce job monitor run %s", run_id)
    return {"run_id": run_id, "status": status, **{k: run[k] for k in (
        "pages", "found", "new_count", "changed_count", "unchanged_count", "reopened_count", "closed_count",
        "rejected_count", "error_count", "request_count", "warning_count", "duration_seconds")}}


def run_job_monitor_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    service = platform.service("job_monitors")
    run = store.find(ctx, "job_monitor_runs", str(task["params"].get("run_id") or ""))
    if run is None:
        raise PermanentTaskError("job monitor run not found")
    if run["status"] in FINISHED_RUN_STATES:
        return {"run_id": run["id"], "status": run["status"], "note": "already finished"}
    monitor = store.get(ctx, "job_source_monitors", run["monitor_id"])
    full = run["mode"] == "full"
    cp: Dict[str, Any] = dict(run.get("checkpoint") or {})
    if run["status"] != "running" or run.get("started_at") is None:
        run = store.update(ctx, "job_monitor_runs", run["id"], {"status": "running",
                                                                "started_at": run.get("started_at") or utcnow()})
    if cp:
        log.info("resuming job monitor run %s at page %s", run["id"], cp.get("pages", 0) + 1)

    max_pages = int(monitor["max_pages_full"] if full else monitor["max_pages_incremental"])
    page_retries = int(_setting(platform, "job_monitor_page_retries", PAGE_RETRIES))
    retry_delays = list(_setting(platform, "job_monitor_retry_delays", RETRY_DELAYS)) or [0.0]
    end_confirmations = int(_setting(platform, "job_monitor_end_confirmations", END_CONFIRMATIONS))
    end_delay = float(_setting(platform, "job_monitor_end_delay", END_DELAY))
    sleep: Callable[[float], None] = _setting(platform, "job_monitor_sleep", time.sleep)
    budget = (page_retries + 1) * max_pages + end_confirmations + 10
    fetcher = make_fetcher(service._http(), max_requests=budget, sleep=sleep)
    fetchers: List[Any] = [fetcher]
    try:
        strategy = strategy_for(monitor, fetcher, platform)
    except ValueError as error:
        _finish(platform, ctx, monitor, run["id"], "failed", error=str(error))
        raise PermanentTaskError(str(error)) from error
    source = strategy.source_name or monitor["source_name"]
    stop_pages = int(monitor.get("incremental_stop_pages") or 3)
    # Incremental listing-date floor (see JobMonitorService.start_run): older listings are not added.
    since_raw = None if full else cp.get("since")
    # "first_page": an empty monitor's first run keeps only the source's newest batch — the floor
    # becomes the listing date of the first page it reads.
    since = date.fromisoformat(str(since_raw)) if since_raw and since_raw != "first_page" else None
    base_requests = int(cp.get("requests", 0))

    def requests_made() -> int:
        return base_requests + sum(getattr(f, "requests", 0) for f in fetchers)

    def save(**changes: Any) -> None:
        nonlocal run
        cp.update(changes)
        cp["requests"] = requests_made()
        run = store.update(ctx, "job_monitor_runs", run["id"], {
            "checkpoint": cp, "pages": cp.get("pages", 0), "found": cp.get("found", 0),
            "rejected_count": cp.get("rejected", 0), "error_count": cp.get("errors", 0),
            "stop_reason": cp.get("stop_reason"), "request_count": cp["requests"],
            "warning_count": cp.get("warning_count", 0), "notes": cp.get("warnings", [])[-50:]})

    def warn(message: str) -> None:
        log.warning("job monitor run %s: %s", run["id"], message)
        cp["warnings"] = (cp.get("warnings") or [])[-49:] + [f"{utcnow().isoformat(timespec='seconds')} {message}"]
        cp["warning_count"] = cp.get("warning_count", 0) + 1

    def read(reader: Any, url: str) -> PageResult:
        try:
            return reader.read(url)
        except Exception as error:  # noqa: BLE001 - a malformed page is a failed page, not a dead run
            log.exception("reading %s failed", url)
            return PageResult(url, Outcome.FAILED, reason=f"unreadable page: {type(error).__name__}: {error}"[:300])

    def wait(seconds: float) -> None:
        """Back off in short steps so cancel / pause requests are noticed."""
        remaining = float(seconds)
        while remaining > 0:
            if reporter.is_cancelled() or reporter.should_pause():
                return
            step = min(5.0, remaining)
            sleep(step)
            remaining -= step

    def read_with_retries(url: str, page_no: int) -> PageResult:
        attempt = 0
        while True:
            result = read(strategy, url)
            if result.ok or result.outcome in REFUSALS or attempt >= page_retries:
                return result
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            warn(f"{result.outcome} on page {page_no}"
                 + (f" (HTTP {result.http_status})" if result.http_status else "")
                 + f"; retry {attempt + 1}/{page_retries} of the same cursor in {delay:g}s")
            save()
            wait(delay)
            if reporter.is_cancelled() or reporter.should_pause():
                return result
            attempt += 1

    def confirm_end(url: str, page_no: int, result: PageResult) -> PageResult:
        """A wedged or rate-limited page looks exactly like the end of the results. Re-read the
        same cursor in fresh sessions; only a consistent "no next page" is believed."""
        for attempt in range(end_confirmations):
            wait(end_delay)
            fresh = make_fetcher(service._http(), max_requests=5, sleep=sleep)
            fetchers.append(fresh)
            again = read(strategy_for(monitor, fresh, platform), url)
            if again.ok and again.next_url:
                warn(f"page {page_no} first showed no next page; a fresh session found one (confirmation "
                     f"{attempt + 1}) - continuing")
                return again
            if not again.ok:
                warn(f"end-of-listing confirmation {attempt + 1} on page {page_no} failed: {again.outcome}")
        if end_confirmations:
            cp["end_confirmed"] = end_confirmations
        return result

    try:
        if cp.get("phase", "listing") == "listing":
            url = cp.get("next_url") or strategy.first_url(monitor)
            while True:
                pages = cp.get("pages", 0)
                if pages >= max_pages:
                    save(stop_reason=f"page limit reached ({max_pages:,} pages)")
                    break
                if reporter.is_cancelled():
                    save(stop_reason="cancelled")
                    _finish(platform, ctx, monitor, run["id"], "cancelled", stop_reason="cancelled by a user")
                    raise TaskCancelled()
                if reporter.should_pause():
                    save(next_url=url)
                    raise TaskPaused({"run_id": run["id"]})
                result = read_with_retries(url, pages + 1)
                if reporter.is_cancelled():
                    continue
                if reporter.should_pause():
                    save(next_url=url)
                    raise TaskPaused({"run_id": run["id"]})
                if result.ok and result.next_url is None and full:
                    result = confirm_end(url, pages + 1, result)
                for problem in result.problems[:5]:
                    warn(f"page {pages + 1}: {problem}")
                if not result.ok:
                    save(errors=cp.get("errors", 0) + 1,
                         stop_reason=f"{result.outcome} on page {pages + 1}"
                                     + (f" (HTTP {result.http_status})" if result.http_status else "")
                                     + (f": {result.reason}" if result.reason else ""), failed_url=url)
                    break
                observed = utcnow()
                values, rejected, older = [], 0, 0
                listed = [d for d in (r.get("listing_date") for r in result.records) if d]
                if listed:
                    newest = max(str(d)[:10] for d in listed)
                    if newest > str(cp.get("newest_listing") or ""):
                        cp["newest_listing"] = newest
                    if since_raw == "first_page" and since is None:
                        since = date.fromisoformat(newest)
                        cp["since"] = since_raw = newest
                for raw in result.records:
                    try:
                        normalized, _problems = normalize_job(raw, source=source, scraped_date=observed.date())
                    except Exception as error:  # noqa: BLE001 - one bad record never stops a sweep
                        warn(f"page {pages + 1}: a record could not be normalised ({type(error).__name__})")
                        normalized = None
                    if normalized is None:
                        rejected += 1
                    elif since and normalized.get("listing_date") and normalized["listing_date"] < since:
                        older += 1          # listed before the floor: not new for this run, not stored
                    else:
                        values.append(normalized)
                batch = service.upsert_batch(ctx, values, observed_at=observed, source_kind="scraper",
                                             source_name=monitor["name"], monitor=monitor, run=run)
                known_page = bool(values) and batch["new"] == 0 and batch["reopened"] == 0
                streak = cp.get("known_streak", 0) + 1 if known_page else 0
                found = cp.get("found", 0) + len(values)
                next_url = result.next_url
                stop = None
                if pages == 0 and not result.records and strategy.name != "jobspy":
                    save(pages=pages + 1, errors=cp.get("errors", 0) + 1,
                         stop_reason="the first page showed no jobs — the page layout may have changed")
                    break
                if next_url is None:
                    stop = "end of the listing" + (f" (confirmed by {cp['end_confirmed']} fresh-session re-reads)"
                                                   if cp.get("end_confirmed") else "")
                elif not full and strategy.newest_first and streak >= stop_pages:
                    stop = f"{streak} pages in a row held only already-known jobs"
                elif older:
                    stop = f"reached jobs listed before {since.isoformat()} (only newer listings are added)"
                save(pages=pages + 1, found=found, rejected=cp.get("rejected", 0) + rejected, known_streak=streak,
                     next_url=next_url, listing_complete=next_url is None, stop_reason=stop)
                if (pages + 1) % LIVE_COUNTS_EVERY == 0:
                    # Live NEW / CHANGED / REOPENED for the monitor page (exact counts again at the end).
                    live = service.run_counts(ctx, run["id"])
                    run = store.update(ctx, "job_monitor_runs", run["id"], {
                        k: live[k] for k in ("new_count", "changed_count", "reopened_count")})
                reporter.progress(f"{monitor['name']}: page {pages + 1:,}, {found:,} jobs read",
                                  run_id=run["id"], pages=pages + 1, found=found)
                if stop:
                    break
                url = next_url

            status = "partial" if cp.get("errors") else "completed"
            if full and status == "completed" and not getattr(strategy, "supports_close", True):
                save(stop_reason=(cp.get("stop_reason") or "done") + " (this source returns search windows, "
                                 "so it never closes jobs)")
                return _finish(platform, ctx, monitor, run["id"], status)
            if full and status == "completed":
                if not cp.get("listing_complete"):
                    status = "partial"
                else:
                    previous = store.first(ctx, "job_monitor_runs", {
                        "monitor_id": monitor["id"], "mode": "full", "status": "completed", "id__ne": run["id"]},
                        order="-finished_at")
                    if previous and previous["found"] > 100 and cp.get("found", 0) < MIN_SWEEP_SHARE * previous["found"]:
                        status = "partial"
                        save(stop_reason=f"only {cp.get('found', 0):,} jobs read but the previous completed sweep "
                                         f"read {previous['found']:,} — treated as incomplete, nothing closed")
            if status != "completed" or not full:
                return _finish(platform, ctx, monitor, run["id"], status)
            save(phase="closing")

        if cp.get("phase") != "checking":
            closed = service.close_missing(ctx, monitor, run,
                                           progress=lambda s: reporter.progress("closing missing jobs",
                                                                                run_id=run["id"], **s))
            save(phase="checking", close_stats=closed)
        closed = dict(cp.get("close_stats") or {"missed": 0, "closed": 0, "expired": 0})
        reporter.progress("checking missed jobs at the source", run_id=run["id"])
        gone_http = _setting(platform, "job_monitor_gone_http", None)
        closed["gone"] = service.check_gone(ctx, monitor, run, http=gone_http,
                                            stop=lambda: reporter.is_cancelled() or reporter.should_pause())
        return _finish(platform, ctx, monitor, run["id"], "completed", closed=closed)
    except (TaskPaused, TaskCancelled, PermanentTaskError):
        raise
    except Exception as error:  # noqa: BLE001 - recorded on the run; the task retries from the checkpoint
        attempts, limit = int(task.get("attempts") or 1), int(task.get("max_attempts") or 1)
        message = f"{type(error).__name__}: {error}"[:2000]
        if attempts >= limit:
            _finish(platform, ctx, monitor, run["id"], "failed", error=message,
                    stop_reason=f"failed after {attempts} attempts")
        else:
            store.update(ctx, "job_monitor_runs", run["id"], {"error": message})
        raise


def run_job_lifecycle_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """The daily lifecycle evaluation of one monitor (queued by ``JobMonitorService.tick``)."""
    from cloud.intel.tasks.worker import PermanentTaskError

    monitor_id = str(task["params"].get("monitor_id") or "")
    if platform.store.find(ctx, "job_source_monitors", monitor_id) is None:
        raise PermanentTaskError("job monitor not found")
    reporter.progress("evaluating stale jobs", monitor_id=monitor_id)
    return platform.service("job_monitors").evaluate_lifecycle(ctx, monitor_id)
