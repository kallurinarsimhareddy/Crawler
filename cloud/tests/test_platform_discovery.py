"""Company discovery: resolution, duplicates, verification with a fake fetcher, SSRF, approval."""

from __future__ import annotations

import unittest

from cloud.intel.core.context import ConflictError
from cloud.intel.core.http import FetchResult, SafeFetcher
from cloud.intel.discovery.service import DiscoveryService, JobPostingSource, score_candidate
from cloud.tests._platform_intel_helpers import make_platform

HOME = """<html><head><meta property="og:site_name" content="Widget Works Inc">
<script type="application/ld+json">{"@type": "Organization", "name": "Widget Works Inc",
 "address": {"addressLocality": "Tulsa", "addressRegion": "OK", "addressCountry": "US"}}</script></head>
<body><a href="/about">About</a><a href="/careers">Careers</a></body></html>"""
CAREERS = '<html><body><a href="https://boards.greenhouse.io/widgetworks">Open roles</a></body></html>'


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url, **_):
        self.calls.append(url)
        if url in self.pages:
            final, text = self.pages[url] if isinstance(self.pages[url], tuple) else (url, self.pages[url])
            return FetchResult(url, final, 200, text=text)
        return FetchResult(url, url, 404, error="not found")


class StubCrm:
    def __init__(self, store):
        self.store, self.calls = store, []

    def upsert_company(self, ctx, values, **kw):
        self.calls.append((values, kw))
        return {"company": self.store.insert(ctx, "companies", {k: v for k, v in values.items()
                                                                 if k != "confidence"}), "created": True}


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.automation = make_platform()
        self.store = self.platform.store
        self.existing = self.store.insert(self.ctx, "companies", {"name": "Known Co", "domain": "known.com",
                                                                  "normalized_name": "known co"})
        self.service: DiscoveryService = self.platform.service("discovery")

    def submit(self, *candidates):
        return self.service.submit_candidates(self.ctx, list(candidates), source_kind="manual", source_name="test")

    def test_statuses_new_duplicate_and_review(self) -> None:
        rows = self.submit({"name": "Known Co", "website": "https://www.known.com"},
                           {"name": "Widget Works", "website": "widgetworks.com"},
                           {"name": "Widget Works Inc", "domain": "widgetworks.com"},
                           {"name": "Known Co"})
        self.assertEqual([r["status"] for r in rows],
                         ["DUPLICATE", "NEW_COMPANY_DISCOVERY", "DUPLICATE", "NEEDS_REVIEW"])
        self.assertEqual(rows[0]["matched_company_id"], self.existing["id"])
        self.assertEqual(rows[3]["match_strength"], "probable")
        steps = [e["step"] for e in rows[1]["evidence"]]
        self.assertEqual(steps, ["normalize", "identity_resolution", "duplicate_detection"])
        # a second batch naming an open candidate's domain is a duplicate of it
        again = self.submit({"name": "Widget", "website": "https://widgetworks.com/x"})
        self.assertEqual(again[0]["status"], "DUPLICATE")

    def test_verification_finds_careers_and_ats(self) -> None:
        row = self.submit({"name": "Widget Works Inc", "website": "https://widgetworks.com"})[0]
        fetcher = FakeFetcher({"https://widgetworks.com": HOME, "https://widgetworks.com/careers": CAREERS})
        verified = self.service.verify(self.ctx, row["id"], fetcher=fetcher)
        self.assertTrue(verified["website_verified"])
        self.assertEqual(verified["careers_url"], "https://widgetworks.com/careers")
        self.assertEqual(verified["ats"], "Greenhouse")
        self.assertEqual((verified["city"], verified["country"]), ("Tulsa", "United States"))
        self.assertEqual(verified["status"], "NEW_COMPANY_DISCOVERY")
        self.assertGreaterEqual(verified["confidence"], 0.9)
        self.assertIn("ats_detection", [e["step"] for e in verified["evidence"]])

    def test_redirect_to_another_domain_needs_review(self) -> None:
        row = self.submit({"name": "Old Brand", "website": "https://oldbrand.com"})[0]
        fetcher = FakeFetcher({"https://oldbrand.com": ("https://newowner.com/", "<html></html>")})
        verified = self.service.verify(self.ctx, row["id"], fetcher=fetcher)
        self.assertEqual(verified["status"], "NEEDS_REVIEW")
        self.assertTrue(verified["steps"]["domain_mismatch"])

    def test_ssrf_target_is_refused_not_fetched(self) -> None:
        row = self.submit({"name": "Evil", "website": "http://169.254.169.254/latest/meta-data"})[0]
        fetcher = SafeFetcher(respect_robots=False)
        verified = self.service.verify(self.ctx, row["id"], fetcher=fetcher)
        self.assertFalse(verified["website_verified"])
        failure = [e for e in verified["evidence"] if e["step"] == "website_verification"][0]
        self.assertIn("unsafe target", failure["detail"]["error"])
        self.assertEqual(verified["status"], "NEEDS_REVIEW")

    def test_approve_goes_through_crm_and_reject_is_final(self) -> None:
        crm = StubCrm(self.store)
        self.platform.override("crm", crm)
        row = self.submit({"name": "Widget Works", "website": "https://widgetworks.com"})[0]
        result = self.service.approve(self.ctx, row["id"])
        self.assertEqual(result["candidate"]["status"], "APPROVED")
        self.assertEqual(crm.calls[0][1]["source_kind"], "discovery")
        self.assertIn("new_company", [e[0] for e in self.automation.events])
        with self.assertRaises(ConflictError):
            self.service.approve(self.ctx, row["id"])
        other = self.submit({"name": "Nope Inc", "website": "https://nope.example"})[0]
        self.assertEqual(self.service.reject(self.ctx, other["id"], "not a fit")["status"], "REJECTED")
        with self.assertRaises(ConflictError):
            self.service.reject(self.ctx, other["id"])

    def test_job_posting_source_and_score(self) -> None:
        self.store.insert(self.ctx, "job_postings", {
            "company_name": "Hiring Co", "domain": "hiring.co", "title": "Dev", "job_url": "https://hiring.co/j/1",
            "url_key": "https://hiring.co/j/1", "first_seen_at": "2026-09-01T00:00:00Z",
            "last_seen_at": "2026-09-01T00:00:00Z", "source_kind": "external_source", "source_name": "x"})
        candidates = JobPostingSource(self.store).candidates(self.ctx)
        self.assertEqual(candidates[0]["name"], "Hiring Co")
        self.assertEqual(score_candidate({}), 0.3)
        self.assertEqual(score_candidate({"website_verified": True, "domain_mismatch": True}), 0.25)


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.automation = make_platform()
        self.store = self.platform.store
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme", "careers_url": "https://a.com/jobs",
                                                                 "ats": "Workday"})
        self.monitoring = self.platform.service("monitoring")

    def test_snapshot_diff(self) -> None:
        cid = self.company["id"]
        self.assertEqual(self.monitoring.check_company(self.ctx, cid), [])  # first snapshot: nothing to compare
        self.store.insert(self.ctx, "contacts", {"company_id": cid, "full_name": "Pat CIO",
                                                 "title": "Chief Information Officer"})
        self.store.insert(self.ctx, "contacts", {"company_id": cid, "full_name": "Sam", "title": "Analyst"})
        self.store.update(self.ctx, "companies", cid, {"ats": "iCIMS", "careers_url": "https://a.com/careers"})
        self.store.insert(self.ctx, "company_technologies", {"company_id": cid, "technology": "SAP ECC",
                                                             "source": "manual", "observed_at": "2026-09-20T00:00:00Z"})
        for i in range(4):
            self.store.insert(self.ctx, "job_postings", {
                "company_id": cid, "company_name": "Acme", "title": f"Job {i}", "job_url": f"https://a.com/{i}",
                "url_key": f"https://a.com/{i}", "first_seen_at": "2026-09-20T00:00:00Z",
                "last_seen_at": "2026-09-20T00:00:00Z", "source_kind": "crawler", "source_name": "x"})
        kinds = sorted(c["change_type"] for c in self.monitoring.check_company(self.ctx, cid))
        self.assertEqual(kinds, ["ats_changed", "careers_url_changed", "hiring_spike", "leadership_change",
                                 "new_contact", "technology_added"])
        self.assertIn("leadership_change", [e[0] for e in self.automation.events])
        self.assertEqual(self.monitoring.check_company(self.ctx, cid), [])  # nothing changed since

    def test_schedule_due_is_idempotent_per_period(self) -> None:
        monitor = self.monitoring.create_monitor(self.ctx, name="Acme weekly", target_type="company",
                                                 target_id=self.company["id"], frequency="weekly")
        self.assertEqual(self.monitoring.schedule_due(self.ctx), 1)
        self.assertEqual(self.monitoring.schedule_due(self.ctx), 0)  # next run is a week away
        self.assertEqual(self.store.count(self.ctx, "platform_tasks", {"kind": "monitor"}), 1)
        nxt = self.store.get(self.ctx, "monitors", monitor["id"])["next_run_at"]
        self.assertGreater(nxt, monitor["next_run_at"])

    def test_monitor_task_runs(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        monitor = self.monitoring.create_monitor(self.ctx, name="m", target_type="company",
                                                 target_id=self.company["id"], frequency="daily")
        task = self.platform.tasks.submit(self.ctx, "monitor", {"monitor_id": monitor["id"]})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertEqual(done["result"]["companies"], 1)
        self.assertIsNotNone(self.store.get(self.ctx, "monitors", monitor["id"])["last_run_at"])


if __name__ == "__main__":
    unittest.main()
