"""Job intelligence: classification, technology detection, ingest, dedupe and the close rule."""

from __future__ import annotations

import unittest

from cloud.intel.jobs.classify import classify
from cloud.intel.jobs.service import canonical_job_url
from cloud.intel.technology.taxonomy import detect
from cloud.tests._platform_intel_helpers import make_platform


def techs(text):
    return [m.technology for m in detect(text)]


class TestClassification(unittest.TestCase):
    def test_seniority_and_department(self) -> None:
        cases = [
            ("Chief Information Officer", "c_level", "leadership"),
            ("VP of Information Technology", "vp", "leadership"),
            ("Director, ERP Applications", "director", "leadership"),
            ("IT Manager", "manager", "it"),
            ("Senior SAP FICO Consultant", "senior", "erp"),
            ("Junior QA Tester", "entry", "qa"),
            ("Software Engineering Intern", "intern", "engineering"),
            ("RPG Developer", "mid", "erp"),
            ("HR Generalist", "mid", "hr"),
            ("Staff Accountant", "mid", "finance"),
            ("Staff Software Engineer", "lead", "engineering"),
        ]
        for title, seniority, department in cases:
            with self.subTest(title=title):
                c = classify(title)
                self.assertEqual((c["seniority"], c["department"]), (seniority, department), c["reasons"])

    def test_workplace_type(self) -> None:
        self.assertEqual(classify("Developer", location="Remote - US")["workplace_type"], "remote")
        self.assertEqual(classify("Developer", "Hybrid schedule, 3 days a week in office")["workplace_type"], "hybrid")
        self.assertEqual(classify("Developer", "This role is on-site at our plant")["workplace_type"], "onsite")
        self.assertEqual(classify("Developer", location="Tulsa, OK")["workplace_type"], "onsite")
        self.assertEqual(classify("Developer")["workplace_type"], "unknown")

    def test_country_state_codes_are_not_countries(self) -> None:
        self.assertEqual(classify("Dev", location="Irvine, CA")["country"], "United States")
        self.assertEqual(classify("Dev", location="Indianapolis, IN")["country"], "United States")
        self.assertEqual(classify("Dev", location="Toronto, Canada")["country"], "Canada")
        self.assertIsNone(classify("Dev", location="Somewhere, XY")["country"])

    def test_experience_certifications_employment(self) -> None:
        c = classify("SAP Consultant", "Requires 5+ years of SAP ABAP. PMP and ITIL preferred. Contract role (W2).")
        self.assertEqual(c["years_experience_min"], 5)
        self.assertIn("PMP", c["certifications"])
        self.assertIn("ITIL", c["certifications"])
        self.assertEqual(c["employment_type"], "contract")
        self.assertIn("SAP ABAP", c["technologies"])


class TestTechnologyDetection(unittest.TestCase):
    def test_iseries_family(self) -> None:
        found = techs("Senior RPG Developer (RPGLE, CLLE, SQLRPGLE) on IBM iSeries AS/400")
        for t in ("RPG", "CL / CLLE", "IBM iSeries", "IBM AS/400"):
            self.assertIn(t, found)

    def test_false_positive_guards(self) -> None:
        self.assertEqual(techs("We love RPGs, tabletop games and LARPing"), [])
        self.assertEqual(techs("CL skills a plus"), [])  # CL needs iSeries context
        self.assertEqual(techs("ECC memory and error correction"), [])  # ECC needs SAP context
        self.assertEqual(techs("JavaScript and React"), ["JavaScript"])  # not Java
        self.assertNotIn("SAP ECC", techs("SAP Business One administrator"))
        self.assertEqual(techs("Plexiglass fabrication"), [])
        self.assertEqual(techs("sage advice"), [])

    def test_erp_products(self) -> None:
        self.assertEqual(techs("JDE E1 9.2 CNC administrator"), ["JD Edwards EnterpriseOne"])
        self.assertIn("JD Edwards World", techs("JD Edwards World A9.1 on AS/400"))
        self.assertEqual(techs("SAP ECC 6.0 to S/4HANA migration"), ["SAP ECC", "SAP S/4HANA"])
        self.assertEqual(techs("Experience with SAP required"), ["SAP (vendor)"])
        self.assertIn("Infor SyteLine", techs("Infor CloudSuite Industrial (SyteLine) analyst"))
        self.assertIn("Dynamics AX", techs("Microsoft Dynamics AX 2012 developer"))
        self.assertIn("Oracle E-Business Suite", techs("Oracle EBS R12 financials"))

    def test_catalogue_aliases_and_exclusions(self) -> None:
        # "Navision" is an acceptable alias in the vendored ZoomInfo catalogue
        self.assertIn("Dynamics NAV", techs("Navision support analyst"))
        self.assertIn("Sage 100", techs("Sage MAS 90 accountant"))


class TestCanonicalUrl(unittest.TestCase):
    def test_tracking_parameters_dropped(self) -> None:
        a = canonical_job_url("https://WWW.Acme.com/jobs/123/?utm_source=x&gh_src=y#apply")
        b = canonical_job_url("https://acme.com/jobs/123")
        self.assertEqual(a, b)
        self.assertNotEqual(canonical_job_url("https://acme.com/jobs?gh_jid=1"),
                            canonical_job_url("https://acme.com/jobs?gh_jid=2"))
        self.assertIsNone(canonical_job_url("javascript:alert(1)"))


