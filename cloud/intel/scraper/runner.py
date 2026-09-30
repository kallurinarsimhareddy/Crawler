"""The ``scraper`` background task: one persistent run, many input URLs, then the output files.

    UI ─► API ─► scrape_runs row ─► platform_tasks (queue) ─► worker ─► this module ─► scrape_results,
                                                                                       scrape_pages, files

    planning     options, limits, AI budget; skip inputs already saved (resume / crash recovery)
    for up to ``concurrency`` input URLs at once (each a :class:`~cloud.intel.scraper.crawler.Crawl`):
        fetching · extracting · paginating · enriching   (cancel / pause checked continuously)
        saving       one scrape_results row per input + one scrape_pages row per fetched page
    validating / normalizing   normalise ─► validate ─► filters ─► dedupe
    saving       CSV / XLSX / JSON / NDJSON
    completed

Crawl threads only fetch and extract; every database write happens on this
(the task's) thread. The run's ``status`` is the current stage; the detailed
counters live in ``stats.progress``.

**Restart recovery and idempotency.** A result row is keyed by the input's
index and a page row by (run, URL): a task re-delivered after a crash, resumed
after a pause, or retried skips every input that already has a result, never
duplicates a record, and never refetches a page it saved. ``retry`` re-does only
inputs whose outcome may be transient (timeout, network failure, rate limit).
Pausing lets in-flight inputs stop at their next page; they are redone on resume.
Cancelling keeps and exports what was scraped so far.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.scraper.crawler import Crawl, VisitedSet, url_key
from cloud.intel.scraper.dedupe import dedupe_records
from cloud.intel.scraper.exports import views, write_outputs
from cloud.intel.scraper.fetcher import PageFetcher, browser_renderer
from cloud.intel.scraper.limits import DomainLimiter, RunBudget
from cloud.intel.scraper.models import (BLOCKING, CrawlOptions, FieldValue, Outcome, PageExtraction, TRANSIENT,
                                        store_status)
from cloud.intel.scraper.normalizer import normalize_record
from cloud.intel.scraper.validator import apply_filters, validate_fields

__all__ = ["ACTIVE_STATES", "AIBudget", "execute_run", "flatten", "run_scrape_inline", "run_scrape_task",
           "scrape_one"]

log = logging.getLogger(__name__)

DEFAULT_MAX_AI_CALLS = 25
ACTIVE_STATES = frozenset({"queued", "planning", "fetching", "extracting", "paginating", "enriching", "validating",
                           "normalizing", "saving", "running"})
WORKING_STATES = ACTIVE_STATES - {"queued"}


class AIBudget:
    """The run's AI: at most ``max_calls`` calls (``concurrency`` at a time), switched off
    for the rest of the run as soon as the provider is unavailable (free quota used up,
    not allowed…).

    A call that fails (e.g. the provider answers 503) is noted on its page and also
    counted here, so the run's summary says AI did not help; the call is not retried."""

    def __init__(self, provider: Any, max_calls: int, note: Optional[str] = None, *, concurrency: int = 1) -> None:
        self.provider = provider
        self.max_calls = max_calls
        self.calls = 0
        self.note = note
        self.failures = 0
        self.last_failure: Optional[str] = None
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max(1, concurrency))

    @property
    def summary(self) -> Optional[str]:
        """What the run reports about AI: why it stopped, and any failed calls."""
        parts = [self.note] if self.note else []
        if self.failures:
            parts.append(f"{self.failures} AI call{'s' if self.failures > 1 else ''} failed "
                         f"(last: {self.last_failure}); the fields asked for were left empty")
        return "; ".join(parts) or None

    @property
    def available(self) -> bool:
        with self._lock:
            if self.provider is not None and self.calls >= self.max_calls and not self.note:
                self.note = f"AI call limit for this run ({self.max_calls}) reached; the rest used rules only"
            return self.provider is not None and self.calls < self.max_calls

    def call(self, fn: Callable[[Any], bool], problems: List[str]) -> bool:
        """Run ``fn(provider)``; returns whether AI was actually used."""
        from cloud.intel.ai.base import AIError, AIUnavailable

        with self._slots:   # checked inside the slot: a call waiting here sees a failure that just happened
            with self._lock:
                provider = self.provider
                if provider is None:
                    return False
                if self.calls >= self.max_calls:
                    if not self.note:
                        self.note = f"AI call limit for this run ({self.max_calls}) reached; the rest used rules only"
                    return False
                self.calls += 1
            try:
                return bool(fn(provider))
            except AIUnavailable as error:
                with self._lock:
                    self.provider = None
                    self.note = f"{error} — the rest of the run used rules only"
                problems.append(f"AI not used: {error}")
            except AIError as error:
                with self._lock:
                    self.failures += 1
                    self.last_failure = str(error)[:200]
                problems.append(f"AI extraction skipped: {str(error)[:200]}")
        return False


