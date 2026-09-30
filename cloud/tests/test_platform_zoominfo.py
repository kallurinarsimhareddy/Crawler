"""ZoomInfo against the published GTM API contract: request bodies, pagination, enrichment limits, status flags,
and the places the platform offers it (Discovery, enrichment routing, research agent). No network."""

from __future__ import annotations

import json
import unittest

from cloud.intel.discovery.service import ZoomInfoCompanySource, run_discovery_task
from cloud.intel.core.context import ValidationError
from cloud.intel.providers.base import PaidCallRefused
from cloud.intel.providers.zoominfo import ZoomInfoConnector
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession, make_platform

SECRET = "zi-secret-NEVER-IN-A-RESPONSE-4242"
TOKEN = {"oauth/v1/token": FakeResponse(200, {"access_token": "opaque-token-value", "expires_in": 3600})}


def _companies(start: int, n: int):
    return [{"type": "Company", "id": str(start + i), "attributes": {"name": f"Co {start + i}"}} for i in range(n)]


def _page(start: int, n: int, number: int, total_pages: int, total_results: int) -> FakeResponse:
    return FakeResponse(200, {"data": _companies(start, n), "meta": {"page": {"number": number, "total": total_pages},
                                                                     "totalResults": total_results}})


def connector(routes, **settings):
    session = FakeSession({**TOKEN, **routes})
    return ZoomInfoConnector({"client_id": "cid", "client_secret": "sec"}, settings=settings, session=session,
                             sleep=lambda s: None), session


def _searches(session, path="/companies/search"):
    return [c for c in session.calls if path in c[1]]


class ContractTests(unittest.TestCase):
    def test_contact_search_uses_contactsearch_and_returns_hints_only(self) -> None:
        zi, session = connector({"/contacts/search": {"data": [{"type": "Contact", "id": "4191419698", "attributes": {
            "firstName": "Jordan", "lastName": "Reyes", "jobTitle": "Director of Marketing", "contactAccuracyScore": 95,
            "hasEmail": True, "hasDirectPhone": False, "company": {"id": 346572700, "name": "Acme Software Inc"}}}],
            "meta": {"page": {"number": 1, "total": 1}, "totalResults": 1}}})
        rows = zi.search_contacts({"companyName": "Acme"}, limit=10)
        _, _, kwargs = _searches(session, "/contacts/search")[0]
        self.assertEqual(kwargs["json"]["data"]["type"], "ContactSearch")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/vnd.api+json")
        self.assertEqual(rows[0]["company_name"], "Acme Software Inc")
        self.assertEqual(rows[0]["zoominfo_company_id"], "346572700")
        self.assertTrue(rows[0]["has_email"])
        self.assertNotIn("email", rows[0])
        self.assertEqual(zi.credit_consuming_calls, 0)

    def test_contact_enrich_body_matches_reference_and_drops_non_matches(self) -> None:
        zi, session = connector({"/contacts/enrich": {"data": [
            {"type": "Contact", "id": "1", "attributes": {"firstName": "A", "lastName": "B", "email": "a@x.com",
                                                          "jobTitle": "CIO"},
             "meta": {"matchStatus": "FULL_MATCH", "input": {"personId": 1}}},
            {"type": "NoMatch", "id": "2", "attributes": {}, "meta": {"matchStatus": "NO_MATCH"}},
            {"type": "Contact", "id": "3", "attributes": {"firstName": "C"}, "meta": {"matchStatus": "OPT_OUT"}}]}})
        rows = zi.enrich_contacts([{"personId": "1"}, {"personId": "2"}, {"personId": "3"}], allow_paid=True)
        body = _searches(session, "/contacts/enrich")[0][2]["json"]
        self.assertEqual(body["data"]["type"], "ContactEnrich")
        self.assertEqual(body["data"]["attributes"]["matchPersonInput"], [{"personId": 1}, {"personId": 2},
                                                                          {"personId": 3}])
        self.assertIn("email", body["data"]["attributes"]["outputFields"])
        self.assertEqual([r["email"] for r in rows], ["a@x.com"])
        self.assertEqual(zi.last_match_statuses, {"FULL_MATCH": 1, "NO_MATCH": 1, "OPT_OUT": 1})

    def test_enrich_refuses_more_than_25_and_never_calls(self) -> None:
        zi, session = connector({})
        with self.assertRaises(ValueError):
            zi.enrich_contacts([{"personId": str(i)} for i in range(26)], allow_paid=True)
        with self.assertRaises(PaidCallRefused):
            zi.enrich_contacts([{"personId": "1"}])
        self.assertEqual(_searches(session, "/enrich"), [])
        self.assertEqual(zi.credit_consuming_calls, 0)

    def test_company_enrich_no_match_is_none(self) -> None:
        zi, session = connector({"/companies/enrich": {"data": [
            {"type": "NoMatch", "id": "", "attributes": {}, "meta": {"matchStatus": "NO_MATCH"}}]}})
        self.assertIsNone(zi.enrich_company({"companyWebsite": "nowhere.example"}, allow_paid=True))
        attrs = _searches(session, "/companies/enrich")[0][2]["json"]["data"]["attributes"]
        self.assertEqual(attrs["matchCompanyInput"], [{"companyWebsite": "nowhere.example"}])
        self.assertIn("website", attrs["outputFields"])