class TestIngest(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, self.automation = make_platform()
        self.store = self.platform.store
        self.company = self.store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.com"})
        self.store.insert(self.ctx, "campaigns", {"key": "COX", "name": "Cox", "focus_keywords": ["ERP"],
                                                  "technologies": ["JD Edwards"], "status": "active"})
        self.jobs = self.platform.service("jobs")

    def posting(self, n, title="JDE Developer", **extra):
        return {"title": title, "job_url": f"https://acme.com/jobs/{n}?utm_source=li", "location": "Tulsa, OK",
                "description": "Support JD Edwards EnterpriseOne", **extra}

    def test_insert_link_classify_relevance_and_technology(self) -> None:
        stats = self.jobs.ingest_postings(self.ctx, [self.posting(1), self.posting(2, "Receptionist", description="")],
                                          source_kind="crawler", source_name="careercrawler",
                                          company_id=self.company["id"])
        self.assertEqual((stats["inserted"], stats["relevant"], stats["linked"]), (2, 1, 2))
        job = self.store.first(self.ctx, "job_postings", {"title": "JDE Developer"})
        self.assertTrue(job["is_relevant"])
        self.assertEqual(job["campaign_keys"], ["COX"])
        self.assertIn("JD Edwards EnterpriseOne", job["technologies"])
        company = self.store.get(self.ctx, "companies", self.company["id"])
        self.assertEqual(company["hiring_count"], 2)
        self.assertIn("JD Edwards EnterpriseOne", company["technologies"])
        tech = self.store.first(self.ctx, "company_technologies", {"technology": "JD Edwards EnterpriseOne"})
        self.assertEqual(tech["source"], "job_posting")
        self.assertTrue(tech["evidence_url"].startswith("https://acme.com/jobs/"))
        self.assertEqual(self.store.count(self.ctx, "change_events", {"change_type": "new_job"}), 2)
        self.assertIn("job_posted", [e[0] for e in self.automation.events])

    def test_dedupe_by_canonical_url_and_domain_linking(self) -> None:
        self.jobs.ingest_postings(self.ctx, [self.posting(1, website="https://acme.com")], source_kind="crawler",
                                  source_name="careercrawler")
        stats = self.jobs.ingest_postings(self.ctx, [{**self.posting(1), "job_url": "https://www.acme.com/jobs/1"}],
                                          source_kind="crawler", source_name="careercrawler")
        self.assertEqual((stats["inserted"], stats["updated"]), (0, 1))
        self.assertEqual(self.store.count(self.ctx, "job_postings"), 1)
        self.assertEqual(self.store.first(self.ctx, "job_postings", {})["company_id"], self.company["id"])

    def test_rejects_bad_postings(self) -> None:
        stats = self.jobs.ingest_postings(self.ctx, [{"title": "", "job_url": "https://x.com/1"},
                                                     {"title": "Dev", "job_url": "ftp://x"}],
                                          source_kind="manual", source_name="manual")
        self.assertEqual(stats["rejected"], 2)

    def test_close_rule_only_after_a_successful_crawl(self) -> None:
        cid = self.company["id"]
        self.jobs.ingest_postings(self.ctx, [self.posting(1), self.posting(2)], source_kind="crawler",
                                  source_name="careercrawler", company_id=cid, crawled_company_ids={cid})
        # a FAILED crawl: nothing seen, company not in crawled set -> nothing closes
        stats = self.jobs.ingest_postings(self.ctx, [], source_kind="crawler", source_name="careercrawler",
                                          company_id=cid, crawled_company_ids=set())
        self.assertEqual(stats["closed"], 0)
        self.assertEqual(self.store.count(self.ctx, "job_postings", {"status": "open"}), 2)
        # a successful crawl that sees only job 1 closes job 2
        stats = self.jobs.ingest_postings(self.ctx, [self.posting(1)], source_kind="crawler",
                                          source_name="careercrawler", company_id=cid, crawled_company_ids={cid})
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(self.store.get(self.ctx, "companies", cid)["hiring_count"], 1)
        self.assertEqual(self.store.count(self.ctx, "change_events", {"change_type": "job_closed"}), 1)
        # it comes back: reopened, not duplicated
        stats = self.jobs.ingest_postings(self.ctx, [self.posting(1), self.posting(2)], source_kind="crawler",
                                          source_name="careercrawler", company_id=cid, crawled_company_ids={cid})
        self.assertEqual(stats["reopened"], 1)
        self.assertEqual(self.store.count(self.ctx, "job_postings"), 2)

    def test_empty_successful_crawl_closes_everything(self) -> None:
        cid = self.company["id"]
        self.jobs.ingest_postings(self.ctx, [self.posting(1)], source_kind="crawler", source_name="careercrawler",
                                  company_id=cid, crawled_company_ids={cid})
        stats = self.jobs.ingest_postings(self.ctx, [], source_kind="crawler", source_name="careercrawler",
                                          company_id=cid, crawled_company_ids={cid})
        self.assertEqual(stats["closed"], 1)


if __name__ == "__main__":
    unittest.main()