def scrape_one(url: str, schema: Mapping[str, Any], fetcher: PageFetcher, ai: Optional[AIBudget] = None,
               on_stage: Callable[[str], Any] = lambda stage: None, *, options: Optional[CrawlOptions] = None,
               budget: Optional[RunBudget] = None, visited: Optional[VisitedSet] = None) -> PageExtraction:
    """Crawl one input URL (see :mod:`cloud.intel.scraper.crawler`)."""
    crawl = Crawl(schema, fetcher, options=options or CrawlOptions(), ai=ai, budget=budget, visited=visited,
                  on_stage=on_stage)
    result = crawl.run(url)
    result.stats["interrupted"] = 1 if crawl.interrupted else 0
    return result


def flatten(record: Mapping[str, FieldValue], schema: Mapping[str, Any], *, source_url: str,
            extracted_at: datetime) -> Dict[str, Any]:
    """Normalise, validate and flatten one record into an output row, keeping full provenance."""
    fields = list(schema["fields"])
    normal = normalize_record(record, fields, now=extracted_at)
    methods = {name: fv.method for name, fv in normal.items()}
    conflicts = [name for name, fv in normal.items() if fv.alternatives]
    clean, statuses, errors = validate_fields({name: fv.value for name, fv in normal.items()}, fields,
                                              methods=methods, conflicts=conflicts)
    kept = {name: fv for name, fv in normal.items() if clean.get(name) is not None}
    confidences = [fv.confidence for fv in kept.values()]
    sources = [fv.source_url for fv in kept.values() if fv.source_url]
    evidence = {}
    for name, fv in kept.items():
        item = {k: v for k, v in fv.as_dict().items() if k != "value"}
        item["status"] = statuses.get(name, "valid")
        evidence[name] = item
    rejected = {name: {"value": normal[name].value, "error": errors[name], "method": normal[name].method,
                       "evidence": normal[name].evidence, "source_url": normal[name].source_url}
                for name in errors if name in normal}
    row: Dict[str, Any] = dict(clean)
    row.update({
        "source_url": next((fv.source_url for name, fv in kept.items() if fv.source_url and name.startswith("job_")),
                           None) or (sources[0] if sources else source_url),
        "extraction_method": "+".join(sorted({fv.method for fv in kept.values()})) or None,
        "confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "extracted_at": extracted_at.isoformat(timespec="seconds"),
        "_evidence": evidence,
        "_field_status": statuses,
        "_conflicts": {name: {"chosen": normal[name].value,
                              "alternatives": [a.as_dict() for a in normal[name].alternatives]} for name in conflicts},
        "_rejected": rejected,
        "_problems": list(errors.values()),
    })
    return row


# --- the task ------------------------------------------------------------------------------------


def _http(platform: Any) -> Any:
    from cloud.intel.core.http import SafeFetcher

    factory = platform.config.extra.get("fetcher_factory")
    return factory() if factory else SafeFetcher()


def _renderer(platform: Any, options: CrawlOptions) -> Any:
    """The browser only when the run asked for it *and* the platform allows it."""
    if not options.browser:
        return None
    factory = platform.config.extra.get("renderer_factory")
    if factory is not None:
        return factory()
    if not platform.config.extra.get("scraper_browser_enabled"):
        return None
    return browser_renderer(True)


