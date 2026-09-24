"""Track A: the CRM service and company identity resolution (offline + PostgreSQL)."""

from __future__ import annotations

import unittest
import uuid

from cloud.intel.core.context import ConflictError, Ctx, ValidationError
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.tests._pg import drop_database, fresh_database


def _platform(store=None):
    store = store or MemoryStore()
    user = str(uuid.uuid4())
    ws = store.create_workspace(user, "Acme GTM", f"acme-{uuid.uuid4().hex[:8]}")
    return Platform(store), Ctx(ws["id"], user, "owner")


class CrmBehaviour:
    """Shared by the MemoryStore and PostgresStore runs."""

    def make(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def setUp(self) -> None:
        self.platform, self.ctx = self.make()
        self.crm = self.platform.service("crm")
        self.store = self.platform.store

    def upsert(self, values, **kw):
        return self.crm.upsert_company(self.ctx, values, source_kind=kw.pop("source_kind", "manual"),
                                       source_name=kw.pop("source_name", "test"), **kw)

    def test_same_domain_merges_and_fills_only_empty_fields(self) -> None:
        first = self.upsert({"name": "Acme Corp", "website": "https://www.acme.com/about", "industry": "Manufacturing"})
        self.assertTrue(first["created"])
        company = first["company"]
        self.assertEqual(company["domain"], "acme.com")
        self.assertEqual(company["normalized_name"], "acme corp")
        second = self.upsert({"name": "ACME Corporation", "domain": "acme.com", "industry": "Aerospace",
                              "city": "Tulsa"}, source_kind="import", source_name="list.csv")
        self.assertFalse(second["created"])
        self.assertEqual(second["company"]["id"], company["id"])
        merged = second["company"]
        self.assertEqual(merged["industry"], "Manufacturing")          # not overwritten
        self.assertEqual(merged["city"], "Tulsa")                       # empty field filled
        self.assertIn("ACME Corporation", merged["aliases"])            # other name kept as alias
        self.assertEqual(merged["source_count"], 2)
        records = self.store.all(self.ctx, "source_records", {"entity_id": company["id"]})
        self.assertEqual({r["source_kind"] for r in records}, {"manual", "import"})
        conflicts = [r["normalized"].get("conflicts") for r in records if r["source_kind"] == "import"][0]
        self.assertEqual(conflicts["industry"], {"kept": "Manufacturing", "incoming": "Aerospace"})

    def test_name_alone_never_merges(self) -> None:
        self.upsert({"name": "Acme Inc"})
        result = self.upsert({"name": "Acme LLC"})
        self.assertTrue(result["needs_review"])
        self.assertIsNone(result["company"])
        self.assertEqual(self.store.count(self.ctx, "companies"), 1)
        same = self.upsert({"name": "Acme Inc"})
        self.assertTrue(same["needs_review"])  # same name, no domain: a person decides

    def test_conflicting_domains_need_review(self) -> None:
        self.upsert({"name": "Globex Inc", "domain": "globex.com"})
        result = self.upsert({"name": "Globex Inc", "domain": "globex.co.uk"})
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["match"]["outcome"], "AMBIGUOUS")
        forced = self.upsert({"name": "Globex Inc", "domain": "globex.co.uk"}, create_if_ambiguous=True)
        self.assertTrue(forced["created"])

    def test_manual_create_refuses_likely_duplicates_unless_forced(self) -> None:
        self.crm.create_company(self.ctx, {"name": "Initech"})
        with self.assertRaises(ConflictError):
            self.crm.create_company(self.ctx, {"name": "Initech"})
        self.assertTrue(self.crm.create_company(self.ctx, {"name": "Initech"}, force=True)["created"])

    def test_contacts_dedupe_and_link_by_email_domain(self) -> None:
        company = self.upsert({"name": "Umbrella", "domain": "umbrella.com"})["company"]
        a = self.crm.upsert_contact(self.ctx, {"full_name": "Jane Doe", "email": "Jane.Doe@Umbrella.com",
                                               "title": "VP of Information Technology"},
                                    source_kind="manual", source_name="t")
        self.assertTrue(a["created"])
        contact = a["contact"]
        self.assertEqual(contact["email"], "jane.doe@umbrella.com")
        self.assertEqual(contact["company_id"], company["id"])
        self.assertEqual((contact["first_name"], contact["last_name"]), ("Jane", "Doe"))
        self.assertEqual(contact["function"], "it")
        b = self.crm.upsert_contact(self.ctx, {"full_name": "Jane D.", "email": "jane.doe@umbrella.com",
                                               "phone": "+1 555 0100"}, source_kind="import", source_name="x.csv")
        self.assertFalse(b["created"])
        self.assertEqual(b["contact"]["phone"], "+1 555 0100")
        free = self.crm.upsert_contact(self.ctx, {"full_name": "Bob", "email": "bob@gmail.com"},
                                       source_kind="manual", source_name="t")
        self.assertIsNone(free["contact"]["company_id"])
        with self.assertRaises(ValidationError):
            self.crm.upsert_contact(self.ctx, {"full_name": "X", "email": "not-an-email"},
                                    source_kind="manual", source_name="t")

    def test_pipeline_defaults_are_idempotent_and_stages_move(self) -> None:
        self.crm.ensure_defaults(self.ctx)
        self.crm.ensure_defaults(self.ctx)
        pipelines = self.store.all(self.ctx, "pipelines")
        self.assertEqual(len(pipelines), 1)
        stages = self.crm.stages(self.ctx, pipelines[0]["id"])
        self.assertEqual([s["name"] for s in stages],
                         ["New", "Researching", "Qualified", "Contacted", "Engaged", "Opportunity", "Proposal",
                          "Won", "Lost"])
        company = self.upsert({"name": "Hooli", "domain": "hooli.com"})["company"]
        opp = self.crm.create_opportunity(self.ctx, company["id"], "SAP S/4 staffing", signal_types=["HIRING_SPIKE"],
                                          score=72.5, reason="5 SAP roles in 30 days")
        self.assertEqual(opp["stage_id"], stages[0]["id"])
        self.assertEqual(opp["status"], "open")
        won = self.crm.move_stage(self.ctx, opp["id"], stages[7]["id"])
        self.assertEqual(won["status"], "won")
        kinds = [a["kind"] for a in self.store.all(self.ctx, "activities", {"opportunity_id": opp["id"]})]
        self.assertIn("stage_changed", kinds)
        other = self.crm.create_pipeline(self.ctx, "Partners", ["Intro", "Won", "Lost"])
        with self.assertRaises(ValidationError):
            self.crm.move_stage(self.ctx, opp["id"], self.crm.stages(self.ctx, other["id"])[0]["id"])

    def test_lists_segments_custom_fields(self) -> None:
        a = self.upsert({"name": "A1", "domain": "a1.com", "industry": "Manufacturing"})["company"]
        b = self.upsert({"name": "B2", "domain": "b2.com", "industry": "Retail"})["company"]
        target = self.crm.create_list(self.ctx, "ERP targets", "companies")
        self.assertEqual(self.crm.add_to_list(self.ctx, target["id"], "companies", [a["id"], b["id"], a["id"]]), 2)
        self.assertEqual(self.crm.add_to_list(self.ctx, target["id"], "companies", [a["id"]]), 0)
        self.assertEqual(self.store.get(self.ctx, "lists", target["id"])["member_count"], 2)
        self.crm.remove_from_list(self.ctx, target["id"], [b["id"]])
        self.assertEqual(self.store.get(self.ctx, "lists", target["id"])["member_count"], 1)
        segment = self.crm.create_segment(self.ctx, "Mfg", "companies", {"industry": "Manufacturing"})
        self.assertEqual([r["id"] for r in self.crm.evaluate_segment(self.ctx, segment["id"]).rows], [a["id"]])
        with self.assertRaises(ValidationError):
            self.crm.create_segment(self.ctx, "Bad", "companies", {"no_such_field": 1})
        self.store.insert(self.ctx, "custom_field_defs", {"entity_type": "companies", "key": "erp_vendor",
                                                          "label": "ERP vendor", "field_type": "select",
                                                          "options": ["SAP", "Oracle"]})
        updated = self.crm.update_company(self.ctx, a["id"], {"custom_fields": {"erp_vendor": "SAP"}})
        self.assertEqual(updated["custom_fields"], {"erp_vendor": "SAP"})
        with self.assertRaises(ValidationError):
            self.crm.update_company(self.ctx, a["id"], {"custom_fields": {"erp_vendor": "Infor"}})
        with self.assertRaises(ValidationError):
            self.crm.update_company(self.ctx, a["id"], {"custom_fields": {"undefined": "x"}})

    def test_relationships_and_merge(self) -> None:
        parent = self.upsert({"name": "Parent Co", "domain": "parent.com"})["company"]
        child = self.upsert({"name": "Child Co", "domain": "child.com"})["company"]
        self.crm.add_relationship(self.ctx, child["id"], parent["id"], "subsidiary")
        self.assertEqual(self.store.get(self.ctx, "companies", child["id"])["parent_company_id"], parent["id"])
        dup = self.upsert({"name": "Parent Company Ltd", "city": "Austin"}, create_if_ambiguous=True)["company"]
        self.crm.upsert_contact(self.ctx, {"full_name": "Ann Lee", "company_id": dup["id"]},
                                source_kind="manual", source_name="t")
        result = self.crm.merge_companies(self.ctx, parent["id"], [dup["id"]])
        self.assertEqual(result["moved"].get("contacts"), 1)
        self.assertEqual(result["company"]["city"], "Austin")
        loser = self.store.get(self.ctx, "companies", dup["id"])
        self.assertEqual((loser["status"], loser["merged_into_id"]), ("merged", parent["id"]))
        with self.assertRaises(ConflictError):
            self.crm.merge_companies(self.ctx, parent["id"], [dup["id"]])
        timeline = self.crm.company_timeline(self.ctx, parent["id"])
        self.assertTrue(any(item["kind"] == "company_merged" for item in timeline))
        self.assertTrue(self.store.count(self.ctx, "audit_log", {"action": "companies.merge"}) >= 1)


