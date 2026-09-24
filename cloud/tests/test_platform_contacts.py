"""Contact intelligence: gap analysis, public-web discovery (no guessed emails), paid gating."""

from __future__ import annotations

import unittest

from cloud.intel.email.providers import LocalValidator
from cloud.intel.email.service import EmailValidationService
from cloud.intel.providers.contacts import contact_functions
from cloud.tests.test_platform_sources_support import HOME, LEADERSHIP, FakeResponse, fetcher, make_platform


class ContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, self.automation = make_platform()
        self.platform.override("email", EmailValidationService(
            self.platform, local=LocalValidator(resolver=lambda d: True)))
        self.service = self.platform.service("contacts")
        self.service.fetcher, self.session = fetcher({
            "www.acme.com/about/leadership": FakeResponse(200, text=LEADERSHIP, headers={"Content-Type": "text/html"}),
            "www.acme.com": FakeResponse(200, text=HOME, headers={"Content-Type": "text/html"}),
        })
        store = self.platform.store
        self.company = store.insert(self.ctx, "companies", {"name": "Acme", "domain": "acme.com",
                                                            "website": "https://www.acme.com"})

    def test_functions(self) -> None:
        self.assertEqual(contact_functions("Chief Technology Officer"), ["it", "cto", "c_level"])
        self.assertIn("recruiting", contact_functions("Senior Technical Recruiter"))
        self.assertEqual(contact_functions(""), [])

    def test_gap_analysis_statuses(self) -> None:
        store = self.platform.store
        store.insert(self.ctx, "contacts", {"company_id": self.company["id"], "full_name": "Hana",
                                            "title": "HR Director", "email": "hana@acme.com",
                                            "email_status": "VALID"})
        store.insert(self.ctx, "contacts", {"company_id": self.company["id"], "full_name": "Ian",
                                            "title": "IT Manager", "email": "ian@acme.com"})
        gap = self.service.gap_analysis(self.ctx, self.company["id"], ["hr", "it", "cio", "executive"])
        statuses = {f: v["status"] for f, v in gap["functions"].items()}
        self.assertEqual(statuses, {"hr": "FOUND", "it": "NEEDS_VERIFICATION", "cio": "MISSING",
                                    "c_level": "MISSING"})
        self.assertEqual(gap["summary"], {"FOUND": 1, "MISSING": 2, "NEEDS_VERIFICATION": 1})

    def test_public_web_records_published_people_only(self) -> None:
        result = self.service.find_contacts(self.ctx, [self.company["id"]], functions=["it", "cio"])
        contacts = {c["full_name"]: c for c in self.platform.store.all(self.ctx, "contacts")}
        self.assertEqual(set(contacts), {"Jane Smith", "Carol White"})
        self.assertEqual(contacts["Jane Smith"]["email"], "jane.smith@acme.com")
        self.assertEqual(contacts["Jane Smith"]["title"], "Chief Information Officer")
        # Carol's address was never published, so none is constructed for her.
        self.assertIsNone(contacts["Carol White"]["email"])
        # the footer role address is not attributed to anyone, and the off-site team page is not read
        self.assertFalse(any(c.get("email") == "info@acme.com" for c in contacts.values()))
        self.assertFalse(any("elsewhere.com" in call[1] for call in self.session.calls))
        prov = self.platform.store.all(self.ctx, "source_records", {"entity_id": contacts["Jane Smith"]["id"]})
        self.assertEqual(prov[0]["source_kind"], "public_web")
        self.assertEqual(prov[0]["source_ref"], "https://www.acme.com/about/leadership")
        self.assertEqual(result["added"], 2)
        # the published email was validated (locally) and new contacts announced
        self.assertEqual(self.platform.store.get(self.ctx, "contacts", contacts["Jane Smith"]["id"])["email_status"],
                         "UNKNOWN")
        self.assertIn("new_contact", [e[0] for e in self.automation.events])

    def test_paid_needed_is_reported_not_silently_skipped(self) -> None:
        self.platform.service("providers").set_credentials(self.ctx, "seamless", {"api_key": "s" * 20})
        result = self.service.find_contacts(self.ctx, [self.company["id"]], functions=["hr"])
        report = result["companies"][0]
        self.assertEqual(result["paid_needed"], [self.company["id"]])
        self.assertIn("allow_paid", report["paid_needed"]["reason"])
        self.assertEqual(self.platform.service("credits").balance(self.ctx, "seamless")["consumed"], 0)

    def test_paid_enrichment_goes_through_the_ledger(self) -> None:
        from cloud.tests.test_platform_sources_support import FakeSession
        from cloud.intel.providers.seamless import SeamlessConnector

        seamless_session = FakeSession({
            "/search/contacts": FakeResponse(200, {"data": [{"searchResultId": "r1", "name": "Pat Lee",
                                                             "title": "VP Human Resources", "domain": "acme.com"}]}),
            "/contacts/research/poll": FakeResponse(200, {"data": [{"requestId": "q1", "status": "done", "contact": {
                "fullName": "Pat Lee", "title": "VP Human Resources", "email": "pat.lee@acme.com"}}]},
                headers={"X-PublicAPI-Credits": "48"}),
            "/contacts/research": FakeResponse(200, {"requestIds": ["q1"]}),
        })
        registry = self.platform.service("providers")
        registry.set_credentials(self.ctx, "seamless", {"api_key": "s" * 20})
        original = registry.enrichment
        registry.enrichment = lambda ctx, name, session=None: SeamlessConnector(
            {"api_key": "s" * 20}, session=seamless_session, sleep=lambda s: None)
        try:
            ledger = self.platform.service("credits")
            ledger.sync(self.ctx, "seamless", remaining=50, source="test")
            result = self.service.find_contacts(self.ctx, [self.company["id"]], functions=["hr"], allow_paid=True,
                                                providers=["seamless"])
        finally:
            registry.enrichment = original
        pat = self.platform.store.first(self.ctx, "contacts", {"full_name": "Pat Lee"})
        self.assertEqual(pat["email"], "pat.lee@acme.com")
        self.assertEqual(pat["source"], "seamless")
        balance = ledger.balance(self.ctx, "seamless")
        self.assertEqual(balance["reserved"], 0)
        self.assertEqual(balance["remaining"], 48)  # re-synced from the provider's own header
        gap = result["companies"][0]["gap"]["functions"]["hr"]
        self.assertIn(gap["status"], ("FOUND", "NEEDS_VERIFICATION"))

    def test_enrichment_task(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        task = self.platform.tasks.submit(self.ctx, "enrichment", {"company_ids": [self.company["id"]],
                                                                   "functions": ["it"]})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual(done["result"]["added"], 2)


if __name__ == "__main__":
    unittest.main()
