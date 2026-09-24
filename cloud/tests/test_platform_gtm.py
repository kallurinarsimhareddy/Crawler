"""Track I: default campaigns, explainable campaign matching, signal -> opportunity mapping."""

from __future__ import annotations

import unittest
import uuid

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore


class StubCrm:
    """Just enough of the CRM track for campaign mapping."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.calls = []

    def create_opportunity(self, ctx, company_id, title, **kw):
        store = self.platform.store
        pipeline = store.first(ctx, "pipelines", {}) or store.insert(ctx, "pipelines", {"name": "Sales"})
        stage = store.first(ctx, "pipeline_stages", {"pipeline_id": pipeline["id"]}) or store.insert(
            ctx, "pipeline_stages", {"pipeline_id": pipeline["id"], "name": "New"})
        self.calls.append((company_id, title, kw))
        return store.insert(ctx, "opportunities", {
            "company_id": company_id, "title": title, "pipeline_id": pipeline["id"], "stage_id": stage["id"],
            "score": kw.get("score"), "reason": kw.get("reason"), "campaign_id": kw.get("campaign_id"),
            "signal_ids": list(kw.get("signal_ids") or []), "signal_types": list(kw.get("signal_types") or []),
            "evidence": list(kw.get("evidence") or []), "source": kw.get("source")})


def make_platform():
    store = MemoryStore()
    platform = Platform(store)
    user = str(uuid.uuid4())
    ws = store.create_workspace(user, "GTM", f"gtm-{uuid.uuid4().hex[:8]}")
    ctx = Ctx(ws["id"], user, "owner")
    platform.override("crm", StubCrm(platform))
    return platform, ctx


def seed_company(platform, ctx, name, technologies, jobs, signals):
    store = platform.store
    company = store.insert(ctx, "companies", {"name": name, "technologies": technologies,
                                              "industry": "Manufacturing"})
    now = utcnow()
    for i, (title, dept, techs) in enumerate(jobs):
        store.insert(ctx, "job_postings", {
            "company_id": company["id"], "company_name": name, "title": title, "department": dept,
            "technologies": techs, "job_url": f"https://jobs.example/{company['id']}/{i}",
            "url_key": f"jobs.example/{company['id']}/{i}", "first_seen_at": now, "last_seen_at": now,
            "source_kind": "crawler", "source_name": "careercrawler", "is_relevant": True})
    for signal_type in signals:
        store.insert(ctx, "hiring_signals", {"company_id": company["id"], "signal_type": signal_type,
                                             "detected_at": now, "summary": f"{signal_type} at {name}",
                                             "fingerprint": f"{company['id']}:{signal_type}"})
    return company


class CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx = make_platform()
        self.campaigns = self.platform.service("campaigns")
        self.campaigns.ensure_defaults(self.ctx)

    def test_defaults_are_idempotent_private_and_never_sending(self) -> None:
        self.campaigns.ensure_defaults(self.ctx)
        rows = self.platform.store.all(self.ctx, "campaigns")
        self.assertEqual(sorted(r["key"] for r in rows), ["cox-little", "itech-us", "riseit"])
        self.assertTrue(all(r["status"] == "draft" and not r["sending_enabled"] for r in rows))
        # another workspace does not see them, and gets its own copies
        other_user = str(uuid.uuid4())
        other = self.platform.store.create_workspace(other_user, "Other", "other-gtm")
        other_ctx = Ctx(other["id"], other_user, "owner")
        self.assertEqual(self.platform.store.count(other_ctx, "campaigns"), 0)
        self.campaigns.ensure_defaults(other_ctx)
        self.assertEqual(self.platform.store.count(other_ctx, "campaigns"), 3)
        self.assertEqual(self.platform.store.count(self.ctx, "campaigns"), 3)

    def _match(self, company):
        store = self.platform.store
        signals = store.all(self.ctx, "hiring_signals", {"company_id": company["id"]})
        jobs = store.all(self.ctx, "job_postings", {"company_id": company["id"]})
        return self.campaigns.match_campaigns(self.ctx, company, signals, jobs)

    def test_erp_rpg_company_maps_to_cox_little_with_reasons(self) -> None:
        company = seed_company(self.platform, self.ctx, "Midwest Castings", ["AS400", "RPG", "JD Edwards"],
                               [("Senior RPG Developer (iSeries)", "IT", ["RPG", "iSeries"]),
                                ("JD Edwards ERP Implementation Lead", "IT", ["JD Edwards"])],
                               ["PROJECT_IMPLEMENTATION", "SPECIALIZED_TECHNOLOGY"])
        results = self._match(company)
        self.assertEqual(results[0]["campaign"]["key"], "cox-little")
        reasons = " | ".join(results[0]["reasons"])
        self.assertIn("signal PROJECT_IMPLEMENTATION", reasons)
        self.assertIn("technology JD Edwards", reasons)
        self.assertIn("technology RPG", reasons)
        self.assertGreater(results[0]["score"], results[-1]["score"])

    def test_data_cloud_company_maps_to_itech_or_riseit_not_cox_little(self) -> None:
        company = seed_company(self.platform, self.ctx, "Cloudy Data Co", ["AWS", "Snowflake"],
                               [("Data Engineer (Snowflake)", "Data", ["Snowflake", "Python"]),
                                ("QA Automation Engineer", "QA", ["Selenium"]),
                                ("Cloud Migration Engineer", "IT", ["AWS"])],
                               ["MULTIPLE_RELEVANT_ROLES", "HIRING_SPIKE"])
        results = self._match(company)
        self.assertIn(results[0]["campaign"]["key"], ("itech-us", "riseit"))
        cox = next(r for r in results if r["campaign"]["key"] == "cox-little")
        self.assertLess(cox["score"], results[0]["score"])

    def test_word_boundaries_prevent_false_matches(self) -> None:
        company = seed_company(self.platform, self.ctx, "Plain Co", [], [("Warehouse Associate", "Operations", [])],
                               [])
        cox = next(r for r in self._match(company) if r["campaign"]["key"] == "cox-little")
        self.assertNotIn("keyword 'IT'", cox["reasons"])

    def test_mapping_is_a_proposal_unless_create_is_true(self) -> None:
        company = seed_company(self.platform, self.ctx, "Midwest Castings", ["AS400", "RPG"],
                               [("Senior RPG Developer", "IT", ["RPG"])],
                               ["SPECIALIZED_TECHNOLOGY", "LEADERSHIP_HIRING"])
        self.platform.store.insert(self.ctx, "contacts", {"company_id": company["id"], "full_name": "Pat Chief",
                                                          "title": "CIO", "email": "pat@midwest.example"})
        proposal = self.campaigns.map_signal_to_opportunity(self.ctx, company["id"])
        self.assertEqual(proposal["status"], "proposed")
        self.assertFalse(proposal["created"])
        self.assertEqual(self.platform.store.count(self.ctx, "opportunities"), 0)
        self.assertEqual(proposal["campaign"]["key"], "cox-little")
        self.assertEqual([c["full_name"] for c in proposal["target_contacts"]["found"]], ["Pat Chief"])
        self.assertIn("CFO", proposal["target_contacts"]["missing_titles"])
        self.assertTrue(any(e["kind"] == "signal" for e in proposal["evidence"]))

        created = self.campaigns.map_signal_to_opportunity(self.ctx, company["id"], create=True)
        self.assertTrue(created["created"])
        opp = self.platform.store.get(self.ctx, "opportunities", created["opportunity"]["id"])
        self.assertEqual(opp["campaign_id"], proposal["campaign"]["id"])
        self.assertIn("SPECIALIZED_TECHNOLOGY", opp["signal_types"])
        self.assertEqual(self.platform.store.count(self.ctx, "sequence_enrollments"), 0)  # never enrolls
        self.assertEqual(self.platform.store.count(self.ctx, "message_events"), 0)       # never sends

    def test_weak_match_is_not_proposed(self) -> None:
        company = seed_company(self.platform, self.ctx, "Quiet Co", [], [], [])
        proposal = self.campaigns.map_signal_to_opportunity(self.ctx, company["id"], create=True)
        self.assertEqual(proposal["status"], "no_campaign_match")
        self.assertEqual(self.platform.store.count(self.ctx, "opportunities"), 0)


if __name__ == "__main__":
    unittest.main()
