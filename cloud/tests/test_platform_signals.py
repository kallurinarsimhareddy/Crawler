"""Hiring signals (each positive + negative), idempotency, expiry, dismissal and explainable scores."""

from __future__ import annotations

import unittest

from cloud.intel.signals.service import SIGNAL_WEIGHTS
from cloud.tests._platform_intel_helpers import add_job, make_platform


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.automation = make_platform()
        self.store = self.platform.store
        self.company = self.store.insert(self.ctx, "companies", {
            "name": "Acme", "domain": "acme.com", "industry": "Manufacturing", "country": "United States",
            "employee_count": 800, "technologies": ["JD Edwards EnterpriseOne"]})
        self.store.insert(self.ctx, "campaigns", {"key": "COX", "name": "Cox", "technologies": ["JD Edwards"],
                                                  "status": "active"})
        self.signals = self.platform.service("signals")
        self.cid = self.company["id"]

    def detect(self):
        return {s["signal_type"]: s for s in self.signals.detect_for_company(self.ctx, self.cid)}

    def job(self, title, **kw):
        return add_job(self.platform, self.ctx, self.cid, title, **kw)

    # --- one positive and one negative case per signal ------------------------------

    def test_new_role(self) -> None:
        self.job("Old ERP Analyst", days_ago=30)
        self.assertNotIn("NEW_ROLE", self.detect())
        self.job("JDE Developer", days_ago=2)
        sig = self.detect()["NEW_ROLE"]
        self.assertEqual(len(sig["job_posting_ids"]), 1)
        self.assertEqual(sig["evidence"][0]["title"], "JDE Developer")

    def test_irrelevant_jobs_do_not_count(self) -> None:
        self.job("Receptionist", days_ago=1, relevant=False, department="other")
        self.assertEqual(self.detect(), {})

    def test_multiple_relevant_roles(self) -> None:
        self.job("A", days_ago=20)
        self.job("B", days_ago=20)
        self.assertNotIn("MULTIPLE_RELEVANT_ROLES", self.detect())
        self.job("C", days_ago=20)
        self.assertIn("MULTIPLE_RELEVANT_ROLES", self.detect())

    def test_hiring_spike(self) -> None:
        self.job("Base", days_ago=60)
        for i in range(4):
            self.job(f"Recent {i}", days_ago=2)
        sig = self.detect()["HIRING_SPIKE"]
        self.assertEqual(len(sig["job_posting_ids"]), 4)
        self.assertIn("hiring_spike", [e[0] for e in self.automation.events])
        self.assertEqual(self.store.count(self.ctx, "change_events", {"change_type": "hiring_spike"}), 1)

    def test_no_spike_when_steady(self) -> None:
        for i in range(12):  # 2 per 14 days for 84 days
            self.job(f"Base {i}", days_ago=16 + i * 7)
        self.job("Recent 1", days_ago=3)
        self.job("Recent 2", days_ago=4)
        self.job("Recent 3", days_ago=5)
        self.assertNotIn("HIRING_SPIKE", self.detect())

    def test_spike_with_no_history_is_low_confidence(self) -> None:
        for i in range(4):
            self.job(f"Recent {i}", days_ago=2)
        sig = self.detect()["HIRING_SPIKE"]
        self.assertIn("limited_history", sig["reason_codes"])
        self.assertLess(sig["confidence"], 0.5)

    def test_hiring_velocity(self) -> None:
        for i in range(4):
            self.job(f"Now {i}", days_ago=5 + i)
        self.assertNotIn("HIRING_VELOCITY", self.detect())  # a burst with nothing before
        self.job("Before 1", days_ago=40)
        self.job("Before 2", days_ago=45)
        self.assertIn("HIRING_VELOCITY", self.detect())

    def test_long_open_role_uses_posted_date_when_earlier(self) -> None:
        self.job("Young", days_ago=10)
        self.assertNotIn("LONG_OPEN_ROLE", self.detect())
        self.job("Old via posted date", days_ago=5, posted_days_ago=50)
        sig = self.detect()["LONG_OPEN_ROLE"]
        self.assertEqual(sig["evidence"][0]["age_basis"], "posted_at")

    def test_hard_to_fill(self) -> None:
        self.job("Plain analyst", days_ago=70)
        self.assertNotIn("HARD_TO_FILL", self.detect())
        self.job("RPG Developer", days_ago=70, technologies=["RPG"])
        sig = self.detect()["HARD_TO_FILL"]
        self.assertIn("open_60d_specialized_technology", sig["reason_codes"])

    def test_specialized_technology(self) -> None:
        self.job("Developer", technologies=["Python"])
        self.assertNotIn("SPECIALIZED_TECHNOLOGY", self.detect())
        self.job("JDE CNC", technologies=["JD Edwards EnterpriseOne"])
        self.assertIn("tech:JD Edwards EnterpriseOne", self.detect()["SPECIALIZED_TECHNOLOGY"]["reason_codes"])

    def test_project_implementation(self) -> None:
        self.job("Analyst", description="Lead the implementation of our new HRIS", technologies=["Python"])
        self.assertNotIn("PROJECT_IMPLEMENTATION", self.detect())  # project words, no enterprise tech
        self.job("ERP Analyst", description="Support our SAP S/4HANA migration and go-live",
                 technologies=["SAP S/4HANA"])
        sig = self.detect()["PROJECT_IMPLEMENTATION"]
        self.assertTrue(any("migration" in p for p in sig["evidence"][0]["phrases"]))

    def test_expansion_hiring(self) -> None:
        self.job("Plant Controller", description="Join our team")
        self.assertNotIn("EXPANSION_HIRING", self.detect())
        self.job("IT Lead", description="Support the opening of our new plant in Texas")
        self.assertIn("expansion_language", self.detect()["EXPANSION_HIRING"]["reason_codes"])

    def test_backfill_needs_explicit_evidence(self) -> None:
        self.job("ERP Analyst", description="Great benefits and growth")
        self.assertNotIn("BACKFILL_REPLACEMENT", self.detect())
        self.job("IT Manager", description="This is a backfill due to a retirement.", seniority="manager")
        sig = self.detect()["BACKFILL_REPLACEMENT"]
        self.assertEqual(sig["reason_codes"], ["explicit_backfill_language"])

    def test_backfill_by_repost_pattern(self) -> None:
        self.job("SAP Basis Admin", days_ago=40, status="closed", closed_days_ago=30)
        self.job("SAP Basis Admin", days_ago=10)
        sig = self.detect()["BACKFILL_REPLACEMENT"]
        self.assertEqual(sig["reason_codes"], ["same_title_reposted_within_60d"])
        self.assertLess(sig["confidence"], 0.6)

    def test_leadership_hiring(self) -> None:
        self.job("ERP Analyst")
        self.assertNotIn("LEADERSHIP_HIRING", self.detect())
        self.job("Director of IT", seniority="director", department="leadership")
        self.assertIn("LEADERSHIP_HIRING", self.detect())

    # --- lifecycle -----------------------------------------------------------------

    def test_idempotent_expire_and_dismiss(self) -> None:
        job = self.job("JDE Developer", days_ago=2)
        self.detect()
        self.detect()
        self.assertEqual(self.store.count(self.ctx, "hiring_signals", {"signal_type": "NEW_ROLE"}), 1)
        self.store.update(self.ctx, "job_postings", job["id"], {"status": "closed"})
        self.detect()
        self.assertEqual(self.store.first(self.ctx, "hiring_signals", {"signal_type": "NEW_ROLE"})["status"], "expired")
        self.store.update(self.ctx, "job_postings", job["id"], {"status": "open"})
        sig = self.detect()["NEW_ROLE"]
        self.signals.dismiss(self.ctx, sig["id"])
        self.assertNotIn("NEW_ROLE", self.detect())
        self.assertEqual(self.store.get(self.ctx, "hiring_signals", sig["id"])["status"], "dismissed")

    def test_company_summary_updated(self) -> None:
        self.job("Director of IT", seniority="director", days_ago=1)
        self.detect()
        company = self.store.get(self.ctx, "companies", self.cid)
        self.assertIn("LEADERSHIP_HIRING", company["hiring_signals"])
        self.assertEqual(company["hiring_velocity"], 1.0)


class ScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _ = make_platform()
        self.store = self.platform.store
        self.signals = self.platform.service("signals")

    def test_breakdown_is_explainable_and_sums(self) -> None:
        company = self.store.insert(self.ctx, "companies", {
            "name": "Fit Co", "domain": "fit.com", "website": "https://fit.com", "industry": "Industrial Manufacturing",
            "country": "United States", "employee_count": 500, "technologies": ["JD Edwards EnterpriseOne"]})
        self.store.insert(self.ctx, "campaigns", {"key": "COX", "name": "Cox", "technologies": ["JD Edwards"],
                                                  "status": "active"})
        add_job(self.platform, self.ctx, company["id"], "Director of ERP", seniority="director", days_ago=1)
        self.signals.detect_for_company(self.ctx, company["id"])
        company = self.store.get(self.ctx, "companies", company["id"])
        breakdown = company["score_breakdown"]
        for key in ("account", "hiring", "opportunity"):
            for comp in breakdown[key]:
                self.assertTrue(comp["reason"])
                self.assertLessEqual(comp["value"], comp["weight"])
        self.assertAlmostEqual(company["account_score"], round(sum(c["value"] for c in breakdown["account"]), 1))
        self.assertAlmostEqual(company["hiring_score"],
                               round(min(100, sum(c["value"] for c in breakdown["hiring"])), 1))
        names = {c["component"] for c in breakdown["account"]}
        self.assertEqual(names, {"technology_fit", "industry_fit", "country_fit", "size_fit", "data_completeness"})
        self.assertGreater(company["account_score"], 70)
        self.assertEqual({c["component"] for c in breakdown["hiring"]} <= set(SIGNAL_WEIGHTS), True)

    def test_unfit_company_scores_low(self) -> None:
        company = self.store.insert(self.ctx, "companies", {"name": "Far Co", "country": "France",
                                                            "industry": "Fashion", "employee_count": 5})
        result = self.signals.score_company(self.ctx, company["id"])
        self.assertLess(result["account_score"], 20)
        self.assertEqual(result["hiring_score"], 0)

    def test_contact_score(self) -> None:
        strong = self.signals.score_contact(self.ctx, {"seniority": "vp", "function": "it", "email": "a@b.com",
                                                       "email_status": "VALID", "confidence": 0.9})
        weak = self.signals.score_contact(self.ctx, {"seniority": "", "function": "sales", "email": None})
        self.assertGreater(strong["contact_score"], 85)
        self.assertLess(weak["contact_score"], 30)
        self.assertEqual(len(strong["breakdown"]), 4)

    def test_signals_task_via_worker(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        company = self.store.insert(self.ctx, "companies", {"name": "T Co"})
        add_job(self.platform, self.ctx, company["id"], "VP of IT", seniority="vp", department="leadership")
        task = self.platform.tasks.submit(self.ctx, "signals", {"company_ids": [company["id"]]})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertGreaterEqual(done["result"]["signals"], 1)


if __name__ == "__main__":
    unittest.main()
