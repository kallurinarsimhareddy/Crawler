"""The crawl task end to end with a fake engine (no network), and the intelligence API."""

from __future__ import annotations

import socket
import tempfile
import unittest
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest import mock

from cloud.intel.jobs import crawl_task
from cloud.intel.tasks.worker import run_task_inline
from cloud.tests._platform_intel_helpers import NoResolver, RecordingAutomation, make_platform


@dataclass
class FakeJob:
    company_name: str
    job_title: str
    job_url: str
    location: str = "Tulsa, OK"
    country: str = ""
    career_page_url: str = ""
    platform: str = "Greenhouse"
    department: str = ""
    employment_type: str = ""
    workplace_type: str = ""
    posted_date: str = ""
    job_id: str = ""


class FakeEngine:
    """Maps a website to (jobs, error). Records every record it was asked to crawl."""

    def __init__(self, boards):
        self.boards, self.records = boards, []

    def crawl_company(self, record, session=None):
        self.records.append(dict(record))
        jobs, error = self.boards.get(record["website"], ([], "unreadable"))
        return SimpleNamespace(jobs=[FakeJob(record["company"], t, u) for t, u in jobs], error=error,
                               platform=SimpleNamespace(value="Greenhouse"), outcome=SimpleNamespace(
                                   value="jobs" if jobs else ("no_jobs" if error is None else "technical")),
                               seed_url="https://boards.greenhouse.io/acme", discovered=True)