def _ai_budget(platform: Any, ctx: Ctx, run: Mapping[str, Any], options: CrawlOptions) -> AIBudget:
    if not options.use_ai:
        return AIBudget(None, 0, "AI turned off for this run")
    try:
        provider = platform.service("ai").for_ctx(ctx, "extraction", run_id=run["id"])
    except Exception as error:  # noqa: BLE001 - AI is optional; rules still work
        return AIBudget(None, 0, f"AI unavailable: {error}")
    if not getattr(provider, "external", False):
        reason = (provider.describe() or {}).get("reason") if hasattr(provider, "describe") else None
        return AIBudget(None, 0, f"rules only: {reason or 'no AI provider'}")
    limit = options.max_ai_calls
    if limit is None:
        limit = int(platform.config.extra.get("scraper_max_ai_calls") or DEFAULT_MAX_AI_CALLS)
    return AIBudget(provider, limit, concurrency=options.ai_concurrency)


class _Progress:
    """Live counters in ``stats.progress`` (and the run's status = stage), written from the task thread."""

    def __init__(self, platform: Any, ctx: Ctx, run: Dict[str, Any], reporter: Any) -> None:
        self.platform, self.ctx, self.run, self.reporter = platform, ctx, run, reporter
        self.state: Dict[str, Any] = dict(run["stats"].get("progress") or {})
        self._last_write = 0.0
        self._lock = threading.Lock()
        self.stage = run["status"]

    def stage_from_thread(self, stage: str) -> None:
        with self._lock:
            self.stage = stage

    def set(self, *, force: bool = True, status: Optional[str] = None, **changes: Any) -> None:
        self.state.update(changes)
        if status:
            self.stage = status
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        self.state["stage"] = self.stage
        values: Dict[str, Any] = {"stats": {**self.run["stats"], "progress": dict(self.state)}}
        if self.stage in WORKING_STATES or self.stage in ("paused", "completed", "cancelled", "failed"):
            values["status"] = self.stage
        try:
            self.run = self.platform.store.update(self.ctx, "scrape_runs", self.run["id"], values)
        except Exception:  # noqa: BLE001 - progress must never break the work
            log.exception("could not save scrape progress for %s", self.run["id"])
        message = f"{self.state.get('processed', 0)}/{self.state.get('total', 0)} URLs · {self.stage}"
        self.reporter.progress(message, done=self.state.get("processed", 0), total=self.state.get("total", 0),
                               stage=self.stage, current_url=self.state.get("current_url"),
                               records=self.state.get("records", 0), pages=self.state.get("pages", 0))


def _save_pages(store: Any, ctx: Ctx, run_id: str, index: int, pages: List[Dict[str, Any]]) -> None:
    for page in pages:
        if page.get("outcome") in (Outcome.SKIPPED, Outcome.LIMIT):
            continue   # never fetched: kept in the result row, not as a page
        key = url_key(page["url"])
        reason = page.get("browser_reason")
        values = {"run_id": run_id, "input_index": index, "url_key": key, "url": page["url"][:2048],
                  "final_url": (page.get("final_url") or page["url"])[:2048], "kind": page.get("kind") or "input",
                  "depth": int(page.get("depth") or 0), "page_no": page.get("page_no"),
                  "outcome": page["outcome"], "http_status": int(page.get("http_status") or 0),
                  "attempts": int(page.get("attempts") or 1), "records": int(page.get("records") or 0),
                  "browser_used": bool(page.get("browser_used")), "browser_reason": reason[:200] if reason else None,
                  "browser_duration_ms": page.get("browser_duration_ms"),
                  "browser_outcome": page.get("browser_outcome"),
                  "error": str(page["error"])[:1000] if page.get("error") else None,
                  "fetched_at": page.get("fetched_at")}
        existing = store.first(ctx, "scrape_pages", {"run_id": run_id, "url_key": key})
        if existing is not None:
            store.update(ctx, "scrape_pages", existing["id"], values)
        else:
            store.insert(ctx, "scrape_pages", values)