class PaginationTests(unittest.TestCase):
    def test_single_page_when_limit_fits(self) -> None:
        zi, session = connector({"/companies/search": _page(0, 5, 1, 1, 5)})
        self.assertEqual(len(zi.search_companies({"companyName": "ZoomInfo"}, limit=5)), 5)
        params = _searches(session)[0][2]["params"]
        self.assertEqual(params, {"page[number]": 1, "page[size]": 5})
        self.assertEqual(zi.last_search, {"total_results": 5, "total_pages": 1, "pages_fetched": 1})

    def test_pages_until_limit_with_max_page_size_100(self) -> None:
        zi, session = connector({"/companies/search": [_page(0, 100, 1, 5, 450), _page(100, 100, 2, 5, 450),
                                                       _page(200, 100, 3, 5, 450)]})
        rows = zi.search_companies({"state": "Ohio"}, limit=250)
        self.assertEqual(len(rows), 250)
        self.assertEqual([c[2]["params"]["page[number]"] for c in _searches(session)], [1, 2, 3])
        self.assertTrue(all(c[2]["params"]["page[size]"] == 100 for c in _searches(session)))
        self.assertEqual(len({r["zoominfo_id"] for r in rows}), 250)

    def test_stops_at_last_page_and_short_page(self) -> None:
        zi, session = connector({"/companies/search": [_page(0, 100, 1, 2, 130), _page(100, 30, 2, 2, 130)]})
        self.assertEqual(len(zi.search_companies({"state": "Ohio"}, limit=500)), 130)
        self.assertEqual(len(_searches(session)), 2)
        zi, session = connector({"/companies/search": _page(0, 7, 1, 9, 900)})  # short page ends it
        self.assertEqual(len(zi.search_companies({"state": "Ohio"}, limit=50)), 7)
        self.assertEqual(len(_searches(session)), 1)

    def test_ceiling_caps_runaway_limits(self) -> None:
        zi, session = connector({"/companies/search": _page(0, 10, 1, 1, 10)}, max_search_results=10)
        zi.search_companies({"state": "Ohio"}, limit=100000)
        self.assertEqual(_searches(session)[0][2]["params"]["page[size]"], 10)


class PlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, _ = make_platform()
        self.registry = self.platform.service("providers")

    def _connect(self) -> None:
        self.registry.set_credentials(self.ctx, "zoominfo", {"client_id": "0oa-client-id", "client_secret": SECRET})

    def _public(self):
        return [c for c in self.registry.list_connections(self.ctx) if c["provider"] == "zoominfo"][0]

    def test_status_flags_configured_then_verified_and_enabled_without_leaking(self) -> None:
        self.assertFalse(self._public()["configured"])
        self._connect()
        flags = self._public()
        self.assertEqual((flags["configured"], flags["verified"], flags["enabled"]), (True, False, False))
        self.assertEqual(flags["requires"], ["client_id", "client_secret"])
        result = self.registry.verify(self.ctx, "zoominfo", session=FakeSession(dict(TOKEN)))
        self.assertEqual(result["connection_status"], "verified")
        flags = self._public()
        self.assertEqual((flags["configured"], flags["verified"], flags["enabled"]), (True, True, True))
        wire = json.dumps(self.registry.list_connections(self.ctx), default=str)
        self.assertNotIn(SECRET, wire)
        self.assertNotIn("opaque-token-value", wire)
        self.assertNotIn("secret_ciphertext", wire)

    def test_rejected_token_marks_error_not_enabled(self) -> None:
        self._connect()
        self.registry.verify(self.ctx, "zoominfo", session=FakeSession({"oauth/v1/token": FakeResponse(401, {})}))
        flags = self._public()
        self.assertEqual(flags["status"], "error")
        self.assertFalse(flags["enabled"])

    def test_discovery_source_queues_zoominfo_companies_for_review(self) -> None:
        zi, _ = connector({"/companies/search": {"data": [
            {"type": "Company", "id": "344589814", "attributes": {"name": "ZoomInfo", "website": "www.zoominfo.com",
                                                                 "city": "Vancouver", "state": "Washington",
                                                                 "country": "United States"}}]}})
        rows = self.platform.service("discovery").submit_from_source(
            self.ctx, ZoomInfoCompanySource(zi, {"companyName": "ZoomInfo"}), limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "zoominfo")
        self.assertEqual(rows[0]["domain"], "zoominfo.com")
        self.assertEqual(rows[0]["status"], "NEW_COMPANY_DISCOVERY")
        self.assertEqual(self.platform.store.count(self.ctx, "companies", {}), 0)  # nothing written to the CRM

    def test_discovery_task_refuses_when_not_connected(self) -> None:
        with self.assertRaises(ValidationError):
            run_discovery_task(self.platform, self.ctx, {"params": {"source": "zoominfo",
                                                                    "filters": {"companyName": "x"}}}, None)

    def test_enrichment_plan_offers_zoominfo_once_connected(self) -> None:
        from cloud.intel.providers.routing import plan_enrichment

        company = self.platform.store.insert(self.ctx, "companies", {"name": "Acme Mfg",
                                                                      "website": "https://acme.example"})
        needs = {"company_ids": [company["id"]], "needs": ["firmographics"]}
        self.assertNotIn("zoominfo", {s["source"] for s in plan_enrichment(self.platform, self.ctx, needs)["steps"]})
        self._connect()
        steps = plan_enrichment(self.platform, self.ctx, needs)["steps"]
        self.assertIn(("zoominfo", "company_search", False),
                      {(s["source"], s["action"], s["paid"]) for s in steps})

    def test_research_agent_search_estimate_is_credit_free_but_still_approved(self) -> None:
        from cloud.intel.agent.tools import TOOLS

        tool = TOOLS["query_zoominfo"]
        estimate = tool.estimator(self.platform, {"limit": 10}, {})
        self.assertEqual(estimate["credits"], {})
        self.assertIn("research", tool.modes)
        self.assertIn("prospecting", tool.modes)
        self.assertTrue(tool.needs_approval(estimate))


if __name__ == "__main__":
    unittest.main()
