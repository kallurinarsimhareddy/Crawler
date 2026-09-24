"""The credit ledger (reserve/consume/release, limits, idempotency, isolation) and routing."""

from __future__ import annotations

import unittest
import uuid

from cloud.intel.core.context import Ctx, ValidationError
from cloud.intel.providers.credits import CreditError, SeamlessCreditLedger
from cloud.intel.providers.routing import plan_enrichment
from cloud.tests.test_platform_sources_support import make_platform


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, _ = make_platform(max_credits_per_task=100)
        self.ledger = self.platform.service("credits")

    def test_no_known_balance_means_no_spending(self) -> None:
        with self.assertRaises(CreditError):
            self.ledger.reserve(self.ctx, "seamless", 1, reason="test")

    def test_reserve_consume_release_arithmetic(self) -> None:
        self.ledger.sync(self.ctx, "seamless", remaining=50, source="X-PublicAPI-Credits")
        r1 = self.ledger.reserve(self.ctx, "seamless", 10, reason="research 10")
        self.assertEqual(self.ledger.balance(self.ctx, "seamless")["remaining"], 40)
        self.ledger.consume(self.ctx, r1["id"], 7)
        balance = self.ledger.balance(self.ctx, "seamless")
        self.assertEqual((balance["consumed"], balance["reserved"], balance["remaining"]), (7, 0, 43))
        r2 = self.ledger.reserve(self.ctx, "seamless", 5, reason="more")
        self.ledger.release(self.ctx, r2["id"])
        self.assertEqual(self.ledger.balance(self.ctx, "seamless")["remaining"], 43)
        kinds = [e["entry_type"] for e in self.ledger.entries(self.ctx, provider="seamless", limit=50).rows]
        self.assertEqual(sorted(kinds), sorted(["sync", "reserve", "consume", "release", "reserve", "release"]))
        with self.assertRaises(CreditError):  # nothing left open on r1
            self.ledger.consume(self.ctx, r1["id"], 1)

    def test_limits(self) -> None:
        self.ledger.sync(self.ctx, "zoominfo", 1000, source="contract")
        with self.assertRaises(CreditError):
            self.ledger.reserve(self.ctx, "zoominfo", 101, reason="over the per-task cap")
        self.ledger.set_hard_limit(self.ctx, "zoominfo", 20)
        self.ledger.reserve(self.ctx, "zoominfo", 15, reason="ok")
        with self.assertRaises(CreditError):
            self.ledger.reserve(self.ctx, "zoominfo", 6, reason="over the hard limit")
        self.ledger.sync(self.ctx, "seamless", remaining=3, source="header")
        with self.assertRaises(CreditError):
            self.ledger.reserve(self.ctx, "seamless", 4, reason="more than available")
        with self.assertRaises(CreditError):
            self.ledger.reserve(self.ctx, "seamless", 0, reason="zero")

    def test_idempotent_reservation(self) -> None:
        self.ledger.sync(self.ctx, "seamless", remaining=10, source="x")
        a = self.ledger.reserve(self.ctx, "seamless", 4, reason="r", idempotency_key="task-1:step-1")
        b = self.ledger.reserve(self.ctx, "seamless", 4, reason="r", idempotency_key="task-1:step-1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.ledger.balance(self.ctx, "seamless")["reserved"], 4)

    def test_users_cannot_write_the_ledger_directly(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.store.insert(self.ctx, "credit_ledger", {"provider": "seamless", "entry_type": "grant",
                                                                   "amount": 1e6, "reason": "forged"})
        with self.assertRaises(ValidationError):
            self.platform.store.insert(self.ctx, "credit_accounts", {"provider": "seamless", "total_credits": 1e6})

    def test_grants_are_admin_only_and_balances_are_per_workspace(self) -> None:
        member = str(uuid.uuid4())
        self.platform.store.add_member(self.ctx, member, "member")
        from cloud.intel.core.context import ForbiddenError

        with self.assertRaises(ForbiddenError):
            self.ledger.grant(Ctx(self.ctx.workspace_id, member, "member"), "seamless", 100, reason="self-grant")
        self.ledger.grant(self.ctx, "seamless", 100, reason="top-up")
        other_user = str(uuid.uuid4())
        ws = self.platform.store.create_workspace(other_user, "Other", f"o-{uuid.uuid4().hex[:6]}")
        other = Ctx(ws["id"], other_user, "owner")
        self.assertEqual(self.ledger.balance(other, "seamless")["remaining"], 0)
        with self.assertRaises(CreditError):
            self.ledger.reserve(other, "seamless", 1, reason="spend someone else's")

    def test_provider_scoped_ledger(self) -> None:
        seamless = SeamlessCreditLedger(self.ledger)
        seamless.sync(self.ctx, remaining=5, source="x")
        r = seamless.reserve(self.ctx, 2, reason="r")
        seamless.consume(self.ctx, r["id"], 2)
        self.assertEqual(seamless.balance(self.ctx)["remaining"], 3)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, _ = make_platform()
        store = self.platform.store
        self.full = store.insert(self.ctx, "companies", {"name": "Full Co", "domain": "full.com",
                                                         "website": "https://full.com", "industry": "Manufacturing",
                                                         "employee_range": "201-500", "revenue_range": "$50M-$100M",
                                                         "country": "United States"})
        for title in ("VP Human Resources", "CIO", "CEO"):
            store.insert(self.ctx, "contacts", {"company_id": self.full["id"], "full_name": f"P {title}",
                                                "title": title})
        store.insert(self.ctx, "company_technologies", {"company_id": self.full["id"], "technology": "SAP",
                                                        "source": "job_postings", "observed_at": "2026-09-01"})
        self.empty = store.insert(self.ctx, "companies", {"name": "Empty Co", "website": "https://empty.com",
                                                          "domain": "empty.com"})

    def test_no_paid_steps_for_data_already_present(self) -> None:
        self.platform.service("providers").set_credentials(self.ctx, "seamless", {"api_key": "s" * 20})
        plan = plan_enrichment(self.platform, self.ctx, {"company_ids": [self.full["id"]]})
        self.assertEqual(plan["paid_steps"], 0)
        self.assertEqual(plan["estimated_credits"], {})
        self.assertTrue(all(s["source"] in ("internal", "cache") for s in plan["steps"]))

    def test_paid_steps_are_last_and_conditional(self) -> None:
        self.platform.service("providers").set_credentials(self.ctx, "seamless", {"api_key": "s" * 20})
        plan = plan_enrichment(self.platform, self.ctx, {"company_ids": [self.empty["id"]], "needs": ["contacts"]})
        sources = [s["source"] for s in plan["steps"]]
        self.assertEqual(sources, ["internal", "public_web", "seamless"])
        paid = plan["steps"][-1]
        self.assertTrue(paid["paid"] and paid["conditional"])
        self.assertGreater(plan["estimated_credits"]["seamless"], 0)

    def test_without_a_paid_provider_nothing_paid_is_planned(self) -> None:
        plan = plan_enrichment(self.platform, self.ctx, {"company_ids": [self.empty["id"]]})
        self.assertEqual(plan["paid_steps"], 0)


if __name__ == "__main__":
    unittest.main()
