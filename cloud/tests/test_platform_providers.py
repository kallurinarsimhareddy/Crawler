"""Provider connections (encrypted, private, admin-only) and the ZoomInfo / Seamless connectors."""

from __future__ import annotations

import json
import unittest
import uuid

from cloud.intel.core.context import Ctx, ForbiddenError
from cloud.intel.providers.base import PaidCallRefused, ProviderNotConfigured
from cloud.intel.providers.registry import SecretsUnavailable
from cloud.intel.providers.seamless import SeamlessConnector
from cloud.intel.providers.zoominfo import ZoomInfoConnector, ZoomInfoError
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession, make_platform

SECRET = "zi-client-secret-VERY-PRIVATE-9876"


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, _ = make_platform()
        self.registry = self.platform.service("providers")

    def test_secrets_are_encrypted_and_never_listed(self) -> None:
        row = self.registry.set_credentials(self.ctx, "zoominfo", {"client_id": "cid-12345678",
                                                                   "client_secret": SECRET})
        self.assertEqual(row["status"], "configured")
        self.assertEqual(row["secret_hint"], "…5678")
        self.assertNotIn(SECRET, json.dumps(row, default=str))
        stored = self.platform.store.first(self.ctx, "provider_connections", {"provider": "zoominfo"})
        self.assertNotIn(SECRET, stored["secret_ciphertext"])
        self.assertNotIn(SECRET, json.dumps(self.registry.list_connections(self.ctx), default=str))
        self.assertEqual(self.registry.get_secrets(self.ctx, "zoominfo")["client_secret"], SECRET)
        # the audit trail names the fields, never the values
        audit = self.platform.store.all(self.ctx, "audit_log")
        self.assertNotIn(SECRET, json.dumps(audit, default=str))

    def test_refuses_to_store_without_a_key(self) -> None:
        platform, ctx, _, _ = make_platform(secrets_key=None)
        with self.assertRaises(SecretsUnavailable):
            platform.service("providers").set_credentials(ctx, "seamless", {"api_key": "k" * 20})
        self.assertIsNone(platform.store.first(ctx, "provider_connections", {"provider": "seamless"}))

    def test_admin_only_and_required_fields(self) -> None:
        member = str(uuid.uuid4())
        self.platform.store.add_member(self.ctx, member, "member")
        with self.assertRaises(ForbiddenError):
            self.registry.set_credentials(Ctx(self.ctx.workspace_id, member, "member"), "seamless", {"api_key": "x" * 20})
        from cloud.intel.core.context import ValidationError

        with self.assertRaises(ValidationError):
            self.registry.set_credentials(self.ctx, "zoominfo", {"client_id": "only-half"})

    def test_connections_are_private_to_their_workspace(self) -> None:
        self.registry.set_credentials(self.ctx, "seamless", {"api_key": "seamless-key-abcdef"})
        other_user = str(uuid.uuid4())
        ws = self.platform.store.create_workspace(other_user, "Other", f"other-{uuid.uuid4().hex[:6]}")
        other = Ctx(ws["id"], other_user, "owner")
        self.assertEqual(self.registry.get_secrets(other, "seamless"), {})
        self.assertFalse(self.registry.configured(other, "seamless"))
        statuses = {c["provider"]: c["status"] for c in self.registry.list_connections(other)}
        self.assertEqual(statuses["seamless"], "not_configured")
        self.assertTrue(self.registry.configured(self.ctx, "seamless"))

    def test_verify_seamless_spends_nothing_without_allow_paid(self) -> None:
        self.registry.set_credentials(self.ctx, "seamless", {"api_key": "seamless-key-abcdef"})
        result = self.registry.verify(self.ctx, "seamless")
        self.assertEqual(result["status"], "configured_unverified")
        self.assertIn("costs 1", result["detail"])

    def test_ats_public_is_available_without_credentials(self) -> None:
        statuses = {c["provider"]: c["status"] for c in self.registry.list_connections(self.ctx)}
        self.assertEqual(statuses["ats_public"], "available")
        self.assertEqual(statuses["linkedin"], "not_configured")


