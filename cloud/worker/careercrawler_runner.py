"""Run cloud jobs through the existing CareerCrawler engine.

This is the only module under ``cloud/`` that imports the crawler, and it
imports only the engine-facing pieces::

    crawler.crawler_engine.CrawlerEngine     seed selection, discovery, adapters, browser rescue
    utils.http.build_session                 the crawler's HTTP session policy
    exporters.excel_exporter.export_jobs     the crawler's own XLSX export
    models.job.Job                           the posting record
    config.settings.configure / SETTINGS     the crawler's process-wide knobs

It never imports ``store`` (the SQLite queue and ``state/crawler.db``),
``sheets`` (Google Sheets), ``crawler.weekly_run``, ``crawler.checkpoint`` or
``crawler.sync`` — ``cloud/tests/test_isolation.py`` fails the build if it does.
Those are the weekly production run's state; a cloud job has none of it.

**What a job does.** Each company becomes the same record shape the crawler
reads from its input sheet (``company`` + ``website``) and goes through
:meth:`CrawlerEngine.crawl_company` — the engine picks the seed, discovers the
careers page, detects the platform, runs the adapter and, if enabled, the
browser rescue. Nothing about how a board is crawled lives here.

**What this module adds** is what a multi-tenant service needs around that
call: a DNS check that the website resolves only to public addresses,
per-company progress, cancellation between companies, a wall-clock limit, a
private workspace for exports and logs, and structured results.

**Process-wide settings.** The crawler's ``SETTINGS`` object is global to the
process. :func:`apply_crawler_settings` sets it once for the worker process —
diagnostics off, and every default output path pointed into the cloud runtime
root rather than the crawler's ``output/``. The production crawler runs in its
own process with its own defaults, which this never touches.

Supported job types: ``single_company`` and ``bulk_companies``. ``weekly_crawl``
(the production roster run, which is Sheets-driven) and ``discovery`` are not
executed here; the API records them with an explanatory status instead.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import socket
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from cloud.shared.models import CompanyTarget, Job, JobType, ResultKind, TargetStatus
from cloud.shared.urls import UnsafeTargetError, resolve_public_addresses
from cloud.worker.results import neutralise_cell
from cloud.worker.runner import Artifact, JobRunner, RunContext, RunOutcome, RunResult
from cloud.worker.workspace import DEFAULT_RUNTIME_ROOT, check_runtime_root

__all__ = ["CareerCrawlerRunner", "apply_crawler_settings", "restore_crawler_settings"]

log = logging.getLogger(__name__)

SUPPORTED = frozenset({JobType.SINGLE_COMPANY, JobType.BULK_COMPANIES})

#: Only these crawler modules may be imported by the cloud. Checked by tests.
ALLOWED_CRAWLER_MODULES = frozenset(
    {
        "config.settings",
        "crawler.crawler_engine",
        "exporters.excel_exporter",
        "models.job",
        "utils.browser",
        "utils.http",
    }
)

_settings_lock = threading.Lock()


def apply_crawler_settings(
    runtime_root: Path,
    *,
    browser_fallback: bool,
    discover_careers: bool = True,
    retries: int = 2,
    browser_budget: Optional[int] = None,
    host_concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    """Configure the crawler for cloud jobs in this process. Returns the previous values.

    ``max_workers`` is deliberately not touched: the cloud runner schedules
    companies itself, and the production default belongs to the production run.
    """
    from config.settings import SETTINGS, configure

    root = check_runtime_root(runtime_root)
    overrides: Dict[str, Any] = {
        "browser_fallback": browser_fallback,
        "discover_careers": discover_careers,
        "retries": retries,
        "diagnostics": False,
        "detect_filters": False,
        "filter_render_budget": 0,
        # Never the crawler's own output/: if anything did write a default path,
        # it would land in the cloud runtime root.
        "diagnostics_dir": root / "_crawler_defaults" / "unknown_platforms",
        "output_dir": root / "_crawler_defaults" / "output",
    }
    if browser_budget is not None:
        overrides["browser_budget"] = browser_budget
    if host_concurrency is not None:
        overrides["host_concurrency"] = host_concurrency
    with _settings_lock:
        previous = {name: getattr(SETTINGS, name) for name in overrides}
        configure(**overrides)
    return previous


def restore_crawler_settings(previous: Dict[str, Any]) -> None:
    from config.settings import SETTINGS

    with _settings_lock:
        for name, value in previous.items():
            setattr(SETTINGS, name, value)


@dataclasses.dataclass
class _Tally:
    completed: int = 0
    failed: int = 0
    jobs_found: int = 0


class CareerCrawlerRunner(JobRunner):
    """Crawl a job's companies with the existing engine.

    Args:
        company_concurrency: Companies crawled at once within one job.
        browser_fallback: Allow the engine's headless-browser rescue.
        max_runtime_seconds: Wall-clock limit per attempt; companies not yet
            started when it passes are skipped.
        max_postings: Upper bound on postings kept per job.
        engine_factory: Builds the engine. Tests inject one with fake adapters;
            production uses the real registry.
        session_factory: Builds one HTTP session per crawl thread.
        resolver: ``getaddrinfo``-compatible, for the public-address check.
        runtime_root: Checked against CareerCrawler's protected directories.
    """

    name = "careercrawler"
    supported_types = SUPPORTED

    def __init__(
        self,
        *,
        company_concurrency: int = 4,
        browser_fallback: bool = False,
        discover_careers: bool = True,
        retries: int = 2,
        max_runtime_seconds: float = 3600.0,
        max_postings: int = 100_000,
        engine_factory: Optional[Callable[[], Any]] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        configure_crawler: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= company_concurrency <= 32:
            raise ValueError("company_concurrency must be between 1 and 32")
        self._concurrency = company_concurrency
        self._browser = browser_fallback
        self._discover = discover_careers
        self._retries = retries
        self._max_runtime = max_runtime_seconds
        self._max_postings = max_postings
        self._engine_factory = engine_factory
        self._session_factory = session_factory
        self._resolver = resolver
        self._runtime_root = check_runtime_root(runtime_root)
        self._configure = configure_crawler
        self._configured = False
        self._clock = clock

    # --- setup ---------------------------------------------------------------

    def _ensure_configured(self) -> None:
        if self._configure and not self._configured:
            apply_crawler_settings(
                self._runtime_root,
                browser_fallback=self._browser,
                discover_careers=self._discover,
                retries=self._retries,
            )
            self._configured = True

    def _engine(self) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory()
        from crawler.crawler_engine import CrawlerEngine

        return CrawlerEngine()

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from utils.http import build_session

        return build_session(retries=self._retries)

    # --- the run -------------------------------------------------------------

    def run(self, job: Job, context: RunContext) -> RunResult:
        if not self.supports(job.type):
            return RunResult.failed(f"{job.type.value} jobs are not run by the CareerCrawler runner yet")
        workspace = getattr(context, "workspace", None)
        if workspace is None:
            return RunResult.failed("the CareerCrawler runner needs a job workspace")
        if not job.targets:
            return RunResult.failed("the job names no companies")

        self._ensure_configured()
        started = self._clock()
        deadline = started + self._max_runtime
        thread_prefix = f"cc-{job.job_id[-12:]}"
        log_sink = self._attach_log(workspace.logs / "crawl.log", thread_prefix)

        engine = self._engine()
        total = len(job.targets)
        tally = _Tally()
        lock = threading.Lock()
        slots: List[Optional[Tuple[Dict[str, Any], List[Any]]]] = [None] * total
        work: "queue.Queue[Tuple[int, CompanyTarget]]" = queue.Queue()
        for item in enumerate(job.targets):
            work.put(item)
        stop = threading.Event()
        timed_out = threading.Event()

        context.update(
            total=total, completed=0, failed=0, jobs_found=0, current_phase="crawling",
            message=f"Crawling {total} {'company' if total == 1 else 'companies'}",
        )

        def finish(position: int, company: Dict[str, Any], jobs: List[Any]) -> None:
            with lock:
                slots[position] = (company, jobs)
                tally.completed += 1
                if company["status"] != TargetStatus.COMPLETED.value:
                    tally.failed += 1
                tally.jobs_found += len(jobs)
                snapshot = dataclasses.replace(tally)
            context.target_finished(
                position,
                status=TargetStatus(company["status"]),
                platform=company.get("platform"),
                outcome=company.get("outcome"),
                jobs_found=len(jobs),
                error=company.get("error"),
            )
            context.update(
                completed=snapshot.completed,
                failed=snapshot.failed,
                jobs_found=snapshot.jobs_found,
                message=f"{snapshot.completed} of {total} companies crawled",
            )

        def crawl_thread() -> None:
            session = None
            try:
                session = self._session()
                while not stop.is_set():
                    if self._clock() > deadline:
                        timed_out.set()
                        stop.set()
                        return
                    if context.is_cancelled():
                        stop.set()
                        return
                    try:
                        position, target = work.get_nowait()
                    except queue.Empty:
                        return
                    self._crawl_one(engine, session, position, target, context, finish)
            finally:
                if session is not None and callable(getattr(session, "close", None)):
                    session.close()
                if self._browser:
                    try:
                        from utils.browser import close_current_thread

                        close_current_thread()
                    except Exception:
                        log.debug("browser teardown failed", exc_info=True)

        threads = [
            threading.Thread(target=crawl_thread, name=f"{thread_prefix}-{index}", daemon=True)
            for index in range(min(self._concurrency, total))
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            self._detach_log(log_sink)

        if context.is_cancelled():
            return RunResult.cancelled()

        # Companies never reached (time limit) are recorded, not silently dropped.
        for position, target in enumerate(job.targets):
            if slots[position] is None:
                finish(
                    position,
                    self._company_row(target, TargetStatus.SKIPPED, error="not started: time limit reached"),
                    [],
                )

        return self._result(job, workspace, slots, tally, started, timed_out.is_set())

    def _crawl_one(
        self,
        engine: Any,
        session: Any,
        position: int,
        target: CompanyTarget,
        context: RunContext,
        finish: Callable[[int, Dict[str, Any], List[Any]], None],
    ) -> None:
        label = target.label()
        context.target_started(position)
        context.update(current_company=label)

        if not target.website:
            finish(
                position,
                self._company_row(target, TargetStatus.SKIPPED, error="no website given; name-only lookup is not supported yet"),
                [],
            )
            return

        parts = urlsplit(target.website)
        try:
            resolve_public_addresses(parts.hostname or "", parts.port, resolver=self._resolver)
        except UnsafeTargetError as refused:
            finish(position, self._company_row(target, TargetStatus.FAILED, error=str(refused)), [])
            return

        record = {"company": target.company_name or (parts.hostname or label), "website": target.website}
        company_started = time.monotonic()
        try:
            result = engine.crawl_company(record, session=session)
        except Exception as error:  # crawl_company contains adapter errors; this is belt and braces
            log.exception("engine raised on %s", label)
            finish(position, self._company_row(target, TargetStatus.FAILED, error=f"{type(error).__name__}: {error}"), [])
            return

        jobs = list(result.jobs)
        status = TargetStatus.COMPLETED if (jobs or result.error is None) else TargetStatus.FAILED
        finish(
            position,
            self._company_row(
                target,
                status,
                platform=getattr(result.platform, "value", str(result.platform)),
                outcome=getattr(result.outcome, "value", None),
                error=result.error,
                seed_url=result.seed_url,
                discovered=result.discovered,
                seconds=round(time.monotonic() - company_started, 2),
                jobs_found=len(jobs),
            ),
            jobs,
        )

    @staticmethod
    def _company_row(target: CompanyTarget, status: TargetStatus, **fields: Any) -> Dict[str, Any]:
        return {
            "company_name": target.company_name,
            "website": target.website,
            "status": status.value,
            "platform": fields.get("platform"),
            "outcome": fields.get("outcome"),
            "jobs_found": fields.get("jobs_found", 0),
            "error": fields.get("error"),
            "seed_url": fields.get("seed_url"),
            "discovered": fields.get("discovered", False),
            "seconds": fields.get("seconds"),
        }

    def _result(
        self,
        job: Job,
        workspace: Any,
        slots: List[Optional[Tuple[Dict[str, Any], List[Any]]]],
        tally: _Tally,
        started: float,
        timed_out: bool,
    ) -> RunResult:
        from exporters.excel_exporter import export_jobs
        from models.job import Job as Posting

        companies: List[Dict[str, Any]] = []
        postings: List[Any] = []
        seen = set()
        for slot in slots:
            if slot is None:
                continue
            company, jobs = slot
            companies.append(company)
            for posting in jobs:
                if posting.key in seen or len(postings) >= self._max_postings:
                    continue
                seen.add(posting.key)
                postings.append(posting)

        fields = [item.name for item in dataclasses.fields(Posting)]
        safe = [
            dataclasses.replace(posting, **{name: neutralise_cell(getattr(posting, name)) for name in fields})
            for posting in postings
        ]
        workbook = export_jobs(safe, workspace.output / "jobs.xlsx", fallback_when_locked=False)

        artifacts = [
            Artifact(
                ResultKind.JOBS_XLSX,
                Path(workbook),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                row_count=len(safe),
            )
        ]
        crawl_log = workspace.logs / "crawl.log"
        if crawl_log.is_file() and crawl_log.stat().st_size > 0:
            artifacts.append(Artifact(ResultKind.CRAWL_LOG, crawl_log, "text/plain; charset=utf-8"))

        platforms = Counter(company["platform"] for company in companies if company.get("platform"))
        summary = {
            "companies": len(job.targets),
            "completed_companies": tally.completed,
            "failed_companies": tally.failed,
            "jobs_found": len(postings),
            "platforms": dict(platforms),
            "seconds": round(self._clock() - started, 2),
            "timed_out": timed_out,
        }
        return RunResult(
            RunOutcome.COMPLETED,
            summary=summary,
            postings=[dataclasses.asdict(posting) for posting in postings],
            posting_fields=fields,
            companies=companies,
            artifacts=artifacts,
        )

    # --- logging -------------------------------------------------------------

    @staticmethod
    def _attach_log(path: Path, thread_prefix: str) -> Optional[int]:
        """Copy this job's crawler log lines (loguru) into the workspace."""
        try:
            from loguru import logger
        except ImportError:  # pragma: no cover
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        return logger.add(
            str(path),
            level="INFO",
            enqueue=False,
            filter=lambda record: record["thread"].name.startswith(thread_prefix),
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
        )

    @staticmethod
    def _detach_log(sink: Optional[int]) -> None:
        if sink is None:
            return
        from loguru import logger

        try:
            logger.remove(sink)
        except ValueError:
            pass