class TestCrmMemory(CrmBehaviour, unittest.TestCase):
    def make(self):
        return _platform()


class TestCrmPostgres(CrmBehaviour, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from cloud.intel.store.postgres import PostgresStore

        cls.url = fresh_database()
        cls.pg = PostgresStore.from_url(cls.url, max_size=4)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pg.close()
        drop_database(cls.url)

    def make(self):
        return _platform(self.pg)


class TestResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx = _platform()
        self.resolver = self.platform.service("dedupe")
        crm = self.platform.service("crm")
        self.acme = crm.upsert_company(self.ctx, {"name": "Acme Inc", "domain": "acme.com", "aliases": ["ACME Group"]},
                                       source_kind="manual", source_name="t")["company"]

    def test_outcomes(self) -> None:
        self.assertIn(self.resolver.resolve(self.ctx, {"name": "Whatever", "domain": "acme.com"})["outcome"],
                      ("EXACT", "STRONG"))
        self.assertEqual(self.resolver.resolve(self.ctx, {"name": "Totally New"})["outcome"], "NONE")
        self.assertEqual(self.resolver.resolve(self.ctx, {"name": "Acme LLC"})["outcome"], "AMBIGUOUS")
        self.assertEqual(self.resolver.resolve(self.ctx, {})["outcome"], "NONE")
        alias = self.resolver.resolve(self.ctx, {"name": "ACME Group"})
        self.assertIn(self.acme["id"], alias["candidates"])

    def test_merged_companies_are_not_candidates(self) -> None:
        self.platform.store.update(self.ctx, "companies", self.acme["id"], {"status": "merged", "domain": None})
        self.assertEqual(self.resolver.resolve(self.ctx, {"name": "X", "domain": "acme.com"})["outcome"], "NONE")


if __name__ == "__main__":
    unittest.main()
