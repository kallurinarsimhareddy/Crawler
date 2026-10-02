"""Job source monitors and the master job record.

``job_postings`` is the single master table. A monitor run or a historical import
hands :meth:`JobMonitorService.upsert_batch` normalised 14-field records; each is
matched by its Job URL key and classified:

* **NEW** — the URL key was never stored: inserted with ``first_seen_run_id``.
* **CHANGED** — a meaningful field differs (content hash): updated, previous values
  kept in ``job_posting_changes``, ``last_changed_at`` / ``last_changed_run_id`` set.
* **UNCHANGED** — only ``last_seen_at`` / ``observation_count`` move.
* **REOPENED** — a CLOSED job is listed again: back to ACTIVE, history kept.

**CLOSED** is decided only after a *completed* full sweep, and only for jobs missing
from ``close_after_missed`` (default 2) completed full sweeps in a row
(:meth:`close_missing`). Failed, partial, cancelled or capped runs never close a job.
Historical rows are never deleted.

Company links follow the CRM rule for name-only sources: a job links to a company
only when its normalised company name matches **exactly one** CRM company. Anything
else goes to ``job_company_reviews``; CRM companies are never created from jobs.

Statuses: ``open`` (shown as ACTIVE), ``closed`` (CLOSED), ``unknown`` (UNKNOWN:
imported, not yet observed by a monitor).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlsplit

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_name
from cloud.intel.job_monitor import diff as jdiff
from cloud.intel.job_monitor.profiles import PROFILES, profile_for_url
from cloud.intel.job_monitor.schema import (HASH_COLUMNS, JOB_FIELDS, KEYWORD_COLUMNS, content_hash, field_values,
                                            normalize_job)
from cloud.intel.job_monitor.relevance import ENGINE_VERSION, RelevanceEngine, parse_keyword_workbook
from cloud.intel.jobs.classify import classify as classify_job

__all__ = ["JobMonitorService", "STATUS_LABELS", "SCHEDULE_STEPS"]

log = logging.getLogger(__name__)

STATUS_LABELS = {"open": "ACTIVE", "stale": "STALE", "closed": "CLOSED", "expired": "EXPIRED", "unknown": "UNKNOWN"}
STATUS_VALUES = {v: k for k, v in STATUS_LABELS.items()}
#: A job a source currently shows (STALE is still active, only old).
ACTIVE_STATES = ("open", "stale")
#: Jobs a completed full sweep is responsible for: absent from it, they miss a sweep or expire.
SWEEP_STATES = ("open", "stale", "unknown")
#: Not shown by the source any more; seeing one again REOPENS it.
ENDED_STATES = ("closed", "expired")
LIFECYCLE_STEP = timedelta(days=1)
#: A job whose own URL was checked recently is not checked again before this.
GONE_RECHECK = timedelta(days=7)
SCHEDULE_STEPS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}
ACTIVE_RUN_STATES = ("queued", "running")
FINISHED_RUN_STATES = ("completed", "partial", "failed", "cancelled")
BATCH = 500
#: Columns a monitor observation may overwrite; Source / Scraped Date are bookkeeping.
OBSERVED_COLUMNS = ("job_url", "title", "company_name", "location", "experience_level", "salary_budget",
                    *KEYWORD_COLUMNS, "remote", "content_hash")
#: Fields the jobs query accepts in conditions (field -> column; "keyword" spans keyword_1-5).
QUERY_FIELDS = {
    "source": "source", "company": "company_name", "company_name": "company_name", "title": "title",
    "job_title": "title", "location": "location", "country": "country", "experience": "experience_level",
    "experience_level": "experience_level", "salary": "salary_budget", "salary_budget": "salary_budget",
    "remote": "remote", "keyword": "keyword", "scraped_date": "scraped_date", "first_seen": "first_seen_at",
    "first_seen_at": "first_seen_at", "last_seen": "last_seen_at", "last_seen_at": "last_seen_at",
    "last_changed": "last_changed_at", "last_changed_at": "last_changed_at", "status": "status",
    "monitor": "source_monitor_id", "company_id": "company_id", "source_board": "source_board",
    "search_term": "search_term", "relevance_score": "relevance_score", "relevance": "relevance_class",
    "relevance_class": "relevance_class",
}
QUERY_OPS = ("eq", "ne", "contains", "in", "gte", "lte", "gt", "lt", "empty", "not_empty")
SORTABLE = ("first_seen_at", "last_seen_at", "last_changed_at", "scraped_date", "title", "company_name",
            "location", "created_at", "relevance_score", "posted_at")


def _chunks(items: Sequence[Any], size: int = BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _day_start(day: Optional[date]) -> Optional[datetime]:
    return datetime.combine(day, dtime(0, 0), tzinfo=timezone.utc) if day else None


def _parse_when(value: Any, *, end: bool = False) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return _day_start(day) + (timedelta(days=1) - timedelta(microseconds=1) if end else timedelta())
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValidationError(f"unreadable date {text!r}") from error


class JobMonitorService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # ------------------------------------------------------------------------------------
    # monitors
    # ------------------------------------------------------------------------------------

    @staticmethod
    def detect(source_url: str) -> Dict[str, Any]:
        """Which strategy/profile a URL gets, and the filters already in its query string."""
        parts = urlsplit(source_url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValidationError("the source URL must be an http(s) address")
        profile = profile_for_url(source_url)
        filters = {k: v for k, v in parse_qsl(parts.query) if v not in ("",)}
        return {
            # WeAreDevelopers: the turbo-stream cursor listing read over plain HTTP ("wad_turbo")
            "strategy": ("wad_turbo" if profile.name == "wearedevelopers" else "site_profile") if profile
            else "ai_scraper",
            "profile": profile.name if profile else None,
            "source_name": profile.source_name if profile else parts.hostname.removeprefix("www."),
            "newest_first": bool(profile and profile.newest_first),
            "filters": filters,
            "fields": list(JOB_FIELDS),
        }

    def plan(self, ctx: Ctx, source_url: str, *, name: Optional[str] = None, schedule: str = "daily",
             preview: bool = False) -> Dict[str, Any]:
        """What a monitor for ``source_url`` would do. ``preview`` reads the first page (one
        request through the safe fetcher) and returns the jobs it shows; nothing is saved."""
        detected = self.detect(source_url)
        country = detected["filters"].get("country")
        plan = {
            "name": name or f"{detected['source_name']}{' ' + country if country else ''} Jobs",
            "source_url": source_url, "schedule": schedule if schedule in SCHEDULE_STEPS or schedule == "manual"
            else "daily", **detected,
            "steps": ["Read the listing newest-first over plain HTTP (no browser, no AI)",
                      "Extract the 14 job fields; missing values stay blank",
                      "Compare every Job URL with the master job database",
                      "Store NEW jobs, update CHANGED jobs, keep everything else",
                      "Daily: stop once pages show only already-known jobs",
                      "Weekly full sweep: a job missing from 2 completed sweeps becomes CLOSED",
                      "Notify SANA GTM with the new / changed / closed counts"],
        }
        if preview:
            from cloud.intel.job_monitor.strategies import make_fetcher, strategy_for

            fetcher = make_fetcher(self._http(), max_requests=3, max_retries=1)
            strategy = strategy_for({"source_url": source_url, **detected}, fetcher)
            result = strategy.read(strategy.first_url({"source_url": source_url, "filters": detected["filters"]}))
            sample = []
            for raw in result.records[:25]:
                values, problems = normalize_job(raw, source=detected["source_name"], scraped_date=utcnow().date())
                if values:
                    sample.append({**field_values(values), "problems": problems})
            plan["preview"] = {"outcome": result.outcome, "reason": result.reason, "jobs_on_page": result.cards,
                               "has_next_page": bool(result.next_url), "sample": sample,
                               "filled": {label: sum(1 for s in sample if s.get(label) not in (None, ""))
                                          for label in JOB_FIELDS}}
        return plan

    def _http(self) -> Any:
        from cloud.intel.core.http import SafeFetcher

        factory = self.platform.config.extra.get("fetcher_factory")
        return factory() if factory else SafeFetcher(per_host_delay=1.0)

    def _jobspy_values(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        from cloud.intel.job_monitor.jobspy_source import BOARD_LABELS, BOARD_URLS, validate_jobspy_filters

        params = validate_jobspy_filters(values.get("filters") or values)
        boards = ", ".join(BOARD_LABELS[b] for b in params["boards"])
        return {**values, "source_url": BOARD_URLS[params["boards"][0]], "filters": params,
                "name": values.get("name") or f"JobSpy {boards} - {params['location']}",
                "source_name": "JobSpy", "strategy": "jobspy", "profile": None, "source_type": "job_board"}

    def create_monitor(self, ctx: Ctx, values: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        if values.get("strategy") == "jobspy" or values.get("source") == "jobspy":
            values = self._jobspy_values(values)
        source_url = str(values.get("source_url") or "").strip()
        detected = self.detect(source_url)
        strategy = values.get("strategy") or detected["strategy"]
        profile = values.get("profile") or detected["profile"]
        if strategy == "wad_turbo":
            profile = "wearedevelopers"
        if strategy in ("site_profile", "wad_turbo") and profile not in PROFILES:
            raise ValidationError(f"no site profile named {profile!r}")
        schedule = values.get("schedule") or "daily"
        now = utcnow()
        row = {
            "name": str(values.get("name") or self.plan(ctx, source_url)["name"])[:200],
            "source_url": source_url, "source_name": str(values.get("source_name") or detected["source_name"])[:200],
            "source_type": values.get("source_type") or "job_board", "strategy": strategy, "profile": profile,
            "filters": dict(values.get("filters") or detected["filters"]),
            "extraction_schema": list(values.get("extraction_schema") or JOB_FIELDS),
            "schedule": schedule, "enabled": bool(values.get("enabled", True)),
            "outputs": [o for o in (values.get("outputs") or ["sana"]) if o == "sana"] or ["sana"],
        }
        for key in ("full_sweep_days", "close_after_missed", "incremental_stop_pages", "max_pages_incremental",
                    "max_pages_full", "stale_after_days", "gone_checks_per_run", "visible_window_days"):
            if values.get(key) is not None:
                row[key] = int(values[key])
        if "visible_window_days" not in row and profile in PROFILES:
            row["visible_window_days"] = PROFILES[profile].visible_window_days
        row["next_lifecycle_at"] = now   # first daily evaluation on the next maintenance pass
        step = SCHEDULE_STEPS.get(schedule)
        # "Run now" starts a run immediately (below); the schedule still begins one period later,
        # so the scheduler does not start a second run right after the first.
        row["next_run_at"] = now + step if step else None
        # auto_full_sweep=False: the scheduler never starts a full sweep (they stay manual) — e.g.
        # until the database has room for a whole listing. next_full_sweep_at NULL means exactly that.
        row["next_full_sweep_at"] = (now + timedelta(days=int(row.get("full_sweep_days") or 7))
                                     if values.get("auto_full_sweep", True) else None)
        try:
            monitor = self.store.insert(ctx, "job_source_monitors", row)
        except ConflictError as error:
            raise ConflictError(f"a monitor named {row['name']!r} already exists") from error
        audit(self.store, ctx, "job_monitor.created", entity_type="job_source_monitors", entity_id=monitor["id"],
              summary=f"{monitor['name']}: {source_url} ({schedule})")
        if values.get("run_now"):
            self.start_run(ctx, monitor["id"], mode=values.get("mode") or "incremental", trigger="manual")
        return self.store.get(ctx, "job_source_monitors", monitor["id"])

    EDITABLE = ("name", "schedule", "enabled", "filters", "full_sweep_days", "close_after_missed",
                "incremental_stop_pages", "max_pages_incremental", "max_pages_full", "source_url",
                "stale_after_days", "visible_window_days", "gone_checks_per_run")

    def update_monitor(self, ctx: Ctx, monitor_id: str, changes: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_write()
        monitor = self.store.get(ctx, "job_source_monitors", monitor_id)
        clean = {k: v for k, v in changes.items() if k in self.EDITABLE}
        if "source_url" in clean:
            self.detect(str(clean["source_url"]))
        if "filters" in clean and monitor["strategy"] == "jobspy":
            from cloud.intel.job_monitor.jobspy_source import validate_jobspy_filters

            clean["filters"] = validate_jobspy_filters(clean["filters"])
        if clean.get("schedule") and clean["schedule"] != monitor["schedule"]:
            step = SCHEDULE_STEPS.get(clean["schedule"])
            clean["next_run_at"] = utcnow() + step if step else None
        if clean.get("enabled") is True and not monitor["enabled"] and monitor["schedule"] in SCHEDULE_STEPS:
            clean["next_run_at"] = utcnow() + SCHEDULE_STEPS[monitor["schedule"]]
        if "auto_full_sweep" in changes:
            days = int(clean.get("full_sweep_days") or monitor.get("full_sweep_days") or 7)
            clean["next_full_sweep_at"] = utcnow() + timedelta(days=days) if changes["auto_full_sweep"] else None
        row = self.store.update(ctx, "job_source_monitors", monitor_id, clean)
        audit(self.store, ctx, "job_monitor.updated", entity_type="job_source_monitors", entity_id=monitor_id,
              summary=f"{row['name']}: {', '.join(sorted(clean))}")
        return row

    def active_run(self, ctx: Ctx, monitor_id: str) -> Optional[Dict[str, Any]]:
        return self.store.first(ctx, "job_monitor_runs", {"monitor_id": monitor_id, "status": list(ACTIVE_RUN_STATES)})

    def start_run(self, ctx: Ctx, monitor_id: str, *, mode: str = "incremental", trigger: str = "manual",
                  since: Optional[Any] = None) -> Dict[str, Any]:
        """``since`` (incremental only): add only jobs the source lists on or after that date and
        stop at the first older listing. Without it, an incremental run's floor is the day before
        the monitor's last run (so a daily run adds only what was listed since the previous check),
        and a monitor's first run on an empty database gets ``since = today`` — today's postings,
        never the source's backlog. Sources that show no listing date are unaffected."""
        ctx.require_write()
        if mode not in ("incremental", "full"):
            raise ValidationError("mode must be incremental or full")
        monitor = self.store.get(ctx, "job_source_monitors", monitor_id)
        floor: Optional[Any] = None
        if since not in (None, ""):
            from cloud.intel.job_monitor.schema import parse_date

            floor = parse_date(since)
            if floor is None:
                raise ValidationError("since must be a date (YYYY-MM-DD)")
            if mode != "incremental":
                raise ValidationError("since applies to incremental runs only (a full sweep reads everything)")
        elif mode == "incremental":
            # The floor comes from the SOURCE's own listing dates, never our clock (WeAreDevelopers
            # dates a fresh posting a day or two back): one day below the newest listing date the
            # previous run saw. A first run on an empty monitor takes the source's newest batch
            # (the first page's listing date). Without a recorded date the known-pages rule decides.
            previous = self.store.first(ctx, "job_monitor_runs", {"monitor_id": monitor_id,
                                                                  "status": ["completed", "partial"]},
                                        order="-finished_at")
            newest = ((previous or {}).get("checkpoint") or {}).get("newest_listing")
            if newest:
                floor = date.fromisoformat(newest) - timedelta(days=1)
            elif previous is None and not self.store.count(ctx, "job_postings", {"source_monitor_id": monitor_id}):
                floor = "first_page"
        running = self.active_run(ctx, monitor_id)
        if running is not None:
            raise ConflictError(f"{monitor['name']} already has a {running['status']} run")
        # One crawler per source site at a time, whichever monitor it belongs to.
        host = (urlsplit(monitor["source_url"]).hostname or "").removeprefix("www.")
        for other in self.store.all(ctx, "job_monitor_runs", {"status": list(ACTIVE_RUN_STATES)}, cap=200):
            other_monitor = self.store.find(ctx, "job_source_monitors", other["monitor_id"])
            other_host = (urlsplit((other_monitor or {}).get("source_url") or "").hostname or "").removeprefix("www.")
            if other_monitor and other_host == host and monitor["strategy"] != "jobspy":
                raise ConflictError(f"{other_monitor['name']} is already reading {host}; one crawler per site")
        run = self.store.insert(ctx, "job_monitor_runs", {"monitor_id": monitor_id, "mode": mode, "trigger": trigger,
                                                          "status": "queued",
                                                          **({"checkpoint": {"since": floor if isinstance(floor, str)
                                                                                else floor.isoformat()}}
                                                             if floor else {})})
        task = self.platform.tasks.submit(ctx, "job_monitor", {"run_id": run["id"]}, max_attempts=5,
                                          idempotency_key=f"job_monitor:{run['id']}", entity_type="job_monitor_runs",
                                          entity_id=run["id"])
        run = self.store.update(ctx, "job_monitor_runs", run["id"], {"task_id": task["id"]})
        audit(self.store, ctx, "job_monitor.run_started", entity_type="job_monitor_runs", entity_id=run["id"],
              summary=f"{monitor['name']}: {mode} run ({trigger})")
        return run

    def tick(self, ctx: Ctx, *, now: Optional[datetime] = None) -> int:
        """Start every enabled monitor that is due (worker maintenance, per workspace). A
        due full sweep replaces that day's incremental run. Cheap when nothing is due."""
        now = now or utcnow()
        started = 0
        for monitor in self.store.all(ctx, "job_source_monitors", {"enabled": True, "next_run_at__lte": now},
                                      cap=1000):
            step = SCHEDULE_STEPS.get(monitor["schedule"])
            if step is None:
                continue
            nxt = (monitor["next_run_at"] or now) + step
            while nxt <= now:  # a monitor that was off for a while runs once, not once per missed day
                nxt += step
            sweep_due = monitor.get("next_full_sweep_at") is not None and monitor["next_full_sweep_at"] <= now
            self.store.update(ctx, "job_source_monitors", monitor["id"], {"next_run_at": nxt})
            if self.active_run(ctx, monitor["id"]) is not None:
                continue
            try:
                self.start_run(ctx, monitor["id"], mode="full" if sweep_due else "incremental", trigger="schedule")
                started += 1
            except Exception:  # noqa: BLE001 - one monitor must not stop the others
                log.exception("could not start job monitor %s", monitor["id"])
        queued = self.schedule_lifecycle(ctx, now=now)
        if queued:
            log.info("queued %d daily job lifecycle evaluation(s)", queued)
        return started

    def schedule_lifecycle(self, ctx: Ctx, *, now: Optional[datetime] = None) -> int:
        """Queue the daily lifecycle evaluation (stale roles, closure candidates, signals) of
        every enabled monitor that is due: one ``job_lifecycle`` task per monitor per UTC day."""
        now = now or utcnow()
        queued = 0
        due = self.store.all(ctx, "job_source_monitors", {"enabled": True, "any_of": [
            {"next_lifecycle_at__lte": now}, {"next_lifecycle_at__isnull": True}]}, cap=1000)
        for monitor in due:
            nxt = monitor.get("next_lifecycle_at") or now
            while nxt <= now:
                nxt += LIFECYCLE_STEP
            self.store.update(ctx, "job_source_monitors", monitor["id"], {"next_lifecycle_at": nxt})
            try:
                self.platform.tasks.submit(ctx, "job_lifecycle", {"monitor_id": monitor["id"]}, max_attempts=3,
                                           idempotency_key=f"job_lifecycle:{monitor['id']}:{now.date().isoformat()}",
                                           entity_type="job_source_monitors", entity_id=monitor["id"])
                queued += 1
            except Exception:  # noqa: BLE001 - one monitor must not stop the others
                log.exception("could not queue the lifecycle evaluation of job monitor %s", monitor["id"])
        return queued

    # ------------------------------------------------------------------------------------
    # the master record
    # ------------------------------------------------------------------------------------

    def _companies_for(self, ctx: Ctx, names: Iterable[str]) -> Dict[str, Tuple[Optional[str], str, List[str]]]:
        """normalised name -> (company_id, "matched" | "review", candidate ids). Exact normalised
        name only; zero or several candidates go to review."""
        keys = sorted({k for k in (normalize_name(n) for n in names if n) if k})
        out: Dict[str, Tuple[Optional[str], str, List[str]]] = {k: (None, "review", []) for k in keys}
        for chunk in _chunks(keys):
            rows = self.store.all(ctx, "companies", {"normalized_name": list(chunk), "status__ne": "merged"},
                                  cap=5 * len(chunk) + 10)
            by_key: Dict[str, List[str]] = {}
            for row in rows:
                by_key.setdefault(row["normalized_name"], []).append(row["id"])
            for key, ids in by_key.items():
                out[key] = (ids[0], "matched", ids) if len(ids) == 1 else (None, "review", ids[:10])
        return out

    def _queue_reviews(self, ctx: Ctx, pending: Mapping[str, Tuple[str, List[str], int]]) -> None:
        """Record unmatched company names for review (one row per normalised name)."""
        if not pending:
            return
        keys = list(pending)
        existing: Dict[str, Dict[str, Any]] = {}
        for chunk in _chunks(keys):
            for row in self.store.all(ctx, "job_company_reviews", {"normalized_name": list(chunk)}, cap=len(chunk) + 5):
                existing[row["normalized_name"]] = row
        now = utcnow()
        updates = [(row["id"], {"job_count": row["job_count"] + pending[key][2], "last_seen_at": now})
                   for key, row in existing.items()]
        inserts = [{"company_name": name[:300], "normalized_name": key[:300], "status": "pending",
                    "reason": "several CRM companies share this name" if candidates else "no CRM company has this name",
                    "candidate_ids": candidates, "job_count": count, "last_seen_at": now}
                   for key, (name, candidates, count) in pending.items() if key not in existing]
        if updates:
            self.store.update_many(ctx, "job_company_reviews", updates)
        if inserts:
            try:
                self.store.insert_many(ctx, "job_company_reviews", inserts)
            except ConflictError:  # a concurrent batch queued the same name
                for row in inserts:
                    try:
                        self.store.insert(ctx, "job_company_reviews", row)
                    except ConflictError:
                        pass

    # ------------------------------------------------------------------------------------
    # relevance
    # ------------------------------------------------------------------------------------

    def upload_keyword_set(self, ctx: Ctx, filename: str, data: bytes, *, name: Optional[str] = None,
                           activate: bool = True) -> Dict[str, Any]:
        """Store a keyword workbook (e.g. IT_Crawler_Keywords.xlsx) as the relevance universe."""
        ctx.require_write()
        try:
            parsed = parse_keyword_workbook(data)
        except Exception as error:  # noqa: BLE001 - an unreadable workbook is the user's input problem
            raise ValidationError(f"could not read the keyword workbook: {error}") from error
        row = self.store.insert(ctx, "job_keyword_sets", {
            "name": (name or filename)[:200], "filename": filename[:300], "keywords": parsed["keywords"],
            "categories": parsed["categories"], "problems": parsed["problems"],
            "keyword_count": parsed["distinct_keywords"], "thresholds": {"high": 70, "review": 40}})
        audit(self.store, ctx, "job_keywords.uploaded", entity_type="job_keyword_sets", entity_id=row["id"],
              summary=f"{filename}: {parsed['distinct_keywords']} keywords, {len(parsed['categories'])} categories")
        return self.activate_keyword_set(ctx, row["id"]) if activate else row

    def activate_keyword_set(self, ctx: Ctx, set_id: str) -> Dict[str, Any]:
        ctx.require_write()
        row = self.store.get(ctx, "job_keyword_sets", set_id)
        others = [(r["id"], {"active": False}) for r in self.store.all(ctx, "job_keyword_sets", {"active": True})
                  if r["id"] != set_id]
        if others:
            self.store.update_many(ctx, "job_keyword_sets", others)
        self._engines = {}
        return self.store.update(ctx, "job_keyword_sets", row["id"], {"active": True})

    def engine(self, ctx: Ctx) -> Optional[RelevanceEngine]:
        """The workspace's active relevance engine (cached per keyword-set version), or None."""
        active = self.store.first(ctx, "job_keyword_sets", {"active": True})
        if active is None:
            return None
        cache = getattr(self, "_engines", None)
        if cache is None:
            cache = self._engines = {}
        key = (ctx.workspace_id, active["id"], active["version"])
        if key not in cache:
            cache[key] = RelevanceEngine(active["keywords"], groups=active.get("groups") or {},
                                         thresholds=active.get("thresholds") or {},
                                         negative_terms=active.get("negative_terms") or [],
                                         version=f"{ENGINE_VERSION}:{active['id']}:{active['version']}")
        return cache[key]

    @staticmethod
    def _relevance(engine: Optional[RelevanceEngine], values: Mapping[str, Any]) -> Dict[str, Any]:
        if engine is None:
            return {}
        tags = [values.get(k) for k in KEYWORD_COLUMNS if values.get(k)]
        result = engine.score(title=values.get("title") or "", description=values.get("description") or "",
                              tags=tags, search_term=values.get("search_term")).as_dict()
        result["relevance_version"] = engine.version
        return result

    def score_text(self, ctx: Ctx, *, title: str, description: str = "", search_term: Optional[str] = None
                   ) -> Dict[str, Any]:
        engine = self.engine(ctx)
        if engine is None:
            raise ValidationError("upload a keyword workbook first (no active keyword set)")
        return engine.score(title=title, description=description, search_term=search_term).as_dict()

    def rescore(self, ctx: Ctx, *, limit: int = 5000) -> Dict[str, int]:
        """Re-score stored jobs whose relevance is missing or from another keyword-set version."""
        ctx.require_write()
        engine = self.engine(ctx)
        if engine is None:
            raise ValidationError("no active keyword set")
        done = 0
        last_id = ""
        while done < limit:
            filters: Dict[str, Any] = {"any_of": [{"relevance_version__isnull": True},
                                                  {"relevance_version__ne": engine.version}]}
            if last_id:
                filters["id__gt"] = last_id
            rows = self.store.rows(ctx, "job_postings", filters, order="id", limit=BATCH)
            if not rows:
                break
            last_id = rows[-1]["id"]
            self.store.update_many(ctx, "job_postings", [(r["id"], self._rescored(engine, r)) for r in rows])
            done += len(rows)
        return {"rescored": done}

    def _rescored(self, engine: RelevanceEngine, row: Mapping[str, Any]) -> Dict[str, Any]:
        """New relevance for a stored job. Keyword 1-5 that were *derived* from the previous
        matches (the source showed no skill tags) are re-derived from the new matches;
        keywords the source itself showed are never touched."""
        kws = [row.get(k) for k in KEYWORD_COLUMNS if row.get(k)]
        previous = list(row.get("matched_keywords") or [])
        derived = bool(kws) and kws == previous[:len(kws)] and len(kws) == min(5, len(previous))
        base = {**row, **{k: None for k in KEYWORD_COLUMNS}} if derived else row
        patch = self._relevance(engine, base)
        if derived:
            fresh = (patch.get("matched_keywords", []) + [None] * 5)[:5]
            patch.update(dict(zip(KEYWORD_COLUMNS, fresh)))
            patch["content_hash"] = content_hash({**row, **patch})
            patch = {**self._relevance(engine, {**row, **patch}), **{k: patch[k] for k in (*KEYWORD_COLUMNS,
                                                                                         "content_hash")}}
        return patch

    def upsert_batch(self, ctx: Ctx, records: Sequence[Mapping[str, Any]], *, observed_at: datetime,
                     source_kind: str, source_name: str, monitor: Optional[Mapping[str, Any]] = None,
                     run: Optional[Mapping[str, Any]] = None, import_id: Optional[str] = None) -> Dict[str, Any]:
        """Store normalised records (from :func:`normalize_job`). ``monitor``/``run`` mean a live
        observation (jobs become ACTIVE); ``import_id`` means historical rows (new jobs are
        UNKNOWN, and an existing job only gains values it is missing — older data never
        overwrites what a monitor observed)."""
        stats = {"found": len(records), "new": 0, "changed": 0, "unchanged": 0, "reopened": 0, "duplicates": 0,
                 "filled": 0, "updated": 0, "linked": 0, "review": 0}
        observing = run is not None
        unique: Dict[str, Mapping[str, Any]] = {}
        for values in records:
            if values["url_key"] in unique:
                stats["duplicates"] += 1
                continue
            unique[values["url_key"]] = values
        if not unique:
            return {**stats, "new_ids": []}
        existing: Dict[str, Dict[str, Any]] = {}
        for chunk in _chunks(list(unique)):
            for row in self.store.all(ctx, "job_postings", {"url_key": list(chunk)}, cap=len(chunk) + 5):
                existing[row["url_key"]] = row

        names = [v.get("company_name") for k, v in unique.items()
                 if v.get("company_name") and (k not in existing or not existing[k].get("company_id"))]
        companies = self._companies_for(ctx, names)
        reviews: Dict[str, Tuple[str, List[str], int]] = {}

        def link(values: Mapping[str, Any]) -> Dict[str, Any]:
            key = normalize_name(values.get("company_name")) if values.get("company_name") else None
            if not key:
                return {"company_id": None, "company_match": "unmatched"}
            company_id, match, candidates = companies.get(key, (None, "review", []))
            if match == "review":
                name, cands, count = reviews.get(key, (values["company_name"], candidates, 0))
                reviews[key] = (name, cands, count + 1)
            return {"company_id": company_id, "company_match": match}

        engine = self.engine(ctx)
        run_id = run["id"] if run else None
        monitor_id = monitor["id"] if monitor else None
        inserts: List[Dict[str, Any]] = []
        updates: List[Tuple[str, Dict[str, Any]]] = []
        history: List[Dict[str, Any]] = []
        for key, values in unique.items():
            row = existing.get(key)
            relevance = self._relevance(engine, values)
            if engine is not None and not any(values.get(k) for k in KEYWORD_COLUMNS) and \
                    relevance.get("matched_keywords"):
                # No skill tags from the source: Keyword 1-5 = the strongest workbook keywords the
                # job itself shows (title first), never anything outside the job's own text. Done
                # before the comparison so every observation of the job is treated the same way.
                padded = (relevance["matched_keywords"] + [None] * 5)[:5]
                values = {**values, **dict(zip(KEYWORD_COLUMNS, padded))}
                values["content_hash"] = content_hash(values)
            if row is None:
                c = classify_job(values["title"], values.get("description") or "", values.get("location") or "")
                first_seen = observed_at if observing else (_day_start(values.get("scraped_date")) or observed_at)
                insert = {
                    **values, **link(values),
                    "normalized_title": re.sub(r"\s+", " ", values["title"].lower())[:500],
                    "country": c["country"], "workplace_type": "remote" if values.get("remote") == "Remote"
                    else ("hybrid" if values.get("remote") == "Hybrid" else c["workplace_type"]),
                    "department": c["department"], "seniority": c["seniority"], "technologies": c["technologies"],
                    "skills": [values[k] for k in KEYWORD_COLUMNS if values.get(k)],
                    "source_kind": source_kind, "source_name": source_name[:200],
                    "status": "open" if observing else "unknown",
                    "first_seen_at": first_seen, "last_seen_at": first_seen,
                    "source_monitor_id": monitor_id, "first_seen_run_id": run_id, "last_seen_run_id": run_id,
                    "observation_count": 1 if observing else 0, **relevance,
                }
                if import_id:
                    insert["last_import_id"] = import_id
                if insert["company_match"] == "matched":
                    stats["linked"] += 1
                elif insert["company_match"] == "review":
                    stats["review"] += 1
                inserts.append(insert)
                stats["new"] += 1
                continue

            if not observing:
                if import_id and row.get("last_import_id") == import_id:
                    stats["duplicates"] += 1        # the same URL earlier in this file (another batch)
                    continue
                patch_i: Dict[str, Any] = {"last_import_id": import_id} if import_id else {}
                fill = {c: values[c] for c in (*OBSERVED_COLUMNS, "source", "scraped_date", "source_board",
                                               "search_term")
                        if c != "content_hash" and row.get(c) in (None, "") and values.get(c) not in (None, "")}
                changed = []
                if not row.get("observation_count"):
                    # Import-owned job (no monitor has observed it): a newer file may correct its
                    # meaningful fields. Blank cells never erase a stored value; a job a monitor
                    # observed is never overwritten by a file — only its blanks are filled.
                    changed = [c for c in HASH_COLUMNS if values.get(c) not in (None, "") and
                               row.get(c) not in (None, "") and
                               (str(values.get(c)).strip().casefold() != str(row.get(c)).strip().casefold())]
                if changed or fill:
                    merged = {**row, **fill, **{c: values.get(c) for c in changed}}
                    patch_i.update(fill)
                    patch_i.update({c: values.get(c) for c in changed})
                    patch_i["content_hash"] = content_hash(merged)
                    if changed:
                        patch_i["last_changed_at"] = observed_at
                        history.append({"job_posting_id": row["id"], "monitor_id": None, "run_id": None,
                                        "import_id": import_id, "change": "changed", "changed_fields": changed,
                                        "before": _jsonable({c: row.get(c) for c in changed}),
                                        "after": _jsonable({c: values.get(c) for c in changed}),
                                        "detected_at": observed_at})
                        if engine is not None:
                            patch_i.update(self._relevance(engine, merged))
                    stats["updated"] += 1
                    if fill and not changed:
                        stats["filled"] += 1
                else:
                    stats["unchanged"] += 1
                if patch_i:
                    updates.append((row["id"], patch_i))
                continue

            change = jdiff.classify(row, values)
            patch: Dict[str, Any] = {"last_seen_at": observed_at, "last_seen_run_id": run_id,
                                     "observation_count": (row.get("observation_count") or 0) + 1,
                                     "missed_full_sweeps": 0, "missed_run_id": None}
            if row.get("source_monitor_id") is None:
                patch["source_monitor_id"] = monitor_id
            if row["status"] == "unknown":
                patch["status"] = "open"
            if values.get("listing_date") and values["listing_date"] != row.get("listing_date"):
                patch["listing_date"] = values["listing_date"]
            if not row.get("company_id"):
                patch.update(link(values))
            if change.kind == jdiff.REOPENED:
                # Seen again after it was closed or expired: active again, from now (the stale
                # clock restarts at reopened_at); closure evidence is cleared, history keeps it.
                patch.update({"status": "open", "closed_at": None, "closure_reason": None, "expired_at": None,
                              "stale_at": None, "reopened_at": observed_at, "gone_status": None,
                              "gone_checked_at": None})
                stats["reopened"] += 1
            if change.changed_fields:
                patch.update({c: values.get(c) for c in change.changed_fields})
                patch.update({"content_hash": values["content_hash"], "last_changed_at": observed_at,
                              "last_changed_run_id": run_id, "job_url": values["job_url"]})
                if change.kind == jdiff.CHANGED:
                    stats["changed"] += 1
            elif not row.get("content_hash"):
                patch["content_hash"] = values["content_hash"]
            # The search term / board that first found a job stay; text and post date follow the source.
            for extra in ("description", "posted_at", "search_term", "source_board"):
                if extra in ("search_term", "source_board") and row.get(extra):
                    continue
                if values.get(extra) and values.get(extra) != row.get(extra):
                    patch[extra] = values[extra]
            if engine is not None and (change.changed_fields or "description" in patch
                                       or row.get("relevance_version") != engine.version):
                patch.update(self._relevance(engine, {**row, **patch}))
            if change.kind == jdiff.UNCHANGED:
                stats["unchanged"] += 1
            if change.kind in (jdiff.CHANGED, jdiff.REOPENED):
                before, after = _jsonable(change.before), _jsonable(change.after)
                if change.kind == jdiff.REOPENED:
                    before = {**before, "status": STATUS_LABELS.get(row["status"], row["status"]),
                              "closure_reason": row.get("closure_reason")}
                    after = {**after, "status": "ACTIVE"}
                history.append({"job_posting_id": row["id"], "monitor_id": monitor_id, "run_id": run_id,
                                "change": change.kind, "changed_fields": change.changed_fields,
                                "before": before, "after": after, "detected_at": observed_at})
            updates.append((row["id"], patch))

        new_ids: List[str] = []
        for chunk in _chunks(inserts):
            try:
                rows = self.store.insert_many(ctx, "job_postings", chunk)
            except ConflictError:  # a concurrent writer stored one of these URLs first
                rows = []
                for one in chunk:
                    try:
                        rows.append(self.store.insert(ctx, "job_postings", one))
                    except ConflictError:
                        stats["new"] -= 1
                        stats["duplicates"] += 1
            new_ids.extend(r["id"] for r in rows)
            if observing:
                history.extend({"job_posting_id": r["id"], "monitor_id": monitor_id, "run_id": run_id,
                                "change": "new", "changed_fields": [], "before": {},
                                "after": {"title": r["title"], "company_name": r.get("company_name")},
                                "detected_at": observed_at} for r in rows)
        for chunk in _chunks(updates):
            self.store.update_many(ctx, "job_postings", list(chunk))
        for chunk in _chunks(history):
            self.store.insert_many(ctx, "job_posting_changes", list(chunk))
        self._queue_reviews(ctx, reviews)
        return {**stats, "new_ids": new_ids}

    def close_scope(self, ctx: Ctx, monitor: Mapping[str, Any]) -> Dict[str, Any]:
        """Jobs a full sweep of ``monitor`` is responsible for: its own, plus imported jobs of
        the same Source that no monitor owns yet — but only when this is the only monitor
        for that Source (two monitors over one site with different filters must not close
        each other's jobs)."""
        siblings = self.store.count(ctx, "job_source_monitors", {"source_name": monitor["source_name"],
                                                                 "id__ne": monitor["id"]})
        if siblings:
            return {"source_monitor_id": monitor["id"]}
        return {"any_of": [{"source_monitor_id": monitor["id"]},
                           {"source_monitor_id": None, "source": monitor["source_name"]}]}

    def close_missing(self, ctx: Ctx, monitor: Mapping[str, Any], run: Mapping[str, Any], *,
                      progress: Optional[Any] = None) -> Dict[str, int]:
        """After a COMPLETED full sweep: every in-scope ACTIVE/UNKNOWN job not seen since the
        sweep started misses one sweep; at ``close_after_missed`` consecutive misses it is
        CLOSED. Idempotent per run (``missed_run_id``), so a resumed task never counts twice."""
        threshold = int(monitor.get("close_after_missed") or 2)
        started = run["started_at"]
        now = utcnow()
        window = monitor.get("visible_window_days")
        # A job last listed before this date cannot appear in the source's listing any more: its
        # absence says nothing about closure, so it EXPIRES (kept, history recorded) instead.
        horizon = (started.date() - timedelta(days=int(window))) if window else None
        stats = {"missed": 0, "closed": 0, "expired": 0}
        # Missed = not seen BY THIS RUN (run id, not timestamps: robust to clock resolution).
        base = {"all_of": [self.close_scope(ctx, monitor), {"status": list(SWEEP_STATES)},
                           {"any_of": [{"last_seen_run_id__ne": run["id"]}, {"last_seen_run_id__isnull": True}]}]}
        last_id = ""
        while True:
            filters = dict(base)
            if last_id:
                filters["id__gt"] = last_id
            rows = self.store.rows(ctx, "job_postings", filters, order="id", limit=BATCH)
            if not rows:
                break
            last_id = rows[-1]["id"]
            updates, history = [], []
            for row in rows:
                if row.get("missed_run_id") == run["id"]:
                    continue
                listed = row.get("listing_date") or (row["last_seen_at"].date() if row.get("last_seen_at") else None)
                if horizon is not None and listed is not None and listed < horizon:
                    updates.append((row["id"], {"status": "expired", "expired_at": now, "missed_run_id": run["id"],
                                                "source_monitor_id": row.get("source_monitor_id") or monitor["id"]}))
                    history.append({"job_posting_id": row["id"], "monitor_id": monitor["id"], "run_id": run["id"],
                                    "change": "expired", "changed_fields": ["status"],
                                    "before": {"status": STATUS_LABELS.get(row["status"])},
                                    "after": {"status": "EXPIRED", "listing_date": listed.isoformat(),
                                              "reason": f"older than the source's {window}-day listing window"},
                                    "detected_at": now})
                    stats["expired"] += 1
                    continue
                missed = (row.get("missed_full_sweeps") or 0) + 1
                patch: Dict[str, Any] = {"missed_full_sweeps": missed, "missed_run_id": run["id"]}
                stats["missed"] += 1
                if missed >= threshold:
                    patch.update({"status": "closed", "closed_at": now, "closed_run_id": run["id"],
                                  "closure_reason": "missed_full_sweeps",
                                  "source_monitor_id": row.get("source_monitor_id") or monitor["id"]})
                    stats["closed"] += 1
                    history.append({"job_posting_id": row["id"], "monitor_id": monitor["id"], "run_id": run["id"],
                                    "change": "closed", "changed_fields": ["status"],
                                    "before": {"status": STATUS_LABELS.get(row["status"])},
                                    "after": {"status": "CLOSED", "missed_full_sweeps": missed,
                                              "reason": "missed_full_sweeps"},
                                    "detected_at": now})
                updates.append((row["id"], patch))
            if updates:
                self.store.update_many(ctx, "job_postings", updates)
            if history:
                self.store.insert_many(ctx, "job_posting_changes", history)
            if progress is not None:
                progress(stats)
        return stats

    def check_gone(self, ctx: Ctx, monitor: Mapping[str, Any], run: Mapping[str, Any], *, http: Any = None,
                   budget: Optional[int] = None, stop: Optional[Any] = None) -> Dict[str, Any]:
        """Direct evidence for the jobs this completed sweep did not see: fetch each job's own URL
        (robots-aware safe fetcher, polite per-host delay). A status the site profile lists as
        gone (e.g. 410) CLOSES the job now (``closure_reason = source_gone``); a live page is
        recorded and keeps the job; a refusal (403/429/robots) stops the checks for this run."""
        from cloud.intel.job_monitor.profiles import PROFILES as _profiles

        profile = _profiles.get(monitor.get("profile") or "")
        gone_statuses = set(getattr(profile, "gone_statuses", (404, 410)))
        limit = int(monitor.get("gone_checks_per_run") or 0) if budget is None else int(budget)
        stats = {"checked": 0, "closed": 0, "live": 0, "errors": 0, "stopped": None}
        if limit <= 0:
            return stats
        http = http or self._http()
        now = utcnow()
        filters = {**self.close_scope(ctx, monitor), "missed_run_id": run["id"],
                   "status": [*SWEEP_STATES, "expired"],
                   "any_of": [{"gone_checked_at__isnull": True}, {"gone_checked_at__lt": now - GONE_RECHECK}]}
        rows = self.store.rows(ctx, "job_postings", filters, order="-missed_full_sweeps", limit=limit)
        for row in rows:
            if stop is not None and stop():
                stats["stopped"] = "cancelled or paused"
                break
            try:
                result = http.fetch(row["job_url"])
            except Exception as error:  # noqa: BLE001 - one bad URL never stops the checks
                log.warning("gone check of %s failed: %s", row["job_url"], error)
                stats["errors"] += 1
                continue
            status = int(getattr(result, "status", 0) or 0)
            if getattr(result, "blocked", False) or status in (401, 403, 429, 503):
                stats["stopped"] = f"the site refused a check (HTTP {status or getattr(result, 'error', '')})"
                break
            stats["checked"] += 1
            checked = utcnow()
            patch: Dict[str, Any] = {"gone_checked_at": checked, "gone_status": status or None}
            if status in gone_statuses:
                patch.update({"status": "closed", "closed_at": checked, "closed_run_id": run["id"],
                              "closure_reason": "source_gone",
                              "source_monitor_id": row.get("source_monitor_id") or monitor["id"]})
                self.store.update(ctx, "job_postings", row["id"], patch)
                self.store.insert(ctx, "job_posting_changes", {
                    "job_posting_id": row["id"], "monitor_id": monitor["id"], "run_id": run["id"],
                    "change": "closed", "changed_fields": ["status"],
                    "before": {"status": STATUS_LABELS.get(row["status"])},
                    "after": {"status": "CLOSED", "reason": "source_gone", "http_status": status},
                    "detected_at": checked})
                stats["closed"] += 1
            else:
                if status == 200:
                    stats["live"] += 1
                elif not status:
                    stats["errors"] += 1
                self.store.update(ctx, "job_postings", row["id"], patch)
        return stats

    def active_since(self, row: Mapping[str, Any]) -> Optional[datetime]:
        """When the role started being active, by the earliest evidence: a reopen restarts the
        clock; otherwise the earlier of first seen and the source's listing date."""
        if row.get("reopened_at"):
            return row["reopened_at"]
        candidates = [row.get("first_seen_at")]
        if row.get("listing_date"):
            candidates.append(_day_start(row["listing_date"]))
        candidates = [c for c in candidates if c is not None]
        return min(candidates) if candidates else None

    def evaluate_lifecycle(self, ctx: Ctx, monitor_id: str, *, now: Optional[datetime] = None,
                           notify: bool = True) -> Dict[str, Any]:
        """The daily evaluation of a monitor's jobs: ACTIVE jobs active longer than the monitor's
        ``stale_after_days`` become STALE (history row, stale_at); the day's new / changed /
        reopened counts and the closure candidates (jobs that missed a completed full sweep)
        are summarised on the monitor. Nothing is closed here — closing needs a completed full
        sweep or direct gone evidence."""
        now = now or utcnow()
        monitor = self.store.get(ctx, "job_source_monitors", monitor_id)
        days = int(monitor.get("stale_after_days") or 30)
        cutoff = now - timedelta(days=days)
        scope = self.close_scope(ctx, monitor)
        stale = 0
        last_id = ""
        while True:
            filters = {"all_of": [scope, {"status": "open"}, {"any_of": [
                {"first_seen_at__lt": cutoff}, {"listing_date__lt": cutoff.date()}]}]}
            if last_id:
                filters["id__gt"] = last_id
            rows = self.store.rows(ctx, "job_postings", filters, order="id", limit=BATCH)
            if not rows:
                break
            last_id = rows[-1]["id"]
            updates, history = [], []
            for row in rows:
                since = self.active_since(row)
                if since is None or since >= cutoff:
                    continue
                updates.append((row["id"], {"status": "stale", "stale_at": now}))
                history.append({"job_posting_id": row["id"], "monitor_id": monitor["id"], "run_id": None,
                                "change": "stale", "changed_fields": ["status"], "before": {"status": "ACTIVE"},
                                "after": {"status": "STALE", "active_since": since.isoformat(),
                                          "stale_after_days": days},
                                "detected_at": now})
            if updates:
                self.store.update_many(ctx, "job_postings", updates)
                self.store.insert_many(ctx, "job_posting_changes", history)
                stale += len(updates)
        day = now - timedelta(days=1)
        recent = {c: self.store.count(ctx, "job_posting_changes", {"monitor_id": monitor["id"], "change": c,
                                                                 "detected_at__gte": day})
                  for c in ("new", "changed", "reopened", "closed", "expired")}
        by_status = {STATUS_LABELS.get(k, k): v for k, v in
                     self.store.group_count(ctx, "job_postings", "status", scope).items()}
        candidates = self.store.count(ctx, "job_postings", {"all_of": [scope, {"status": list(SWEEP_STATES)},
                                                                       {"missed_full_sweeps__gte": 1}]})
        result = {"evaluated_at": now.isoformat(), "stale_after_days": days, "became_stale": stale,
                  "last_24h": recent, "by_status": by_status, "closure_candidates": candidates}
        try:
            result["signals"] = self.platform.service("signals").detect_job_signals(ctx, now=now)
        except Exception as error:  # noqa: BLE001 - signals are derived data; the lifecycle is stored
            log.exception("job-derived signal detection failed")
            result["signals"] = {"error": f"{type(error).__name__}: {error}"[:300]}
        self.store.update(ctx, "job_source_monitors", monitor["id"],
                          {"last_lifecycle_at": now, "last_lifecycle_result": _jsonable(result)})
        if notify and stale:
            self.platform.service("notifications").notify(
                ctx, title=f"{stale:,} job{'s' if stale != 1 else ''} became stale", kind="job_monitor",
                severity="info", body=f"{monitor['name']}: still active more than {days} days.",
                link=f"/jobs?monitor={monitor['id']}&change=stale&since={now.date().isoformat()}",
                entity_type="job_source_monitors", entity_id=monitor["id"])
        return result

    # ------------------------------------------------------------------------------------
    # results, notifications, SANA chat
    # ------------------------------------------------------------------------------------

    def run_counts(self, ctx: Ctx, run_id: str) -> Dict[str, int]:
        """Exact counts from the database (correct even after a crash/resume re-read a page)."""
        return {
            "new_count": self.store.count(ctx, "job_postings", {"first_seen_run_id": run_id}),
            "changed_count": self.store.count(ctx, "job_posting_changes", {"run_id": run_id, "change": "changed"}),
            "reopened_count": self.store.count(ctx, "job_posting_changes", {"run_id": run_id, "change": "reopened"}),
            "closed_count": self.store.count(ctx, "job_postings", {"closed_run_id": run_id}),
            "expired_count": self.store.count(ctx, "job_posting_changes", {"run_id": run_id, "change": "expired"}),
        }

    def jobs_link(self, monitor_id: str, run_id: str, change: str) -> str:
        return f"/jobs?monitor={monitor_id}&run={run_id}&change={change}"

    def announce(self, ctx: Ctx, monitor: Mapping[str, Any], run: Mapping[str, Any]) -> Dict[str, Any]:
        """Notifications (each deep-linked to the filtered jobs) and one SANA chat update."""
        notes = self.platform.service("notifications")
        source = monitor["source_name"]
        sent = []
        status = run["status"]
        if status == "failed":
            sent.append(notes.notify(ctx, title=f"Monitor failed: {monitor['name']}", kind="job_monitor",
                                     body=(run.get("error") or run.get("stop_reason") or "")[:500], severity="error",
                                     link=f"/monitors/{monitor['id']}", entity_type="job_monitor_runs",
                                     entity_id=run["id"]))
            return {"notifications": [n["id"] for n in sent]}
        new, changed, closed = run["new_count"], run["changed_count"], run["closed_count"]
        if new:
            sent.append(notes.notify(ctx, title=f"{new:,} new job{'s' if new != 1 else ''} found from {source}",
                                     kind="job_monitor", severity="success",
                                     body=f"{monitor['name']}: {run['found']:,} jobs read on {run['pages']:,} pages.",
                                     link=self.jobs_link(monitor["id"], run["id"], "new"),
                                     entity_type="job_monitor_runs", entity_id=run["id"]))
        if changed:
            sent.append(notes.notify(ctx, title=f"{changed:,} job{'s' if changed != 1 else ''} changed",
                                     kind="job_monitor", body=f"{monitor['name']} ({source})",
                                     link=self.jobs_link(monitor["id"], run["id"], "changed"),
                                     entity_type="job_monitor_runs", entity_id=run["id"]))
        if closed:
            gone = run.get("gone_closed_count") or 0
            reasons = []
            if closed - gone:
                reasons.append(f"{closed - gone:,} missing from {monitor['close_after_missed']} completed full sweeps")
            if gone:
                reasons.append(f"{gone:,} confirmed removed at the source (job page gone)")
            sent.append(notes.notify(ctx, title=f"{closed:,} job{'s were' if closed != 1 else ' was'} closed",
                                     kind="job_monitor", body=f"{monitor['name']} ({source}): " + "; ".join(reasons),
                                     link=self.jobs_link(monitor["id"], run["id"], "closed"),
                                     entity_type="job_monitor_runs", entity_id=run["id"]))
        expired = run.get("expired_count") or 0
        if expired:
            sent.append(notes.notify(ctx, title=f"{expired:,} job{'s' if expired != 1 else ''} expired",
                                     kind="job_monitor", body=f"{monitor['name']} ({source}): older than the source's "
                                     f"{monitor.get('visible_window_days')}-day listing window (kept, not closed).",
                                     link=self.jobs_link(monitor["id"], run["id"], "expired"),
                                     entity_type="job_monitor_runs", entity_id=run["id"]))
        reopened = run.get("reopened_count") or 0
        if reopened:
            sent.append(notes.notify(ctx, title=f"{reopened:,} closed job{'s' if reopened != 1 else ''} reopened",
                                     kind="job_monitor", body=f"{monitor['name']} ({source})",
                                     link=self.jobs_link(monitor["id"], run["id"], "reopened"),
                                     entity_type="job_monitor_runs", entity_id=run["id"]))
        if status != "completed":
            sent.append(notes.notify(ctx, title=f"Monitor finished as {status}: {monitor['name']}", kind="job_monitor",
                                     severity="warning",
                                     body=f"{new:,} new, {changed:,} changed, {closed:,} closed"
                                          + (f" — {run['stop_reason']}" if run.get("stop_reason") else ""),
                                     link=f"/monitors/{monitor['id']}", entity_type="job_monitor_runs",
                                     entity_id=run["id"]))
        if not (new or changed or closed or reopened or expired):
            return {"notifications": [n["id"] for n in sent], "chat_message": None}   # nothing worth a message
        message = self.post_chat(ctx, monitor, run)
        return {"notifications": [n["id"] for n in sent], "chat_message": message["id"] if message else None}

    CHAT_TITLE = "SANA job monitor updates"

    def post_chat(self, ctx: Ctx, monitor: Mapping[str, Any], run: Mapping[str, Any], *, limit: int = 10
                  ) -> Optional[Dict[str, Any]]:
        """One assistant message in the workspace's monitor-updates conversation, with the
        newest jobs of the run (real source URLs only — a job without one is not listed)."""
        system = ctx if ctx.system else ctx.as_system()
        session = self.store.first(system, "agent_sessions", {"title": self.CHAT_TITLE, "mode": "monitoring"})
        if session is None:
            session = self.store.insert(system, "agent_sessions", {"title": self.CHAT_TITLE, "mode": "monitoring"})
        new = self.store.list(system, "job_postings", {"first_seen_run_id": run["id"]}, order="-first_seen_at",
                              limit=limit).rows
        jobs = [{"id": j["id"], "title": j["title"], "company": j.get("company_name"), "location": j.get("location"),
                 "remote": j.get("remote"), "keywords": [j[k] for k in KEYWORD_COLUMNS if j.get(k)],
                 "job_url": j["job_url"]} for j in new if j.get("job_url")]
        lines = [f"{monitor['name']} was checked."]
        if run["status"] != "completed":
            lines.append(f"The run finished as {run['status']}" + (f": {run['stop_reason']}" if run.get("stop_reason")
                                                                  else "") + ". No job was closed.")
        lines.append("")
        lines.append(f"{run['new_count']:,} new job{'s' if run['new_count'] != 1 else ''} found.")
        lines.append(f"{run['changed_count']:,} job{'s' if run['changed_count'] != 1 else ''} changed.")
        if run["mode"] == "full":
            lines.append(f"{run['closed_count']:,} job{'s' if run['closed_count'] != 1 else ''} appear closed.")
        if jobs:
            lines.append("")
            lines.append("Here are the newest jobs.")
        data = {"kind": "job_monitor_update", "monitor_id": monitor["id"], "run_id": run["id"],
                "source": monitor["source_name"], "status": run["status"], "mode": run["mode"],
                "counts": {k: run[k] for k in ("found", "new_count", "changed_count", "closed_count", "reopened_count")},
                "jobs": jobs, "links": {c: self.jobs_link(monitor["id"], run["id"], c)
                                        for c in ("new", "changed", "closed")}}
        try:
            return self.store.insert(system, "agent_messages", {"session_id": session["id"], "role": "assistant",
                                                                "content": "\n".join(lines), "data": data})
        except Exception:  # noqa: BLE001 - chat is a convenience; the notifications are already stored
            log.exception("could not post the job monitor chat update for run %s", run["id"])
            return None

    # ------------------------------------------------------------------------------------
    # job queries
    # ------------------------------------------------------------------------------------

    def _condition(self, cond: Mapping[str, Any]) -> Dict[str, Any]:
        """``{"field": "title", "op": "contains", "value": "engineer"}`` -> a store filter dict."""
        if "all" in cond or "any" in cond:
            key, items = ("all_of", cond["all"]) if "all" in cond else ("any_of", cond["any"])
            if not isinstance(items, list) or not items:
                raise ValidationError("a condition group needs at least one condition")
            return {key: [self._condition(c) for c in items]}
        field_name = str(cond.get("field") or "")
        column = QUERY_FIELDS.get(field_name)
        if column is None:
            raise ValidationError(f"cannot filter jobs by {field_name!r}")
        op = str(cond.get("op") or "eq")
        if op not in QUERY_OPS:
            raise ValidationError(f"unknown condition operator {op!r}")
        value = cond.get("value")
        if column == "keyword":
            # "has keyword X" matches any of Keyword 1-5; "not X" / "empty" must hold for all five.
            parts = [self._simple(k, op, value) for k in KEYWORD_COLUMNS]
            return {"all_of": parts} if op in ("ne", "empty") else {"any_of": parts}
        if column == "status":
            value = [STATUS_VALUES.get(str(v).upper(), v) for v in value] if isinstance(value, list) \
                else STATUS_VALUES.get(str(value).upper(), value)
        return self._simple(column, op, value)

    @staticmethod
    def _simple(column: str, op: str, value: Any) -> Dict[str, Any]:
        if column in ("first_seen_at", "last_seen_at", "last_changed_at") and op in ("gte", "gt", "lte", "lt"):
            value = _parse_when(value, end=op in ("lte",))
        if op == "contains":
            return {f"{column}__ilike": str(value or "")}
        if op == "empty":
            return {f"{column}__isnull": True}
        if op == "not_empty":
            return {f"{column}__isnull": False}
        if op == "in":
            return {column: list(value) if isinstance(value, (list, tuple)) else [v for v in str(value).split(",")]}
        if op == "eq":
            return {column: value}
        return {f"{column}__{op}": value}

    def query_filters(self, ctx: Ctx, params: Mapping[str, Any]) -> Dict[str, Any]:
        """Simple parameters (all ANDed) plus an optional ``conditions`` tree (all/any)."""
        groups: List[Dict[str, Any]] = []
        simple = {"source": "source", "company": "company_name", "title": "title", "location": "location",
                  "country": "country", "experience": "experience_level", "salary": "salary_budget",
                  "source_board": "source_board", "search_term": "search_term"}
        if params.get("relevance") not in (None, ""):
            groups.append({"relevance_class": [c.strip().upper() for c in str(params["relevance"]).split(",")]})
        if params.get("relevance_min") not in (None, ""):
            groups.append({"relevance_score__gte": float(params["relevance_min"])})
        if params.get("category") not in (None, ""):
            groups.append({"matched_categories__contains": str(params["category"])})
        for param, column in simple.items():
            if params.get(param) not in (None, ""):
                groups.append({f"{column}__ilike": str(params[param])})
        if params.get("remote") not in (None, ""):
            remote = str(params["remote"])
            groups.append({"remote__isnull": True} if remote.lower() in ("blank", "unknown", "none")
                          else {"remote": remote})
        if params.get("keyword") not in (None, ""):
            groups.append(self._condition({"field": "keyword", "op": "contains", "value": params["keyword"]}))
        if params.get("status") not in (None, ""):
            values = [STATUS_VALUES.get(s.strip().upper(), s.strip()) for s in str(params["status"]).split(",") if s]
            groups.append({"status": values})
        if params.get("company_id"):
            groups.append({"company_id": params["company_id"]})
        if params.get("import"):
            imported = self.store.get(ctx, "job_imports", str(params["import"]))
            groups.append({"source_kind": "import", "source_name": f"Import: {imported['filename']}"[:200]})
        for param, column in (("scraped", "scraped_date"), ("first_seen", "first_seen_at"),
                              ("last_changed", "last_changed_at")):
            low, high = params.get(f"{param}_from"), params.get(f"{param}_to")
            if column == "scraped_date":
                if low:
                    groups.append({f"{column}__gte": str(low)})
                if high:
                    groups.append({f"{column}__lte": str(high)})
            else:
                if low:
                    groups.append({f"{column}__gte": _parse_when(low)})
                if high:
                    groups.append({f"{column}__lte": _parse_when(high, end=True)})
        monitor_id, run_id = params.get("monitor"), params.get("run")
        change = params.get("change")
        if change in ("stale", "expired"):
            # A state, not a run event: every STALE / EXPIRED job (optionally since a date, of a
            # monitor, or — EXPIRED only — expired by one sweep).
            column = {"stale": "stale_at", "expired": "expired_at"}[change]
            groups.append({"status": change})
            if run_id and change == "expired":
                groups.append({"missed_run_id": run_id})
            elif params.get("since"):
                groups.append({f"{column}__gte": _parse_when(params["since"])})
            if monitor_id:
                groups.append({"source_monitor_id": str(monitor_id)})
            change = None
            monitor_id = None
        if monitor_id and not run_id and change != "unchanged" and (change or params.get("since_last_run")):
            monitor = self.store.get(ctx, "job_source_monitors", str(monitor_id))
            run_id = monitor.get("last_run_id")
            change = change or "new"
            if run_id is None:
                groups.append({"id": "__none__"})
        if change:
            if change not in ("new", "changed", "closed", "reopened", "unchanged"):
                raise ValidationError("change must be new, changed, unchanged, stale, expired, closed or reopened")
            if change == "unchanged" and not run_id and monitor_id:
                run_id = (self.store.get(ctx, "job_source_monitors", str(monitor_id)).get("last_run_id")
                          or "__none__")
            if change == "unchanged" and not run_id:
                raise ValidationError("unchanged needs a monitor or a run")
            if run_id and change == "unchanged":
                groups.append({"last_seen_run_id": run_id, "first_seen_run_id__ne": run_id,
                               "last_changed_run_id__ne": run_id})
            elif run_id:
                if change == "reopened":
                    ids = [c["job_posting_id"] for c in self.store.all(
                        ctx, "job_posting_changes", {"run_id": run_id, "change": "reopened"}, cap=10000)]
                    groups.append({"id": ids or ["__none__"]})
                else:
                    groups.append({{"new": "first_seen_run_id", "changed": "last_changed_run_id",
                                    "closed": "closed_run_id"}[change]: run_id})
            elif change == "closed" and not params.get("since"):
                groups.append({"status": "closed"})          # every CLOSED job
                if monitor_id:
                    groups.append({"source_monitor_id": str(monitor_id)})
            elif change == "reopened" and not params.get("since"):
                groups.append({"reopened_at__isnull": False})
                if monitor_id:
                    groups.append({"source_monitor_id": str(monitor_id)})
            else:
                since = _parse_when(params.get("since")) or (utcnow() - timedelta(days=7))
                groups.append({{"new": "first_seen_at__gte", "changed": "last_changed_at__gte",
                                "closed": "closed_at__gte", "reopened": "reopened_at__gte"}[change]: since})
                if change == "closed":
                    groups.append({"status": "closed"})
        elif monitor_id:
            groups.append({"source_monitor_id": str(monitor_id)})
        conditions = params.get("conditions")
        if conditions:
            if not isinstance(conditions, Mapping):
                raise ValidationError("conditions must be an object with all/any")
            groups.append(self._condition(conditions))
        filters: Dict[str, Any] = {"all_of": groups} if groups else {}
        if params.get("q"):
            filters["q"] = str(params["q"])[:200]
        return filters

    def search_jobs(self, ctx: Ctx, params: Mapping[str, Any]) -> Dict[str, Any]:
        filters = self.query_filters(ctx, params)
        order = str(params.get("order") or "-first_seen_at")
        if order.lstrip("-") not in SORTABLE:
            raise ValidationError(f"cannot sort jobs by {order!r}")
        limit = max(1, min(int(params.get("limit") or 50), 500))
        offset = max(0, int(params.get("offset") or 0))
        page = self.store.list(ctx, "job_postings", filters, order=order, limit=limit, offset=offset)
        last_runs = self._last_runs(ctx, {r.get("source_monitor_id") for r in page.rows})
        return {"rows": [self.present(r, last_runs) for r in page.rows], "total": page.total, "limit": limit,
                "offset": offset}

    def _last_runs(self, ctx: Ctx, monitor_ids: Iterable[Optional[str]]) -> Dict[str, Optional[str]]:
        ids = [m for m in monitor_ids if m]
        if not ids:
            return {}
        return {m["id"]: m.get("last_run_id") for m in self.store.all(ctx, "job_source_monitors", {"id": ids},
                                                                       cap=len(ids) + 1)}

    @staticmethod
    def present(row: Mapping[str, Any], last_runs: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, Any]:
        """A job for the API: the row, its 14 labelled fields, display status and a change badge
        relative to its monitor's last run."""
        last_run = (last_runs or {}).get(row.get("source_monitor_id") or "")
        badge = None
        if row["status"] == "closed":
            badge = "Closed"
        elif row["status"] == "expired":
            badge = "Expired"
        elif last_run and row.get("first_seen_run_id") == last_run:
            badge = "New"
        elif last_run and row.get("last_changed_run_id") == last_run:
            badge = "Changed"
        elif row["status"] == "stale":
            badge = "Stale"
        return {**row, "normalized_job_url": row.get("url_key"), "status_label": STATUS_LABELS.get(row["status"],
                row["status"]), "change_badge": badge, "keywords": [row[k] for k in KEYWORD_COLUMNS if row.get(k)],
                "fields": field_values(row)}

    def job_detail(self, ctx: Ctx, job_id: str) -> Dict[str, Any]:
        row = self.store.get(ctx, "job_postings", job_id)
        history = self.store.all(ctx, "job_posting_changes", {"job_posting_id": job_id}, order="-detected_at", cap=200)
        monitor = self.store.find(ctx, "job_source_monitors", row["source_monitor_id"]) if row.get(
            "source_monitor_id") else None
        company = self.store.find(ctx, "companies", row["company_id"]) if row.get("company_id") else None
        last_runs = {monitor["id"]: monitor.get("last_run_id")} if monitor else {}
        return {"job": self.present(row, last_runs), "history": history,
                "monitor": {k: monitor[k] for k in ("id", "name", "source_name", "last_run_at")} if monitor else None,
                "company": {"id": company["id"], "name": company["name"]} if company else None}

    def company_jobs(self, ctx: Ctx, company_id: str, *, days: int = 30, weeks: int = 12) -> Dict[str, Any]:
        """Open, new and recently closed jobs plus weekly hiring activity for a company page."""
        self.store.get(ctx, "companies", company_id)
        rows = self.store.all(ctx, "job_postings", {"company_id": company_id}, order="-first_seen_at", cap=5000)
        now = utcnow()
        since = now - timedelta(days=days)
        open_jobs = [r for r in rows if r["status"] == "open"]
        new_jobs = [r for r in rows if r["first_seen_at"] and r["first_seen_at"] >= since]
        closed = [r for r in rows if r["status"] == "closed" and r.get("closed_at") and r["closed_at"] >= since]
        start = (now - timedelta(weeks=weeks)).date()
        start -= timedelta(days=start.weekday())
        activity = []
        for i in range(weeks + 1):
            week = start + timedelta(weeks=i)
            nxt = week + timedelta(weeks=1)
            activity.append({
                "week": week.isoformat(),
                "new": sum(1 for r in rows if r["first_seen_at"] and week <= r["first_seen_at"].date() < nxt),
                "closed": sum(1 for r in rows if r.get("closed_at") and week <= r["closed_at"].date() < nxt),
            })
        pick = lambda items: [self.present(r) for r in items[:200]]  # noqa: E731
        return {"counts": {"open": len(open_jobs), "new": len(new_jobs), "recently_closed": len(closed),
                           "total": len(rows)},
                "open_jobs": pick(open_jobs), "new_jobs": pick(new_jobs), "recently_closed": pick(closed),
                "activity": activity, "history": pick(rows)}

    # ------------------------------------------------------------------------------------
    # company review queue
    # ------------------------------------------------------------------------------------

    def _jobs_named(self, ctx: Ctx, review: Mapping[str, Any]) -> List[Dict[str, Any]]:
        rows = self.store.all(ctx, "job_postings", {"company_match": "review",
                                                    "company_name__ilike": review["company_name"]}, cap=100_000)
        return [r for r in rows if normalize_name(r.get("company_name")) == review["normalized_name"]]

    def resolve_review(self, ctx: Ctx, review_id: str, *, action: str, company_id: Optional[str] = None
                       ) -> Dict[str, Any]:
        """``link`` every job with this company name to ``company_id`` (an existing CRM
        company chosen by a person), or ``ignore`` the name. Never creates a company."""
        ctx.require_write()
        review = self.store.get(ctx, "job_company_reviews", review_id)
        if action == "link":
            company = self.store.get(ctx, "companies", str(company_id or ""))
            jobs = self._jobs_named(ctx, review)
            for chunk in _chunks(jobs):
                self.store.update_many(ctx, "job_postings", [(j["id"], {"company_id": company["id"],
                                                                        "company_match": "matched"}) for j in chunk])
            row = self.store.update(ctx, "job_company_reviews", review_id, {"status": "linked",
                                                                           "company_id": company["id"]})
            audit(self.store, ctx, "job_company.linked", entity_type="job_company_reviews", entity_id=review_id,
                  summary=f"{review['company_name']} -> {company['name']} ({len(jobs)} jobs)")
            return {**row, "jobs_linked": len(jobs)}
        if action == "ignore":
            row = self.store.update(ctx, "job_company_reviews", review_id, {"status": "ignored"})
            audit(self.store, ctx, "job_company.ignored", entity_type="job_company_reviews", entity_id=review_id,
                  summary=review["company_name"])
            return row
        raise ValidationError("action must be link or ignore")

    # ------------------------------------------------------------------------------------
    # monitor views
    # ------------------------------------------------------------------------------------

    def monitor_detail(self, ctx: Ctx, monitor_id: str) -> Dict[str, Any]:
        monitor = self.store.get(ctx, "job_source_monitors", monitor_id)
        runs = self.store.list(ctx, "job_monitor_runs", {"monitor_id": monitor_id}, order="-created_at", limit=20).rows
        last = runs[0] if runs else None
        by_status = self.store.group_count(ctx, "job_postings", "status", {"source_monitor_id": monitor_id})
        return {"monitor": {**monitor, "auto_full_sweep": monitor.get("next_full_sweep_at") is not None},
                "runs": runs, "active_run": self.active_run(ctx, monitor_id),
                "jobs": {"total": sum(by_status.values()), "active": by_status.get("open", 0),
                         "stale": by_status.get("stale", 0), "expired": by_status.get("expired", 0),
                         "closed": by_status.get("closed", 0), "unknown": by_status.get("unknown", 0)},
                "new_since_last_run": (last or {}).get("new_count", 0) if last and last.get("status") in
                FINISHED_RUN_STATES else monitor.get("last_result", {}).get("new_count", 0),
                "links": {c: f"/jobs?monitor={monitor_id}&change={c}"
                          for c in ("new", "changed", "stale", "expired", "closed", "reopened")}}


def _jsonable(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in values.items()}
