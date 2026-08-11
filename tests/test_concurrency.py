"""Tests for the concurrent crawl, outcome classification and settings.

Concurrency is where a crawler quietly loses data: a result written to the
wrong slot, a company dropped when a worker exits early, a session shared
across threads. These tests hold the pool to the same guarantees the sequential
path always gave — one result per record, in input order, nothing lost — and add
the ones only the pool needs: a session per worker, and per-host politeness.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Dict, List, Optional

from config.settings import SETTINGS, Settings, configure, default_workers
from crawler.crawler_engine import CrawlerEngine, CrawlResult, Outcome, _HostThrottle
from crawler.platform_detector import Platform
from models.job import Job

WORKDAY_URL = "https://acme.wd1.myworkdayjobs.com/en-US/External"

configure(browser_fallback=False, discover_careers=False, diagnostics=False, max_workers=1)


def _job(company: str = "Acme", url: str = "https://acme.com/jobs/1") -> Job:
    """Build one posting.

    Args:
        company: Company name.
        url: Posting URL, which is half of the deduplication key.

    Returns:
        The job.
    """
    return Job(company_name=company, job_title="Engineer", job_url=url)


class CountingSession:
    """A session that records which thread built it."""

    def __init__(self) -> None:
        self.thread = threading.current_thread().name
        self.closed = False

    def close(self) -> None:
        """Record that the session was closed."""
        self.closed = True


class SlowAdapter:
    """An adapter that sleeps, so the pool has something to overlap.

    Args:
        delay: Seconds each call takes.
        jobs: Postings to return per call.
    """

    def __init__(self, delay: float = 0.05, jobs: Optional[List[Job]] = None) -> None:
        self.delay = delay
        self.jobs = jobs if jobs is not None else [_job()]
        self.threads: List[str] = []
        self._lock = threading.Lock()

    def __call__(
        self, career_url: str, company_name: str, session: Optional[object] = None
    ) -> List[Job]:
        """Return the scripted postings after a delay.

        Args:
            career_url: Ignored.
            company_name: Used to make each posting URL unique.
            session: Recorded so a test can check it was per-thread.

        Returns:
            The scripted postings, retitled per company.
        """
        time.sleep(self.delay)
        with self._lock:
            self.threads.append(threading.current_thread().name)
        return [
            Job(company_name=company_name, job_title="Engineer", job_url=f"https://x/{company_name}")
            for _ in self.jobs
        ]


class TestConcurrentCrawl(unittest.TestCase):
    """The pool keeps every guarantee the sequential path gave."""

    RECORDS = [{"company": f"Company {index}", "it_link": WORKDAY_URL} for index in range(24)]

    def test_one_result_per_record_in_input_order(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: SlowAdapter(delay=0.01)})

        results = engine.crawl_all(self.RECORDS, max_workers=8)

        self.assertEqual([result.company for result in results], [r["company"] for r in self.RECORDS])

    def test_no_company_is_lost(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: SlowAdapter(delay=0.01)})

        results = engine.crawl_all(self.RECORDS, max_workers=8)

        self.assertEqual(len(results), len(self.RECORDS))
        self.assertTrue(all(result.jobs for result in results))

    def test_work_is_spread_across_the_pool(self) -> None:
        adapter = SlowAdapter(delay=0.02)
        engine = CrawlerEngine(registry={Platform.WORKDAY: adapter})

        engine.crawl_all(self.RECORDS, max_workers=6)

        self.assertGreater(len(set(adapter.threads)), 1)

    def test_each_worker_gets_its_own_session(self) -> None:
        built: List[CountingSession] = []
        lock = threading.Lock()

        def factory() -> CountingSession:
            session = CountingSession()
            with lock:
                built.append(session)
            return session

        adapter = SlowAdapter(delay=0.02)
        engine = CrawlerEngine(registry={Platform.WORKDAY: adapter}, session_factory=factory)

        engine.crawl_all(self.RECORDS, max_workers=4)

        self.assertEqual(len(built), 4)
        self.assertTrue(all(session.closed for session in built))
        # A session must never be handed to a thread that did not build it.
        self.assertEqual({session.thread for session in built}, set(adapter.threads))

    def test_a_failing_company_does_not_stop_the_others(self) -> None:
        def flaky(career_url: str, company_name: str, session: Optional[object] = None) -> List[Job]:
            if company_name.endswith("3"):
                raise RuntimeError("boom")
            return [_job(company=company_name, url=f"https://x/{company_name}")]

        engine = CrawlerEngine(registry={Platform.WORKDAY: flaky})

        results = engine.crawl_all(self.RECORDS, max_workers=8)

        failed = [result for result in results if not result.ok]
        self.assertEqual(
            [result.company for result in failed], ["Company 3", "Company 13", "Company 23"]
        )
        self.assertTrue(all(result.jobs for result in results if result.ok))
        self.assertEqual(len(results), len(self.RECORDS))

    def test_one_record_stays_sequential(self) -> None:
        """Spinning up a pool for a single company is pure overhead."""
        built: List[CountingSession] = []
        engine = CrawlerEngine(
            registry={Platform.WORKDAY: SlowAdapter(delay=0)},
            session_factory=lambda: built.append(CountingSession()) or built[-1],
        )

        engine.crawl_all([self.RECORDS[0]], max_workers=8)

        self.assertEqual(len(built), 1)

    def test_workers_are_never_more_than_companies(self) -> None:
        built: List[CountingSession] = []
        lock = threading.Lock()

        def factory() -> CountingSession:
            session = CountingSession()
            with lock:
                built.append(session)
            return session

        engine = CrawlerEngine(
            registry={Platform.WORKDAY: SlowAdapter(delay=0.01)}, session_factory=factory
        )

        engine.crawl_all(self.RECORDS[:3], max_workers=16)

        self.assertEqual(len(built), 3)

    def test_empty_input_is_fine(self) -> None:
        engine = CrawlerEngine(registry={})
        self.assertEqual(engine.crawl_all([], max_workers=8), [])


class TestHostThrottle(unittest.TestCase):
    """Politeness is per host, so unrelated companies still run in parallel."""

    def test_repeat_visits_to_one_host_are_spaced(self) -> None:
        throttle = _HostThrottle(0.05)

        started = time.monotonic()
        for _ in range(3):
            throttle.wait("https://acme.com/careers")
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.09)

    def test_different_hosts_do_not_wait_for_each_other(self) -> None:
        throttle = _HostThrottle(0.2)

        started = time.monotonic()
        for index in range(5):
            throttle.wait(f"https://host{index}.com/careers")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)

    def test_a_zero_delay_never_sleeps(self) -> None:
        throttle = _HostThrottle(0.0)

        started = time.monotonic()
        for _ in range(50):
            throttle.wait("https://acme.com/")

        self.assertLess(time.monotonic() - started, 0.05)

    def test_an_unparsable_url_is_ignored(self) -> None:
        throttle = _HostThrottle(0.5)
        started = time.monotonic()

        for url in ("", "not a url", "mailto:x@y.z"):
            throttle.wait(url)

        self.assertLess(time.monotonic() - started, 0.05)


class TestOutcome(unittest.TestCase):
    """Every company lands in exactly one report."""

    CASES = (
        (CrawlResult("A", Platform.WORKDAY, jobs=[_job()]), Outcome.JOBS),
        (CrawlResult("A", Platform.WORKDAY), Outcome.NO_JOBS),
        (CrawlResult("A", Platform.GEM, error="no adapter for Gem"), Outcome.UNSUPPORTED),
        (CrawlResult("A", Platform.ICIMS, error="AdapterHttpError: HTTP 403"), Outcome.TECHNICAL),
        (CrawlResult("A", Platform.UNKNOWN, error="no usable URL in the input sheet"), Outcome.UNKNOWN),
    )

    def test_classification(self) -> None:
        for result, expected in self.CASES:
            with self.subTest(expected=expected.value):
                self.assertIs(result.outcome, expected)

    def test_jobs_beat_every_other_signal(self) -> None:
        result = CrawlResult("A", Platform.ICIMS, jobs=[_job()], error="AdapterHttpError: page 2")
        self.assertIs(result.outcome, Outcome.JOBS)

    def test_ok_is_unchanged_by_the_new_classification(self) -> None:
        self.assertTrue(CrawlResult("A", Platform.WORKDAY).ok)
        self.assertFalse(CrawlResult("A", Platform.WORKDAY, error="x").ok)

    def test_timing_and_discovery_default_to_nothing(self) -> None:
        result = CrawlResult("A", Platform.WORKDAY)
        self.assertEqual(result.seconds, 0.0)
        self.assertFalse(result.discovered)


class TestSeedFallback(unittest.TestCase):
    """A wrong ``it_link`` must not sink a company with a good ``career_url``.

    Real sheets carry ``it_link`` values pointing at a vendor's public job
    aggregator rather than the company's own board. Version 1 took the first
    usable URL and never looked at the alternatives, so those companies were
    reported as failures — or worse, as producing another employer's postings.
    """

    RECORD = {
        "company": "Acme",
        "it_link": WORKDAY_URL,
        "career_url": "https://acme.com/careers",
        "website": "https://acme.com",
    }

    def _engine(self, workday_jobs: List[Job], generic_jobs: List[Job]) -> CrawlerEngine:
        """Build an engine with a scripted adapter per platform.

        Args:
            workday_jobs: What the Workday adapter returns.
            generic_jobs: What the generic adapter returns.

        Returns:
            The engine.
        """
        return CrawlerEngine(
            registry={
                Platform.WORKDAY: lambda url, name, session=None: list(workday_jobs),
                Platform.GENERIC_HTML: lambda url, name, session=None: list(generic_jobs),
            }
        )

    def test_candidates_are_listed_best_first_without_duplicates(self) -> None:
        record = {
            "company": "Acme",
            "it_link": "https://acme.com/careers",
            "career_url": "https://acme.com/careers/",
            "website": "https://acme.com",
        }

        candidates = CrawlerEngine.seed_candidates(record)

        self.assertEqual([field for _, field, _ in candidates], ["it_link", "website"])

    def test_the_first_seed_is_used_when_it_produces_jobs(self) -> None:
        engine = self._engine([_job(url="https://x/wd")], [_job(url="https://x/generic")])

        result = engine.crawl_company(self.RECORD)

        self.assertEqual(result.seed_field, "it_link")
        self.assertIs(result.platform, Platform.WORKDAY)

    def test_it_falls_through_to_the_next_seed_when_the_first_gives_nothing(self) -> None:
        engine = self._engine([], [_job(url="https://x/generic")])

        result = engine.crawl_company(self.RECORD)

        self.assertEqual(result.seed_field, "career_url")
        self.assertIs(result.platform, Platform.GENERIC_HTML)
        self.assertEqual(len(result.jobs), 1)

    def test_it_falls_through_when_the_first_seed_raises(self) -> None:
        def explode(url: str, name: str, session: Optional[object] = None) -> List[Job]:
            raise RuntimeError("that is the vendor's aggregator, not this company")

        engine = CrawlerEngine(
            registry={
                Platform.WORKDAY: explode,
                Platform.GENERIC_HTML: lambda url, name, session=None: [_job(url="https://x/g")],
            }
        )

        result = engine.crawl_company(self.RECORD)

        self.assertEqual(len(result.jobs), 1)
        self.assertIsNone(result.error)

    def test_the_first_seeds_outcome_is_reported_when_every_seed_fails(self) -> None:
        def explode(url: str, name: str, session: Optional[object] = None) -> List[Job]:
            raise RuntimeError("boom")

        engine = CrawlerEngine(
            registry={Platform.WORKDAY: explode, Platform.GENERIC_HTML: lambda *a, **k: []}
        )

        result = engine.crawl_company(self.RECORD)

        self.assertEqual(result.seed_field, "it_link")
        self.assertIn("boom", result.error or "")

    def test_no_more_than_two_urls_are_tried(self) -> None:
        """Trying every column would re-crawl the same site and double the run."""
        tried: List[str] = []

        def record_url(url: str, name: str, session: Optional[object] = None) -> List[Job]:
            tried.append(url)
            return []

        engine = CrawlerEngine(
            registry={Platform.WORKDAY: record_url, Platform.GENERIC_HTML: record_url}
        )
        engine.crawl_company(self.RECORD)

        self.assertEqual(len(tried), 2)

    def test_a_single_url_record_behaves_exactly_as_before(self) -> None:
        tried: List[str] = []

        def record_url(url: str, name: str, session: Optional[object] = None) -> List[Job]:
            tried.append(url)
            return []

        engine = CrawlerEngine(registry={Platform.WORKDAY: record_url})
        result = engine.crawl_company({"company": "Acme", "it_link": WORKDAY_URL})

        self.assertEqual(tried, [WORKDAY_URL])
        self.assertEqual(result.seed_field, "it_link")


class TestBrowserRescue(unittest.TestCase):
    """A failed board gets one browser attempt before it is written off."""

    RECORD = {"company": "Acme", "it_link": WORKDAY_URL}

    def _engine(self) -> CrawlerEngine:
        """Build an engine whose only adapter always fails.

        Returns:
            The engine.
        """

        def always_fails(career_url: str, company_name: str, session: Optional[object] = None):
            raise RuntimeError("anti-bot interstitial")

        return CrawlerEngine(registry={Platform.WORKDAY: always_fails})

    def test_no_rescue_is_attempted_when_the_run_forbids_the_browser(self) -> None:
        configure(browser_fallback=False)

        result = self._engine().crawl_company(self.RECORD)

        self.assertFalse(result.jobs)
        self.assertIn("anti-bot interstitial", result.error or "")
        self.assertIs(result.outcome, Outcome.TECHNICAL)

    def test_a_rescue_that_finds_jobs_clears_the_error(self) -> None:
        import adapters.generic as generic

        rescued = [_job(url="https://acme.com/jobs/rescued")]
        original = generic.render_and_extract
        configure(browser_fallback=True)
        generic.render_and_extract = lambda *args, **kwargs: rescued

        try:
            result = self._engine().crawl_company(self.RECORD)
        finally:
            generic.render_and_extract = original
            configure(browser_fallback=False)

        self.assertEqual(result.jobs, rescued)
        self.assertIsNone(result.error)
        self.assertIs(result.outcome, Outcome.JOBS)

    def test_a_rescue_that_finds_nothing_keeps_the_original_error(self) -> None:
        import adapters.generic as generic

        original = generic.render_and_extract
        configure(browser_fallback=True)
        generic.render_and_extract = lambda *args, **kwargs: []

        try:
            result = self._engine().crawl_company(self.RECORD)
        finally:
            generic.render_and_extract = original
            configure(browser_fallback=False)

        self.assertIn("anti-bot interstitial", result.error or "")

    def test_a_rescue_that_itself_fails_is_contained(self) -> None:
        import adapters.generic as generic

        def explode(*args: object, **kwargs: object) -> List[Job]:
            raise RuntimeError("no browser today")

        original = generic.render_and_extract
        configure(browser_fallback=True)
        generic.render_and_extract = explode

        try:
            result = self._engine().crawl_company(self.RECORD)
        finally:
            generic.render_and_extract = original
            configure(browser_fallback=False)

        self.assertIn("anti-bot interstitial", result.error or "")

    def test_rescued_jobs_keep_the_platform_label(self) -> None:
        import adapters.generic as generic

        seen: Dict[str, object] = {}

        def capture(url: str, company: str, platform: str = "", **kwargs: object) -> List[Job]:
            seen["platform"] = platform
            return []

        original = generic.render_and_extract
        configure(browser_fallback=True)
        generic.render_and_extract = capture

        try:
            self._engine().crawl_company(self.RECORD)
        finally:
            generic.render_and_extract = original
            configure(browser_fallback=False)

        self.assertEqual(seen["platform"], "Workday")


class TestSettings(unittest.TestCase):
    """The shipped defaults must be inert; only a run turns things on."""

    def test_defaults_do_nothing_surprising(self) -> None:
        fresh = Settings()
        self.assertEqual(fresh.max_workers, 1)
        self.assertFalse(fresh.browser_fallback)
        self.assertFalse(fresh.discover_careers)
        self.assertFalse(fresh.diagnostics)
        self.assertEqual(fresh.per_host_delay, 0.0)

    def test_configure_applies_and_ignores_none(self) -> None:
        original = SETTINGS.max_workers
        try:
            configure(max_workers=7)
            self.assertEqual(SETTINGS.max_workers, 7)
            configure(max_workers=None)
            self.assertEqual(SETTINGS.max_workers, 7)
        finally:
            configure(max_workers=original)

    def test_an_unknown_setting_is_a_programming_error(self) -> None:
        with self.assertRaises(KeyError):
            configure(no_such_setting=1)

    def test_default_workers_is_a_sane_range(self) -> None:
        self.assertGreaterEqual(default_workers(), 4)
        self.assertLessEqual(default_workers(), 16)


if __name__ == "__main__":
    unittest.main()
