"""Unit tests for :mod:`crawler.crawler_engine`.

Adapters are injected as fakes throughout, so nothing here touches the network.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from crawler.crawler_engine import (
    ADAPTER_MODULES,
    CrawlerEngine,
    CrawlResult,
    build_registry,
)
from crawler.platform_detector import Platform
from models.job import Job

WORKDAY_URL = "https://acme.wd1.myworkdayjobs.com/en-US/External"
GREENHOUSE_URL = "https://boards.greenhouse.io/acme"
LEVER_URL = "https://jobs.lever.co/acme"
ASHBY_URL = "https://jobs.ashbyhq.com/acme"


def _job(company: str = "Acme", title: str = "Engineer", url: str = "https://x/1") -> Job:
    """Build one posting."""
    return Job(company_name=company, job_title=title, job_url=url)


class RecordingAdapter:
    """Fake ``fetch_jobs`` that records its calls and returns canned jobs."""

    def __init__(self, jobs: Optional[List[Job]] = None, raises: Optional[Exception] = None) -> None:
        self.jobs = jobs if jobs is not None else []
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self, career_url: str, company_name: str, session: Optional[object] = None
    ) -> List[Job]:
        self.calls.append({"url": career_url, "company": company_name, "session": session})
        if self.raises is not None:
            raise self.raises
        return list(self.jobs)


class TestSelectSeed(unittest.TestCase):
    """The best available URL is chosen, and junk is stepped over."""

    def setUp(self) -> None:
        self.engine = CrawlerEngine(registry={})

    def test_it_link_wins(self) -> None:
        record = {
            "company": "Acme",
            "it_link": WORKDAY_URL,
            "career_url": GREENHOUSE_URL,
            "website": "https://acme.com",
        }
        url, source, platform = self.engine.select_seed(record)
        self.assertEqual((url, source, platform), (WORKDAY_URL, "it_link", Platform.WORKDAY))

    def test_falls_back_to_career_url(self) -> None:
        record = {"company": "Acme", "it_link": "", "career_url": GREENHOUSE_URL}
        url, source, platform = self.engine.select_seed(record)
        self.assertEqual((url, source, platform), (GREENHOUSE_URL, "career_url", Platform.GREENHOUSE))

    def test_falls_back_to_website(self) -> None:
        record = {"company": "Acme", "website": "acme.com"}
        url, source, platform = self.engine.select_seed(record)
        self.assertEqual((source, platform), ("website", Platform.GENERIC_HTML))

    def test_skips_filler_it_link(self) -> None:
        """A junk it_link must not mask a good career_url."""
        record = {"company": "Acme", "it_link": "N/A", "career_url": GREENHOUSE_URL}
        url, source, _ = self.engine.select_seed(record)
        self.assertEqual((url, source), (GREENHOUSE_URL, "career_url"))

    def test_missing_it_link_key_is_tolerated(self) -> None:
        """Records from a sheet with no IT LINK column still work."""
        record = {"company": "Acme", "career_url": GREENHOUSE_URL, "website": "acme.com"}
        url, source, _ = self.engine.select_seed(record)
        self.assertEqual((url, source), (GREENHOUSE_URL, "career_url"))

    def test_no_usable_url(self) -> None:
        record = {"company": "Acme", "it_link": "N/A", "career_url": "", "website": "TBD"}
        self.assertEqual(self.engine.select_seed(record), ("", "", Platform.UNKNOWN))


class TestDispatch(unittest.TestCase):
    """The registry, not a branch, decides which adapter runs."""

    def test_routes_to_the_matching_adapter(self) -> None:
        workday = RecordingAdapter([_job(title="WD Engineer")])
        greenhouse = RecordingAdapter([_job(title="GH Engineer")])
        engine = CrawlerEngine(
            registry={Platform.WORKDAY: workday, Platform.GREENHOUSE: greenhouse}
        )

        result = engine.crawl_company({"company": "Acme", "it_link": WORKDAY_URL})

        self.assertEqual([job.job_title for job in result.jobs], ["WD Engineer"])
        self.assertEqual(len(workday.calls), 1)
        self.assertEqual(greenhouse.calls, [])

    def test_passes_seed_url_and_company_to_the_adapter(self) -> None:
        adapter = RecordingAdapter()
        engine = CrawlerEngine(registry={Platform.LEVER: adapter})

        engine.crawl_company({"company": "Acme Corp", "it_link": LEVER_URL})

        self.assertEqual(adapter.calls[0]["url"], LEVER_URL)
        self.assertEqual(adapter.calls[0]["company"], "Acme Corp")

    def test_generic_html_routes_to_the_generic_adapter(self) -> None:
        generic = RecordingAdapter([_job()])
        engine = CrawlerEngine(registry={Platform.GENERIC_HTML: generic})

        result = engine.crawl_company({"company": "Acme", "career_url": "https://acme.com/careers"})

        self.assertEqual(result.platform, Platform.GENERIC_HTML)
        self.assertEqual(len(result.jobs), 1)

    def test_unsupported_platform_returns_empty_without_error_state(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: RecordingAdapter([_job()])})

        result = engine.crawl_company({"company": "Acme", "it_link": ASHBY_URL})

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.platform, Platform.ASHBY)
        self.assertIn("no adapter", result.error or "")

    def test_no_usable_url_returns_empty(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: RecordingAdapter([_job()])})

        result = engine.crawl_company({"company": "Acme", "career_url": "N/A"})

        self.assertEqual(result.jobs, [])
        self.assertEqual(result.platform, Platform.UNKNOWN)

    def test_record_without_company_name_is_skipped(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: RecordingAdapter([_job()])})

        result = engine.crawl_company({"company": "  ", "it_link": WORKDAY_URL})

        self.assertEqual(result.jobs, [])
        self.assertFalse(result.ok)


class TestFailureIsolation(unittest.TestCase):
    """One broken company cannot end the run."""

    def test_adapter_exception_is_contained(self) -> None:
        engine = CrawlerEngine(
            registry={Platform.WORKDAY: RecordingAdapter(raises=RuntimeError("board is down"))}
        )

        result = engine.crawl_company({"company": "Acme", "it_link": WORKDAY_URL})

        self.assertEqual(result.jobs, [])
        self.assertFalse(result.ok)
        self.assertIn("board is down", result.error or "")

    def test_run_continues_past_a_failure(self) -> None:
        engine = CrawlerEngine(
            registry={
                Platform.WORKDAY: RecordingAdapter(raises=RuntimeError("boom")),
                Platform.GREENHOUSE: RecordingAdapter([_job(company="Beta", url="https://x/2")]),
            }
        )

        jobs = engine.crawl(
            [
                {"company": "Acme", "it_link": WORKDAY_URL},
                {"company": "Beta", "it_link": GREENHOUSE_URL},
            ]
        )

        self.assertEqual([job.company_name for job in jobs], ["Beta"])

    def test_even_a_baseexception_subclass_is_reported_as_a_result(self) -> None:
        """Adapters raising odd exception types must not escape either."""
        engine = CrawlerEngine(
            registry={Platform.WORKDAY: RecordingAdapter(raises=ValueError("bad payload"))}
        )

        results = engine.crawl_all([{"company": "Acme", "it_link": WORKDAY_URL}])

        self.assertEqual(len(results), 1)
        self.assertIn("ValueError", results[0].error or "")

    def test_adapter_returning_none_is_tolerated(self) -> None:
        class NoneAdapter:
            def __call__(self, career_url, company_name, session=None):  # type: ignore[no-untyped-def]
                return None

        engine = CrawlerEngine(registry={Platform.WORKDAY: NoneAdapter()})

        self.assertEqual(engine.crawl_company({"company": "Acme", "it_link": WORKDAY_URL}).jobs, [])


class TestCrawlAll(unittest.TestCase):
    """Batch behaviour: order, dedup, session sharing."""

    def test_one_result_per_record_in_input_order(self) -> None:
        engine = CrawlerEngine(registry={Platform.WORKDAY: RecordingAdapter([_job()])})

        results = engine.crawl_all(
            [
                {"company": "Acme", "it_link": WORKDAY_URL},
                {"company": "Beta", "it_link": "N/A"},
                {"company": "Gamma", "it_link": ASHBY_URL},
            ]
        )

        self.assertEqual([r.company for r in results], ["Acme", "Beta", "Gamma"])

    def test_crawl_deduplicates_across_companies(self) -> None:
        shared = _job(company="Acme", url="https://x/1")
        engine = CrawlerEngine(registry={Platform.WORKDAY: RecordingAdapter([shared, shared])})

        jobs = engine.crawl([{"company": "Acme", "it_link": WORKDAY_URL}])

        self.assertEqual(len(jobs), 1)

    def test_session_is_built_once_shared_and_closed(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        built: List[FakeSession] = []

        def factory() -> FakeSession:
            session = FakeSession()
            built.append(session)
            return session

        adapter = RecordingAdapter()
        engine = CrawlerEngine(registry={Platform.WORKDAY: adapter}, session_factory=factory)

        engine.crawl_all(
            [
                {"company": "Acme", "it_link": WORKDAY_URL},
                {"company": "Beta", "it_link": WORKDAY_URL},
            ]
        )

        self.assertEqual(len(built), 1)
        self.assertTrue(built[0].closed)
        self.assertEqual([call["session"] for call in adapter.calls], [built[0], built[0]])

    def test_session_is_closed_even_when_a_company_fails(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        session = FakeSession()
        engine = CrawlerEngine(
            registry={Platform.WORKDAY: RecordingAdapter(raises=RuntimeError("boom"))},
            session_factory=lambda: session,
        )

        engine.crawl_all([{"company": "Acme", "it_link": WORKDAY_URL}])

        self.assertTrue(session.closed)

    def test_without_a_factory_adapters_get_none(self) -> None:
        adapter = RecordingAdapter()
        engine = CrawlerEngine(registry={Platform.WORKDAY: adapter})

        engine.crawl_all([{"company": "Acme", "it_link": WORKDAY_URL}])

        self.assertIsNone(adapter.calls[0]["session"])

    def test_empty_input(self) -> None:
        engine = CrawlerEngine(registry={})
        self.assertEqual(engine.crawl([]), [])


class TestBuildRegistry(unittest.TestCase):
    """Only adapters that exist and expose fetch_jobs are registered."""

    def test_registers_the_implemented_adapter(self) -> None:
        registry = build_registry()
        self.assertIn(Platform.WORKDAY, registry)
        self.assertTrue(callable(registry[Platform.WORKDAY]))

    def test_skips_modules_without_fetch_jobs(self) -> None:
        """A module that defines no fetch_jobs must not be registered."""
        registry = build_registry({Platform.LEVER: "adapters._paginated_html"})
        self.assertEqual(registry, {})

    def test_skips_missing_modules(self) -> None:
        registry = build_registry({Platform.LEVER: "adapters.does_not_exist"})
        self.assertEqual(registry, {})

    def test_every_implemented_adapter_is_registered(self) -> None:
        registry = build_registry()
        for platform in ADAPTER_MODULES:
            with self.subTest(platform=platform):
                self.assertIn(platform, registry)

    def test_every_mapped_module_lives_under_adapters(self) -> None:
        for platform, module in ADAPTER_MODULES.items():
            with self.subTest(platform=platform):
                self.assertTrue(module.startswith("adapters."))

    def test_engine_defaults_to_the_built_registry(self) -> None:
        engine = CrawlerEngine()
        self.assertIn(Platform.WORKDAY, engine.supported_platforms)


class TestCrawlResult(unittest.TestCase):
    """The result reports success honestly."""

    def test_ok_is_false_when_an_error_is_recorded(self) -> None:
        self.assertFalse(CrawlResult("Acme", Platform.WORKDAY, error="boom").ok)

    def test_ok_is_true_without_an_error(self) -> None:
        self.assertTrue(CrawlResult("Acme", Platform.WORKDAY).ok)

    def test_jobs_default_is_not_shared_between_results(self) -> None:
        first = CrawlResult("Acme", Platform.WORKDAY)
        first.jobs.append(_job())
        self.assertEqual(CrawlResult("Beta", Platform.WORKDAY).jobs, [])


if __name__ == "__main__":
    unittest.main()