def _token_route():
    return {"oauth/v1/token": FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})}


class ZoomInfoTests(unittest.TestCase):
    def connector(self, routes, **settings):
        session = FakeSession({**_token_route(), **routes})
        sleeps = []
        zi = ZoomInfoConnector({"client_id": "id", "client_secret": "secret"}, settings=settings,
                               session=session, sleep=sleeps.append)
        return zi, session, sleeps

    def test_unconfigured_reports_browser_mode_honestly(self) -> None:
        health = ZoomInfoConnector({}, session=FakeSession()).health()
        self.assertEqual(health["status"], "not_configured")
        self.assertIn("operator-attended", health["detail"])
        with self.assertRaises(ProviderNotConfigured):
            ZoomInfoConnector({}, session=FakeSession()).search_companies({"country": "United States"})

    def test_company_search_is_credit_free_and_authenticated(self) -> None:
        zi, session, _ = self.connector({"/companies/search": {"data": [
            {"id": 42, "attributes": {"name": "Acme Mfg", "website": "acme.com", "city": "Tulsa", "state": "OK",
                                      "employeeCount": 300}}]}})
        rows = zi.search_companies({"country": "United States", "techAttributeTagIdList": ["115017"]})
        self.assertEqual(rows[0]["name"], "Acme Mfg")
        self.assertEqual(rows[0]["zoominfo_id"], "42")
        method, url, kwargs = session.calls[-1]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-1")
        self.assertEqual(kwargs["json"]["data"]["type"], "CompanySearchRequest")
        self.assertEqual(zi.credit_consuming_calls, 0)
        self.assertEqual(zi.estimate_cost("search_companies", 100), 0.0)

    def test_retry_after_is_honoured_and_403_is_a_hard_stop(self) -> None:
        zi, _, sleeps = self.connector({"/companies/search": [FakeResponse(429, {}, headers={"Retry-After": "7"}),
                                                              FakeResponse(200, {"data": []})]})
        self.assertEqual(zi.search_companies({}), [])
        self.assertIn(7.0, sleeps)
        zi, session, _ = self.connector({"/companies/search": FakeResponse(403, {})})
        with self.assertRaises(ZoomInfoError):
            zi.search_companies({})
        self.assertEqual(sum(1 for c in session.calls if "/companies/search" in c[1]), 1)

    def test_long_retry_after_is_not_waited_out(self) -> None:
        zi, _, sleeps = self.connector({"/companies/search": FakeResponse(429, {}, headers={"Retry-After": "3600"})})
        with self.assertRaises(ZoomInfoError):
            zi.search_companies({})
        self.assertNotIn(3600.0, sleeps)

    def test_enrichment_requires_allow_paid(self) -> None:
        zi, session, _ = self.connector({})
        with self.assertRaises(PaidCallRefused):
            zi.enrich_company({"companyWebsite": "acme.com"})
        with self.assertRaises(PaidCallRefused):
            zi.enrich_contacts([{"personId": "1"}])
        self.assertEqual(session.calls, [])

    def test_verify_only_requests_a_token(self) -> None:
        zi, session, _ = self.connector({})
        self.assertEqual(zi.verify()["status"], "ok")
        self.assertEqual([c[1] for c in session.calls], ["https://api.zoominfo.com/gtm/oauth/v1/token"])