def _save_result(store: Any, ctx: Ctx, run: Mapping[str, Any], index: int, item: Mapping[str, Any],
                 page: PageExtraction, schema: Mapping[str, Any], existing: Optional[Mapping[str, Any]]
                 ) -> Dict[str, Any]:
    extracted_at = page.fetched_at or datetime.now(timezone.utc)
    rows = [flatten(r, schema, source_url=page.final_url or item["url"], extracted_at=extracted_at)
            for r in page.records]
    for row in rows:
        row["input_url"], row["input_row"], row["input_batch"] = item["url"], item.get("row"), item.get("batch")
    data = {"index": index, "input": dict(item), "outcome": page.outcome, "records": rows, "pages": page.pages,
            "ai_used": page.ai_used, "stats": page.stats,
            "reason": page.problems[0] if page.problems and page.outcome not in (Outcome.OK, Outcome.EMPTY) else None}
    values = {"run_id": run["id"], "url": item["url"][:2048], "final_url": (page.final_url or item["url"])[:2048],
              "status": store_status(page.outcome), "method": page.method, "data": data,
              "field_sources": {k: v["method"] for k, v in (rows[0]["_evidence"] if rows else {}).items()},
              "problems": page.problems[:50], "fetched_at": extracted_at}
    _save_pages(store, ctx, run["id"], index, page.pages)
    if existing is not None:
        return store.update(ctx, "scrape_results", existing["id"], values)
    return store.insert(ctx, "scrape_results", values)


