"""External sources: ATS public APIs, keyed/partner connectors, honest status, ingestion."""

from __future__ import annotations

import unittest

from cloud.intel.sources.adapters import ADAPTERS, ATSPublicAdapter, LinkedInAdapter, USAJobsAdapter
from cloud.intel.sources.base import SourceQuery, SourceUnavailable, dedupe_postings
from cloud.tests.test_platform_sources_support import GREENHOUSE, LEVER, FakeResponse, fetcher, make_platform


class ATSAdapterTests(unittest.TestCase):
    def test_greenhouse_normalize_and_dedupe(self) -> None:
        fetch, session = fetcher({"boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE})
        rows = ATSPublicAdapter(fetcher=fetch).run(SourceQuery(board_url="https://boards.greenhouse.io/acme"))
        self.assertEqual(len(rows), 2)  # the duplicate 101 is dropped
        sap = rows[0]
        self.assertEqual(sap["title"], "SAP FICO Consultant")
        self.assertEqual(sap["job_url"], "https://boards.greenhouse.io/acme/jobs/101")
        self.assertEqual(sap["location"], "Remote - US")
        self.assertEqual(sap["workplace_type"], "remote")
        self.assertEqual(sap["department"], "IT")
        self.assertEqual(sap["ats"], "Greenhouse")
        self.assertTrue(sap["posted_at"].startswith("2026-09-01T14:00:00"))
        self.assertEqual(sap["source_name"], "ats:greenhouse")
        self.assertIn("boards-api.greenhouse.io", session.calls[0][1])

    def test_lever_normalize(self) -> None:
        fetch, _ = fetcher({"api.lever.co/v0/postings/acme": LEVER})
        rows = ATSPublicAdapter(fetcher=fetch).run(SourceQuery(board_url="https://jobs.lever.co/acme",
                                                               company="Acme Corp"))
        self.assertEqual(rows[0]["company_name"], "Acme Corp")
        self.assertEqual(rows[0]["workplace_type"], "hybrid")
        self.assertEqual(rows[0]["employment_type"], "Full-time")
        self.assertEqual(rows[0]["department"], "Information Technology")
        self.assertTrue(rows[0]["posted_at"].startswith("2025-08-24"))

    def test_keyword_filter(self) -> None:
        fetch, _ = fetcher({"greenhouse.io/v1/boards/acme/jobs": GREENHOUSE})
        rows = ATSPublicAdapter(fetcher=fetch).run(SourceQuery(board_url="https://boards.greenhouse.io/acme",
                                                               keywords="sap"))
        self.assertEqual([r["title"] for r in rows], ["SAP FICO Consultant"])

    def test_blocked_board_is_reported_not_retried_around(self) -> None:
        fetch, session = fetcher({"greenhouse.io": FakeResponse(403, {"error": "forbidden"})})
        from cloud.intel.sources.base import SourceError

        with self.assertRaises(SourceError):
            ATSPublicAdapter(fetcher=fetch).run(SourceQuery(board_url="https://boards.greenhouse.io/acme"))
        self.assertEqual(len(session.calls), 1)

    def test_private_targets_are_refused(self) -> None:
        from cloud.intel.core.http import SafeFetcher
        from cloud.intel.sources.base import SourceError
        from cloud.tests.test_platform_sources_support import FakeSession

        session = FakeSession({"": GREENHOUSE})
        private = SafeFetcher(session=session, resolver=lambda h, p, type=None: [(2, 1, 6, "", ("10.1.2.3", p))],
                              per_host_delay=0, respect_robots=False)
        with self.assertRaises(SourceError):
            ATSPublicAdapter(fetcher=private).run(SourceQuery(board_url="https://boards.greenhouse.io/acme"))
        self.assertEqual(session.calls, [])

    def test_dedupe_by_url_then_content(self) -> None:
        rows = dedupe_postings([{"job_url": "https://x/1", "title": "a"}, {"job_url": "https://X/1/", "title": "b"},
                                {"company_name": "A", "title": "T", "location": "L"},
                                {"company_name": "a", "title": "t", "location": "l"}])
        self.assertEqual(len(rows), 2)


class RestrictedSourceTests(unittest.TestCase):
    def test_partner_and_licensed_sources_are_honest_without_credentials(self) -> None:
        for name in ("linkedin", "indeed", "dice", "wellfound", "builtin", "ziprecruiter", "usajobs", "adzuna"):
            adapter = ADAPTERS[name]()
            health = adapter.health()
            self.assertEqual(health["status"], "not_configured", name)
            self.assertTrue(health["detail"], name)
            with self.assertRaises(SourceUnavailable, msg=name):
                adapter.search(SourceQuery(keywords="sap"))

    def test_linkedin_stays_unavailable_even_with_a_token(self) -> None:
        adapter = LinkedInAdapter(credentials={"partner_api_token": "t" * 20})
        self.assertEqual(adapter.health()["status"], "configured_unverified")
        with self.assertRaises(SourceUnavailable):
            adapter.search(SourceQuery(keywords="sap"))

    def test_usajobs_call_shape_with_a_key(self) -> None:
        fetch, session = fetcher({"data.usajobs.gov/api/search": {"SearchResult": {"SearchResultItems": [
            {"MatchedObjectDescriptor": {"PositionTitle": "IT Specialist", "PositionURI": "https://usajobs.gov/j/1",
                                         "OrganizationName": "VA", "PositionID": "1",
                                         "PublicationStartDate": "2026-09-01"}}]}}})
        adapter = USAJobsAdapter(credentials={"api_key": "k" * 12, "email": "ops@example.com"}, fetcher=fetch)
        self.assertEqual(adapter.health()["status"], "configured_unverified")
        rows = adapter.run(SourceQuery(keywords="it specialist"))
        self.assertEqual(rows[0]["title"], "IT Specialist")
        headers = session.calls[0][2]["headers"]
        self.assertEqual(headers["Authorization-Key"], "k" * 12)
        self.assertEqual(headers["User-Agent"], "ops@example.com")


class SourceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.jobs, _ = make_platform()
        self.service = self.platform.service("sources")

    def test_list_sources_reports_status_per_workspace(self) -> None:
        by_name = {s["name"]: s for s in self.service.list_sources(self.ctx)}
        self.assertEqual(by_name["ats_public"]["health"]["status"], "ok")
        self.assertEqual(by_name["linkedin"]["health"]["status"], "not_configured")
        self.assertIn("partner", by_name["linkedin"]["health"]["detail"].lower())
        self.platform.service("providers").set_credentials(self.ctx, "adzuna", {"app_id": "id123456",
                                                                                "app_key": "key12345678"})
        by_name = {s["name"]: s for s in self.service.list_sources(self.ctx)}
        self.assertEqual(by_name["adzuna"]["health"]["status"], "configured_unverified")

    def test_search_ingests_through_the_jobs_service_and_records_usage(self) -> None:
        self.service.fetcher, _ = fetcher({"greenhouse.io/v1/boards/acme/jobs": GREENHOUSE})
        result = self.service.search(self.ctx, "ats_public", {"board_url": "https://boards.greenhouse.io/acme"})
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.jobs.batches[0]["source_name"], "ats_public")
        self.assertEqual(self.jobs.batches[0]["source_kind"], "external_source")
        usage = self.platform.service("credits").usage(self.ctx, "ats_public")
        self.assertEqual(usage["operations"]["ats_public:search"]["units"], 2)

    def test_unconfigured_search_refuses_and_records_the_failure(self) -> None:
        with self.assertRaises(SourceUnavailable):
            self.service.search(self.ctx, "indeed", {"keywords": "sap"})
        self.assertEqual(self.jobs.batches, [])

    def test_careercrawler_becomes_a_crawl_task(self) -> None:
        result = self.service.search(self.ctx, "careercrawler",
                                     {"companies": [{"name": "Acme", "website": "https://acme.com"}]})
        task = self.platform.tasks.get(self.ctx, result["task_id"])
        self.assertEqual(task["kind"], "crawl")

    def test_source_search_task(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        self.service.fetcher, _ = fetcher({"api.lever.co/v0/postings/acme": LEVER})
        task = self.platform.tasks.submit(self.ctx, "source_search", {
            "source": "ats_public", "query": {"board_url": "https://jobs.lever.co/acme"}})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual(done["result"]["postings"], 1)

    def test_unavailable_source_task_fails_without_retrying(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        task = self.platform.tasks.submit(self.ctx, "source_search", {"source": "linkedin", "query": {}})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("LinkedIn", done["error"])


if __name__ == "__main__":
    unittest.main()