class SeamlessTests(unittest.TestCase):
    def test_every_credit_call_needs_allow_paid(self) -> None:
        session = FakeSession()
        connector = SeamlessConnector({"api_key": "k"}, session=session, sleep=lambda s: None)
        with self.assertRaises(PaidCallRefused):
            connector.search_contacts({"companyDomain": ["acme.com"]})
        with self.assertRaises(PaidCallRefused):
            connector.enrich_contacts([{"search_result_id": "r1"}])
        self.assertEqual(session.calls, [])

    def test_search_research_poll_and_credit_observation(self) -> None:
        session = FakeSession({
            "/search/contacts": FakeResponse(200, {"data": [
                {"searchResultId": "r1", "name": "Pat Lee", "title": "VP of Human Resources", "company": "Acme",
                 "domain": "acme.com"},
                {"searchResultId": "r2", "name": "Sam Roe", "title": "Sales Associate", "domain": "acme.com"}]},
                headers={"X-PublicAPI-Credits": "990"}),
            "/contacts/research/poll": FakeResponse(200, {"data": [{"requestId": "q1", "status": "done", "contact": {
                "fullName": "Pat Lee", "title": "VP of Human Resources", "email": "pat.lee@acme.com",
                "companyDomain": "acme.com"}}]}, headers={"X-PublicAPI-Credits": "989"}),
            "/contacts/research": FakeResponse(200, {"requestIds": ["q1"]}),
        })
        connector = SeamlessConnector({"api_key": "k"}, session=session, sleep=lambda s: None)
        hits = connector.search_contacts({"companyDomain": ["acme.com"]}, limit=10, allow_paid=True)
        self.assertEqual(hits[0]["function"], "hr")
        chosen = connector.select_targets(hits, 1)
        self.assertEqual([c["full_name"] for c in chosen], ["Pat Lee"])
        enriched = connector.enrich_contacts(chosen, allow_paid=True)
        self.assertEqual(enriched[0]["email"], "pat.lee@acme.com")
        self.assertEqual(connector.credits_remaining, 989)
        self.assertEqual(connector.estimated_spend, 2)  # 1 search block + 1 research record; polling is free
        self.assertEqual(session.calls[0][2]["headers"]["Token"], "k")


if __name__ == "__main__":
    unittest.main()


class PostgresIsolationTests(unittest.TestCase):
    """The same guarantees against real PostgreSQL + RLS."""

    @classmethod
    def setUpClass(cls) -> None:
        from cloud.intel.store.postgres import PostgresStore
        from cloud.tests._pg import fresh_database

        cls.url = fresh_database()
        cls.store = PostgresStore.from_url(cls.url, max_size=4)

    @classmethod
    def tearDownClass(cls) -> None:
        from cloud.tests._pg import drop_database

        cls.store.close()
        drop_database(cls.url)

    def test_connections_and_ledger_are_workspace_private(self) -> None:
        from cryptography.fernet import Fernet

        from cloud.intel.platform import Platform, PlatformConfig

        platform = Platform(self.store, config=PlatformConfig(secrets_key=Fernet.generate_key().decode()))
        a_user, b_user = str(uuid.uuid4()), str(uuid.uuid4())
        a = Ctx(self.store.create_workspace(a_user, "A", f"a-{uuid.uuid4().hex[:6]}")["id"], a_user, "owner")
        b = Ctx(self.store.create_workspace(b_user, "B", f"b-{uuid.uuid4().hex[:6]}")["id"], b_user, "owner")
        registry, ledger = platform.service("providers"), platform.service("credits")
        registry.set_credentials(a, "seamless", {"api_key": KEY_A})
        ledger.sync(a, "seamless", remaining=40, source="test")
        reservation = ledger.reserve(a, "seamless", 5, reason="pg test")
        ledger.consume(a, reservation["id"], 3)
        self.assertEqual(ledger.balance(a, "seamless")["remaining"], 37)
        self.assertEqual(registry.get_secrets(a, "seamless")["api_key"], KEY_A)
        self.assertEqual(registry.get_secrets(b, "seamless"), {})
        self.assertEqual(self.store.count(b, "credit_ledger"), 0)
        self.assertEqual(ledger.balance(b, "seamless")["remaining"], 0)
        from cloud.intel.core.context import ValidationError

        with self.assertRaises(ValidationError):
            self.store.insert(a, "credit_ledger", {"provider": "seamless", "entry_type": "grant", "amount": 1e6,
                                                   "reason": "forged"})


KEY_A = "workspace-a-seamless-key-1111"