def _inputs(run: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return list(run["stats"].get("inputs") or
                [{"url": u, "row": i + 1, "batch": "-", "source": "list"}
                 for i, u in enumerate(run["stats"].get("urls") or [])])


class _Reporter:
    """For inline runs (research agent): never cancelled or paused from outside."""

    checkpoint: Dict[str, Any] = {}

    def progress(self, *a: Any, **k: Any) -> None:
        pass

    def is_cancelled(self) -> bool:
        return False

    def should_pause(self) -> bool:
        return False


def run_scrape_inline(platform: Any, ctx: Ctx, run_id: str) -> Dict[str, Any]:
    """Run a scrape synchronously in the caller's thread (used by the research agent's
    tool, which already runs inside a background task)."""
    return execute_run(platform, ctx, run_id, _Reporter(), task={"params": {"run_id": run_id}, "attempts": 1})


def run_scrape_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    return execute_run(platform, ctx, task["params"].get("run_id", ""), reporter, task=task)


def execute_run(platform: Any, ctx: Ctx, run_id: str, reporter: Any, *, task: Mapping[str, Any]) -> Dict[str, Any]:
    from cloud.intel.tasks.worker import PermanentTaskError, TaskCancelled, TaskPaused

    store = platform.store
    run = store.find(ctx, "scrape_runs", run_id)
    if run is None:
        raise PermanentTaskError("scrape run not found")
    if run["status"] == "cancelled":
        raise TaskCancelled()
    schema = run["schema"]
    inputs = _inputs(run)
    options = CrawlOptions.from_mapping(run["stats"].get("options"))
    attempt = int(task.get("attempts") or 1)
    existing = {int(r["data"].get("index", -1)): r for r in store.all(ctx, "scrape_results", {"run_id": run["id"]},
                                                                      order="created_at")}
    redo = set()
    if task.get("params", {}).get("retry"):
        redo = {i for i, r in existing.items() if r["data"].get("outcome") in TRANSIENT}
    done = {i: r for i, r in existing.items() if i not in redo}
    stats = dict(run["stats"])
    if done and (attempt > 1 or run["status"] in WORKING_STATES or run["status"] == "paused"):
        recovered = {"attempt": attempt, "resumed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "inputs_already_saved": len(done), "previous_status": run["status"]}
        stats["recoveries"] = list(stats.get("recoveries") or [])[-9:] + [recovered]
        log.info("scrape.recover run=%s attempt=%s saved_inputs=%s", run["id"], attempt, len(done))
    run = store.update(ctx, "scrape_runs", run["id"], {"status": "planning", "error": None, "stats": stats})
    progress = _Progress(platform, ctx, run, reporter)
    started_at = progress.state.get("started_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved_pages = store.all(ctx, "scrape_pages", {"run_id": run["id"]}, cap=100_000)
    progress.set(status="planning", total=len(inputs), processed=len(done),
                 completed=sum(1 for r in done.values() if r["status"] in ("ok", "empty")),
                 failed=sum(1 for r in done.values() if r["status"] == "error"),
                 blocked=sum(1 for r in done.values() if r["status"] == "blocked"),
                 pages=len(saved_pages), records=sum(len(r["data"].get("records") or []) for r in done.values()),
                 requests=sum(int((r["data"].get("stats") or {}).get("requests") or 0) for r in done.values()),
                 browser_pages=sum(1 for p in saved_pages if p["browser_used"]),
                 current_url=None, current_urls=[], started_at=started_at, attempt=attempt,
                 options=options.as_dict())

    ai = _ai_budget(platform, ctx, run, options)
    budget = RunBudget(max_runtime_s=options.max_runtime_s, max_records=options.max_records,
                       max_browser_pages=max(0, options.max_browser_pages - progress.state.get("browser_pages", 0)),
                       already_records=progress.state.get("records", 0))
    limiter = DomainLimiter(concurrency=options.domain_concurrency, max_requests=options.max_requests_per_domain)
    fetcher = PageFetcher(_http(platform), _renderer(platform, options), options=options, limiter=limiter,
                          budget=budget)
    visited = VisitedSet(p["url_key"] for p in saved_pages)
    todo = [i for i in range(len(inputs)) if i not in done]
    in_flight: Dict[Future, int] = {}
    current: Dict[int, str] = {}
    stop: Optional[str] = None

    def crawl(index: int) -> PageExtraction:
        url = inputs[index]["url"]
        try:
            return scrape_one(url, schema, fetcher, ai, on_stage=progress.stage_from_thread, options=options,
                              budget=budget, visited=visited)
        except Exception as error:  # noqa: BLE001 - one bad page must not sink the run
            log.exception("scrape.error run=%s url=%s", run["id"], url)
            return PageExtraction(url, url, Outcome.FAILED, problems=[f"{type(error).__name__}: {error}"[:300]],
                                  fetched_at=datetime.now(timezone.utc))

    try:
        progress.set(status="fetching")
        with ThreadPoolExecutor(max_workers=options.concurrency, thread_name_prefix="scrape") as pool:
            while todo or in_flight:
                if stop is None:
                    if reporter.is_cancelled():
                        stop = "cancelled"
                    elif reporter.should_pause():
                        stop = "paused"
                    if stop:
                        budget.reason = f"the run was {stop}"
                        budget.stop.set()
                while stop is None and todo and len(in_flight) < options.concurrency:
                    index = todo.pop(0)
                    current[index] = inputs[index]["url"]
                    in_flight[pool.submit(crawl, index)] = index
                if not in_flight:
                    break
                finished, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    index = in_flight.pop(future)
                    current.pop(index, None)
                    page = future.result()
                    if stop == "paused" and page.stats.get("interrupted"):
                        continue   # redone on resume
                    if stop == "cancelled" and page.stats.get("interrupted"):
                        page.problems.append("stopped: the run was cancelled")
                    progress.set(status="saving", force=False)
                    row = _save_result(store, ctx, run, index, inputs[index], page, schema, existing.get(index))
                    existing[index] = row
                    ok = page.outcome in (Outcome.OK, Outcome.EMPTY)
                    fetched = [p for p in page.pages if p.get("outcome") not in (Outcome.SKIPPED, Outcome.LIMIT)]
                    log.info("scrape.input run=%s index=%s outcome=%s pages=%s records=%s", run["id"], index,
                             page.outcome, len(fetched), len(page.records))
                    progress.state.update(
                        processed=progress.state["processed"] + 1,
                        completed=progress.state["completed"] + (1 if ok else 0),
                        failed=progress.state["failed"] + (0 if ok or page.outcome in BLOCKING else 1),
                        blocked=progress.state.get("blocked", 0) + (1 if page.outcome in BLOCKING else 0),
                        pages=progress.state["pages"] + len(fetched),
                        records=progress.state["records"] + len(page.records),
                        requests=progress.state.get("requests", 0) + int(page.stats.get("requests") or 0),
                        browser_pages=progress.state.get("browser_pages", 0) + int(page.stats.get("browser_pages") or 0),
                        checkpoint_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                stage = progress.stage if progress.stage in WORKING_STATES and progress.stage != "saving" else "fetching"
                progress.set(status=stage, force=False, current_urls=list(current.values())[:10],
                             current_url=next(iter(current.values()), None),
                             ai_calls=ai.calls, ai_failures=ai.failures, ai_note=ai.summary)
        if stop == "cancelled":
            _finish(platform, ctx, progress, schema, inputs, ai, status="cancelled")
            raise TaskCancelled()
        if stop == "paused":
            progress.set(status="paused", current_url=None, current_urls=[])
            raise TaskPaused({"remaining": len(inputs) - len(existing)})
        return _finish(platform, ctx, progress, schema, inputs, ai, status="completed")
    except (TaskCancelled, TaskPaused):
        raise
    except Exception as error:
        store.update(ctx, "scrape_runs", run["id"], {"status": "failed", "error": f"{type(error).__name__}: {error}"[:4000]})
        raise


def _finish(platform: Any, ctx: Ctx, progress: _Progress, schema: Mapping[str, Any], inputs: List[Dict[str, Any]],
            ai: AIBudget, *, status: str) -> Dict[str, Any]:
    """Assemble every result row (including ones from before a pause/crash) into the output files."""
    store = platform.store
    run = progress.run
    progress.set(status="validating", current_url=None, current_urls=[])
    results = sorted(store.all(ctx, "scrape_results", {"run_id": run["id"]}, order="created_at"),
                     key=lambda r: int(r["data"].get("index", 0)))
    saved_pages = {p["url_key"]: p for p in store.all(ctx, "scrape_pages", {"run_id": run["id"]}, cap=100_000)}
    records: List[Dict[str, Any]] = []
    inputs_table: List[Dict[str, Any]] = []
    pages_table: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    outcomes: Dict[str, int] = {}
    validation_errors = 0
    for row in results:
        data = row["data"]
        outcome = data.get("outcome") or row["status"].upper()
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        item = data.get("input") or {}
        page_list = data.get("pages") or []
        inputs_table.append({"row": item.get("row"), "batch": item.get("batch"), "source": item.get("source"),
                             "url": row["url"], "final_url": row["final_url"], "outcome": outcome,
                             "reason": data.get("reason") or "; ".join(row["problems"][:2]) or None,
                             "records": len(data.get("records") or []),
                             "pages_fetched": sum(1 for p in page_list if p.get("outcome") not in
                                                  (Outcome.SKIPPED, Outcome.LIMIT)),
                             "ai_used": bool(data.get("ai_used")), "extraction_method": row["method"]})
        for page in page_list:
            fetched = page.get("outcome") not in (Outcome.SKIPPED, Outcome.LIMIT)
            saved = saved_pages.get(url_key(page["url"])) if fetched else None
            pages_table.append({"input_row": item.get("row"), "input_url": row["url"], "url": page["url"],
                                "final_url": page.get("final_url"), "kind": page.get("kind"),
                                "page_no": page.get("page_no"), "outcome": page.get("outcome"),
                                "http_status": page.get("http_status"), "attempts": page.get("attempts"),
                                "records": (saved or page).get("records"), "browser_used": page.get("browser_used"),
                                "browser_reason": page.get("browser_reason"),
                                "browser_duration_ms": page.get("browser_duration_ms"),
                                "browser_outcome": page.get("browser_outcome"), "error": page.get("error"),
                                "fetched_at": page.get("fetched_at")})
            if page.get("outcome") not in (Outcome.OK, Outcome.EMPTY, Outcome.SKIPPED):
                errors.append({"kind": "page", "input_row": item.get("row"), "url": page["url"],
                               "outcome": page.get("outcome"), "error": page.get("error")})
        for problem in row["problems"]:
            errors.append({"kind": "input", "input_row": item.get("row"), "url": row["url"], "outcome": outcome,
                           "error": problem})
        for record in data.get("records") or []:
            validation_errors += len(record.get("_problems") or [])
            for name, info in (record.get("_rejected") or {}).items():
                errors.append({"kind": "validation", "input_row": item.get("row"), "url": record.get("source_url"),
                               "outcome": "INVALID", "error": info.get("error"), "field": name,
                               "value": str(info.get("value"))[:200]})
            records.append(dict(record))
    progress.set(status="normalizing")
    fields = [f["name"] for f in schema["fields"]]
    records, filtered_out = apply_filters(records, schema.get("filters") or [])
    records, duplicates = dedupe_records(records, fields, schema.get("entity", "company"))
    tables = views(records, schema)
    started = progress.state.get("started_at")
    try:
        duration = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() if started else None
    except ValueError:
        duration = None
    blocked = sum(n for o, n in outcomes.items() if o in BLOCKING)
    completed_urls = sum(n for o, n in outcomes.items() if o in (Outcome.OK, Outcome.EMPTY))
    observability = {
        "total_urls": len(inputs), "completed_urls": completed_urls, "blocked_urls": blocked,
        "failed_urls": len(results) - completed_urls - blocked,
        "pages_visited": sum(1 for p in pages_table if p["outcome"] not in (Outcome.SKIPPED, Outcome.LIMIT)),
        "records_found": len(records), "companies_found": len(tables["companies"]), "jobs_found": len(tables["jobs"]),
        "browser_pages": sum(1 for p in pages_table if p.get("browser_used")), "ai_calls": ai.calls,
        "ai_failures": ai.failures, "validation_errors": validation_errors, "duplicates_removed": duplicates,
        "filtered_out": filtered_out, "requests": progress.state.get("requests", 0),
        "duration_seconds": round(duration, 1) if duration is not None else None}
    progress.set(status="saving")
    summary = {"run_id": run["id"], "status": status, "instruction": run.get("instruction"), "outcomes": outcomes,
               "records": len(records), "duplicates_removed": duplicates, "filtered_out": filtered_out,
               "validation_problems": validation_errors, "ai_calls": ai.calls, "ai_failures": ai.failures,
               "ai_note": ai.summary, "options": progress.state.get("options"),
               "input_report": run["stats"].get("input_report"), **observability}
    files = write_outputs(platform.storage, f"platform/{ctx.workspace_id}/scrape/{run['id']}", run["id"], schema,
                          records, inputs_table, summary, pages=pages_table, errors=errors)
    counts = {"ok": 0, "blocked": 0, "error": 0, "empty": 0}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    progress.state.update(stage=status, records=len(records), jobs=len(tables["jobs"]),
                          companies=len(tables["companies"]), ai_calls=ai.calls, ai_failures=ai.failures,
                          ai_note=ai.summary, current_url=None, current_urls=[], errors=len(errors),
                          finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          pages_visited=observability["pages_visited"],
                          duration_seconds=observability["duration_seconds"])
    stats = {**progress.run["stats"], "counts": counts, "outcomes": outcomes, "records": len(records),
             "duplicates_removed": duplicates, "filtered_out": filtered_out, "validation_problems": validation_errors,
             "files": files, "ai_calls": ai.calls, "ai_failures": ai.failures, "ai_note": ai.summary,
             "observability": observability, "errors": len(errors), "progress": dict(progress.state)}
    store.update(ctx, "scrape_runs", run["id"], {"status": status, "stats": stats})
    if status == "completed":
        try:  # workflows on "scrape_completed"; best-effort, never fails the run
            platform.service("automation").emit(ctx, "scrape_completed", f"scrape:{run['id']}",
                                                {"run_id": run["id"], "records": len(records), "counts": counts})
        except Exception:  # noqa: BLE001
            log.debug("scrape_completed emit skipped", exc_info=True)
    log.info("scrape.finish run=%s status=%s records=%s pages=%s ai_calls=%s duration=%s", run["id"], status,
             len(records), observability["pages_visited"], ai.calls, observability["duration_seconds"])
    return {"run_id": run["id"], "records": len(records), "counts": counts, "outcomes": outcomes,
            "duplicates_removed": duplicates, "filtered_out": filtered_out, "ai_calls": ai.calls}