def public_resolver(host, port, type=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class CrawlTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.automation = make_platform()
        self.store = self.platform.store
        self.acme = self.store.insert(self.ctx, "companies", {"name": "Acme", "website": "https://acme.com"})
        self.broken = self.store.insert(self.ctx, "companies", {"name": "Broken", "website": "https://broken.com"})
        self.engine = FakeEngine({
            "https://acme.com": ([("RPG Developer", "https://boards.greenhouse.io/acme/jobs/1"),
                                  ("Director of IT", "https://boards.greenhouse.io/acme/jobs/2")], None),
        })
        patcher = mock.patch.multiple(crawl_task, CRAWL_ENGINE_FACTORY=lambda: self.engine,
                                      CRAWL_RESOLVER=public_resolver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_crawl(self, company_ids):
        task = self.platform.tasks.submit(self.ctx, "crawl", {"company_ids": company_ids})
        return run_task_inline(self.platform, self.ctx.workspace_id, task["id"])

    def test_crawl_ingests_records_ats_and_detects_signals(self) -> None:
        done = self.run_crawl([self.acme["id"], self.broken["id"]])
        self.assertEqual(done["status"], "completed", done.get("error"))
        result = done["result"]
        self.assertEqual((result["crawled"], result["failed"], result["new"]), (1, 1, 2))
        acme = self.store.get(self.ctx, "companies", self.acme["id"])
        self.assertEqual(acme["ats"], "Greenhouse")
        self.assertEqual(acme["careers_url"], "https://boards.greenhouse.io/acme")
        self.assertEqual(acme["hiring_count"], 2)
        self.assertIn("LEADERSHIP_HIRING", acme["hiring_signals"])
        self.assertEqual(self.engine.records[0], {"company": "Acme", "website": "https://acme.com"})

    def test_failed_crawl_does_not_close_jobs(self) -> None:
        self.run_crawl([self.acme["id"]])
        self.engine.boards["https://acme.com"] = ([], "HTTP 503")
        self.run_crawl([self.acme["id"]])
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "open"}), 2)
        self.engine.boards["https://acme.com"] = ([("RPG Developer", "https://boards.greenhouse.io/acme/jobs/1")], None)
        self.run_crawl([self.acme["id"]])
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "open"}), 1)

    def test_private_address_is_refused_before_the_engine(self) -> None:
        with mock.patch.object(crawl_task, "CRAWL_RESOLVER",
                               lambda h, p, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", p))]):
            done = self.run_crawl([self.acme["id"]])
        self.assertEqual(done["result"]["failed"], 1)
        self.assertIn("non-public", done["result"]["failures"][0]["error"])
        self.assertEqual(self.engine.records, [])

    def test_crawl_needs_targets(self) -> None:
        task = self.platform.tasks.submit(self.ctx, "crawl", {})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("company_ids", done["error"])


class IntelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.intel.platform import Platform
        from cloud.intel.store.memory import MemoryStore
        from cloud.shared.storage import LocalFileStorage
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        issuer = DevTokenIssuer("intel-api-tests-secret-0123456789abcdefghij")
        self.platform = Platform(MemoryStore())
        self.platform.override("automation", RecordingAutomation())
        self.platform.override("dedupe", NoResolver())
        app = create_app(Settings(auth_mode="dev", results_dir=Path(scratch.name) / "r"),
                         storage=LocalFileStorage(Path(scratch.name) / "r"), token_verifier=issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        session = issuer.issue("intel@example.com")
        self.client.headers["Authorization"] = f"Bearer {session['access_token']}"
        user_id = issuer.verify(session["access_token"]).user_id
        ws = self.platform.store.create_workspace(user_id, "API", f"api-{uuid.uuid4().hex[:6]}")
        self.base = f"/api/v1/w/{ws['id']}"
        from cloud.intel.core.context import Ctx

        self.ctx = Ctx(ws["id"], user_id, "owner")

    def test_jobs_signals_scores_technology_discovery_monitors(self) -> None:
        company = self.platform.store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.com"})
        r = self.client.post(f"{self.base}/jobs/ingest", json={"postings": [
            {"title": "VP of Information Technology", "job_url": "https://acme.com/j/1", "company_id": company["id"],
             "description": "Lead our SAP S/4HANA migration"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["inserted"], 1)
        jobs = self.client.get(f"{self.base}/jobs", params={"seniority": "vp"}).json()
        self.assertEqual(jobs["total"], 1)
        self.assertEqual(self.client.get(f"{self.base}/jobs", params={"bogus": "x"}).status_code, 422)

        r = self.client.post(f"{self.base}/companies/{company['id']}/scores")
        self.assertEqual(r.status_code, 200, r.text)
        types = {s["signal_type"] for s in r.json()["signals"]}
        self.assertIn("LEADERSHIP_HIRING", types)
        scores = self.client.get(f"{self.base}/companies/{company['id']}/scores").json()
        self.assertIn("breakdown", scores)
        self.assertGreaterEqual(len(self.client.get(f"{self.base}/hiring-signals").json()["items"]), 1)
        sig = scores["signals"][0]
        self.assertEqual(self.client.post(f"{self.base}/hiring-signals/{sig['id']}/dismiss").json()["status"],
                         "dismissed")

        detected = self.client.post(f"{self.base}/technology/detect", json={"text": "RPGLE on AS/400"}).json()
        self.assertEqual({t["technology"] for t in detected["items"]}, {"RPG", "IBM AS/400"})
        self.assertIn("ERP", self.client.get(f"{self.base}/technology/taxonomy").json()["categories"])
        r = self.client.post(f"{self.base}/company-technologies",
                             json={"company_id": company["id"], "technology": "Epicor", "category": "ERP"})
        self.assertEqual(r.status_code, 201, r.text)

        r = self.client.post(f"{self.base}/discovery/candidates",
                             json={"candidates": [{"name": "Widget", "website": "https://widget.example"}]})
        self.assertEqual(r.status_code, 201, r.text)
        cand = r.json()["items"][0]
        self.assertEqual(self.client.post(f"{self.base}/discovery/candidates/{cand['id']}/reject",
                                          json={"reason": "no"}).json()["status"], "REJECTED")
        self.assertEqual(self.client.post(f"{self.base}/discovery/candidates/{cand['id']}/maybe").status_code, 404)
        task = self.client.post(f"{self.base}/discovery/run", json={}).json()
        self.assertEqual(task["kind"], "discovery")

        r = self.client.post(f"{self.base}/monitors", json={"name": "Acme", "target_type": "company",
                                                            "target_id": company["id"], "frequency": "daily"})
        self.assertEqual(r.status_code, 201, r.text)
        run = self.client.post(f"{self.base}/monitors/{r.json()['id']}/run")
        self.assertEqual(run.status_code, 201, run.text)
        self.assertEqual(self.client.post(f"{self.base}/crawl", json={}).status_code, 422)
        crawl = self.client.post(f"{self.base}/crawl", json={"company_ids": [company["id"]]},
                                 headers={"Idempotency-Key": "crawl-1"})
        again = self.client.post(f"{self.base}/crawl", json={"company_ids": [company["id"]]},
                                 headers={"Idempotency-Key": "crawl-1"})
        self.assertEqual(crawl.json()["id"], again.json()["id"])
        self.assertGreaterEqual(len(self.client.get(f"{self.base}/change-events").json()["items"]), 1)

    def test_other_workspace_is_404(self) -> None:
        other = self.platform.store.create_workspace(str(uuid.uuid4()), "Other", f"o-{uuid.uuid4().hex[:6]}")
        self.assertEqual(self.client.get(f"/api/v1/w/{other['id']}/jobs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
