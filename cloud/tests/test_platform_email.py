"""Email validation: local statuses, cache (no double spend), paid provider gating, ELV mapping."""

from __future__ import annotations

import unittest

from cloud.intel.email.providers import EmailListVerifyProvider, LocalValidator, ValidationResult
from cloud.intel.email.service import EmailValidationService
from cloud.intel.providers.base import ProviderError
from cloud.tests.test_platform_sources_support import FakeResponse, FakeSession, make_platform

NO_MX = {"nomx.example"}


def resolver(domain):
    if domain == "flaky.example":
        return None
    return domain not in NO_MX


class CountingPaid:
    name, paid = "emaillistverify", True

    def __init__(self, status="VALID") -> None:
        self.calls = []
        self.status = status

    def check(self, email):
        self.calls.append(email)
        return ValidationResult(email, self.status, 95.0, self.name, {"result_code": "ok"})


class LocalValidatorTests(unittest.TestCase):
    def test_statuses(self) -> None:
        v = LocalValidator(resolver=resolver)
        cases = {"not-an-email": "INVALID", "a@mailinator.com": "DISPOSABLE", "x@nomx.example": "INVALID",
                 "info@acme.com": "ROLE", "bob@gmail.com": "FREE_PROVIDER", "jane.doe@acme.com": "UNKNOWN",
                 "jane@flaky.example": "UNKNOWN"}
        for email, status in cases.items():
            self.assertEqual(v.check(email).status, status, email)
        self.assertTrue(v.check("info@acme.com").decisive)
        self.assertFalse(v.check("jane.doe@acme.com").decisive)
        self.assertEqual(v.check("jane.doe@acme.com").checks["mailbox"], "not checked (no SMTP probing)")

    def test_smtp_probing_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LocalValidator(smtp_probe=True)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.ctx, _, self.automation = make_platform()
        self.service = EmailValidationService(self.platform, local=LocalValidator(resolver=resolver))
        self.paid = CountingPaid()
        self.service.paid_factory = lambda ctx: self.paid
        self.platform.override("email", self.service)
        self.ledger = self.platform.service("credits")

    def test_without_allow_paid_nothing_is_spent_and_the_reason_is_recorded(self) -> None:
        [result] = self.service.validate(self.ctx, ["jane.doe@acme.com"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["checks"]["paid_skipped"], "allow_paid was not set")
        self.assertEqual(self.paid.calls, [])

    def test_decisive_local_results_never_reach_the_paid_provider(self) -> None:
        self.ledger.sync(self.ctx, "emaillistverify", 100, source="test")
        results = self.service.validate(self.ctx, ["x@nomx.example", "a@yopmail.com", "hr@acme.com"],
                                        allow_paid=True)
        self.assertEqual([r["status"] for r in results], ["INVALID", "DISPOSABLE", "ROLE"])
        self.assertEqual(self.paid.calls, [])
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 0)

    def test_paid_path_reserves_and_consumes_then_cache_prevents_double_spend(self) -> None:
        self.ledger.sync(self.ctx, "emaillistverify", 100, source="test")
        first = self.service.validate(self.ctx, ["jane.doe@acme.com", "JANE.DOE@acme.com"], allow_paid=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "VALID")
        self.assertEqual(self.paid.calls, ["jane.doe@acme.com"])
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 1)
        again = self.service.validate(self.ctx, ["jane.doe@acme.com"], allow_paid=True)
        self.assertTrue(again[0]["cached"])
        self.assertEqual(self.paid.calls, ["jane.doe@acme.com"])
        self.assertEqual(self.ledger.balance(self.ctx, "emaillistverify")["consumed"], 1)

    def test_emaillistverify_is_used_only_once_verified(self) -> None:
        service = EmailValidationService(self.platform, local=LocalValidator(resolver=resolver))
        registry = self.platform.service("providers")
        registry.set_credentials(self.ctx, "emaillistverify", {"api_key": "elv-key-12345678"})
        self.assertIsNone(service._paid(self.ctx))  # noqa: SLF001 - stored but never checked
        row = registry.connection(self.ctx, "emaillistverify")
        self.platform.store.update(self.ctx, "provider_connections", row["id"], {"status": "verified"})
        self.assertIsNotNone(service._paid(self.ctx))  # noqa: SLF001
        self.platform.store.update(self.ctx, "provider_connections", row["id"], {"status": "error"})
        self.assertIsNone(service._paid(self.ctx))  # noqa: SLF001

    def test_paid_needs_a_known_balance(self) -> None:
        from cloud.intel.providers.credits import CreditError

        with self.assertRaises(CreditError):
            self.service.validate(self.ctx, ["jane.doe@acme.com"], allow_paid=True)
        self.assertEqual(self.paid.calls, [])

    def test_contacts_are_updated_and_the_event_emitted(self) -> None:
        contact = self.platform.store.insert(self.ctx, "contacts", {"full_name": "Jane", "email": "info@acme.com"})
        self.service.validate(self.ctx, ["info@acme.com"])
        updated = self.platform.store.get(self.ctx, "contacts", contact["id"])
        self.assertEqual(updated["email_status"], "ROLE")
        self.assertIsNotNone(updated["email_validated_at"])
        self.assertEqual(self.automation.events[0][0], "email_validated")

    def test_validation_task(self) -> None:
        from cloud.intel.tasks.worker import run_task_inline

        contact = self.platform.store.insert(self.ctx, "contacts", {"full_name": "Bob", "email": "bob@gmail.com"})
        task = self.platform.tasks.submit(self.ctx, "validation", {"contact_ids": [contact["id"]],
                                                                   "emails": ["a@mailinator.com"]})
        done = run_task_inline(self.platform, self.ctx.workspace_id, task["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual(done["result"]["counts"], {"DISPOSABLE": 1, "FREE_PROVIDER": 1})


class EmailListVerifyTests(unittest.TestCase):
    def provider(self, answer, status=200):
        session = FakeSession({"verifyEmail": FakeResponse(status, text=answer),
                               "/api/credits": FakeResponse(200, {"onDemand": {"available": 1000},
                                                                  "subscription": {"available": 234,
                                                                                   "expiresAt": "2026-12-01T00:00:00Z"}})})
        return EmailListVerifyProvider("elv-secret-key", session=session), session

    def test_result_code_mapping(self) -> None:
        for code, status in (("ok", "VALID"), ("ok_for_all", "RISKY"), ("email_disabled", "INVALID"),
                             ("disposable", "DISPOSABLE"), ("unknown", "UNKNOWN"), ("something_new", "UNKNOWN")):
            provider, session = self.provider(code)
            result = provider.check("jane@acme.com")
            self.assertEqual(result.status, status, code)
            self.assertFalse(result.checks["mapping_verified"])
            method, url, kwargs = session.calls[0]
            self.assertTrue(url.startswith("https://api.emaillistverify.com/api/verifyEmail?"))
            self.assertNotIn("elv-secret-key", url)   # the key travels in a header, never the URL
            self.assertEqual(kwargs["headers"]["x-api-key"], "elv-secret-key")

    def test_errors_raise_and_health(self) -> None:
        provider, _ = self.provider("key_not_valid")
        with self.assertRaises(ProviderError):
            provider.check("jane@acme.com")
        self.assertEqual(provider.health()["status"], "configured_unverified")
        health = provider.health(live=True)
        self.assertEqual((health["credits"], health["credits_on_demand"], health["credits_subscription"]),
                         (1234, 1000, 234))
        self.assertEqual(EmailListVerifyProvider("").health()["status"], "not_configured")

    def test_error_credit_raises_and_a_rejected_key_is_reported(self) -> None:
        provider, _ = self.provider("error_credit")
        with self.assertRaises(ProviderError):
            provider.check("jane@acme.com")
        denied = EmailListVerifyProvider("elv-secret-key", session=FakeSession(
            {"/api/credits": FakeResponse(401, {"statusCode": 401, "message": "Invalid api key"})}))
        self.assertEqual(denied.health(live=True)["status"], "error")
        legacy_html = EmailListVerifyProvider("elv-secret-key", session=FakeSession(
            {"/api/credits": FakeResponse(200, text="<!DOCTYPE html><html></html>")}))
        self.assertEqual(legacy_html.health(live=True)["status"], "error")


if __name__ == "__main__":
    unittest.main()
