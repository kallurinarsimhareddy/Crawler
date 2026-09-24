"""The provider-neutral AI layer: Claude via a fake SDK client, REST providers via
fake sessions, the workspace data policy, redaction and usage logging."""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace

from cloud.intel.ai.base import AIError, AIRefused, AIRetryable, AIUnavailable, validate_against_schema
from cloud.intel.ai.claude import ClaudeProvider
from cloud.intel.ai.registry import AIRegistry, redact
from cloud.intel.ai.rest import GeminiProvider, OpenAICompatibleProvider
from cloud.intel.ai.rules import RulesProvider
from cloud.intel.core.context import Ctx
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.tests.test_platform_ai_fakes import claude_reply, fake_claude_client

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
          "additionalProperties": False}


def _rate_limit():
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)


class ClaudeProviderTests(unittest.TestCase):
    def test_json_success_uses_structured_output_and_adaptive_thinking(self) -> None:
        client = fake_claude_client(claude_reply('{"name": "Acme"}'))
        provider = ClaudeProvider(client=client)
        self.assertEqual(provider.complete_json("sys", "prompt", SCHEMA), {"name": "Acme"})
        request = client.messages.requests[0]
        self.assertEqual(request["model"], "claude-opus-5")
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")

    def test_refusal_is_checked_before_content(self) -> None:
        provider = ClaudeProvider(client=fake_claude_client(claude_reply('{"name": "x"}', stop_reason="refusal")))
        with self.assertRaises(AIRefused):
            provider.complete_json("s", "p", SCHEMA)

    def test_rate_limit_is_retryable(self) -> None:
        provider = ClaudeProvider(client=fake_claude_client(_rate_limit()))
        with self.assertRaises(AIRetryable):
            provider.complete_text("s", "p")

    def test_answer_not_matching_schema_is_refused(self) -> None:
        provider = ClaudeProvider(client=fake_claude_client(claude_reply('{"nom": "Acme"}')))
        with self.assertRaises(AIError):
            provider.complete_json("s", "p", SCHEMA)

    def test_missing_key_means_unavailable(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AIUnavailable):
                ClaudeProvider()


class RestProviderTests(unittest.TestCase):
    def _session(self, status: int, payload: dict):
        response = SimpleNamespace(status_code=status, json=lambda: payload, text=str(payload))
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return response

        return SimpleNamespace(post=post, calls=calls)

    def test_gemini_key_goes_in_a_header_not_the_url(self) -> None:
        session = self._session(200, {"candidates": [{"content": {"parts": [{"text": '{"name": "G"}'}]}}]})
        provider = GeminiProvider(api_key="k-secret", session=session)
        self.assertEqual(provider.complete_json("s", "p", SCHEMA), {"name": "G"})
        url, kwargs = session.calls[0]
        self.assertNotIn("k-secret", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "k-secret")

    def test_openai_compatible_requires_https_and_maps_5xx(self) -> None:
        with self.assertRaises(AIUnavailable):
            OpenAICompatibleProvider(api_key="k", base_url="http://evil.example", model="m")
        provider = OpenAICompatibleProvider(api_key="k", base_url="https://llm.example/v1", model="m",
                                            session=self._session(503, {}))
        with self.assertRaises(AIRetryable):
            provider.complete_text("s", "p")


class RegistryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        self.ws = self.store.create_workspace(self.user, "W", "w-ai")
        self.client = fake_claude_client(claude_reply('{"name": "ok"}'))
        self.platform = Platform(self.store, config=PlatformConfig(ai_provider="claude"))
        self.registry = AIRegistry(self.platform, factory=lambda name, model: ClaudeProvider(client=self.client))

    def test_private_data_stays_home_unless_the_workspace_allows_it(self) -> None:
        ctx = Ctx(self.ws["id"], self.user, "owner", ai_external_allowed=False)
        provider = self.registry.for_ctx(ctx, "extraction")
        self.assertIsInstance(provider, RulesProvider)
        with self.assertRaises(AIUnavailable):
            provider.complete_json("s", "p", SCHEMA)
        self.assertEqual(self.client.messages.requests, [])

    def test_allowed_calls_are_redacted_and_logged(self) -> None:
        ctx = Ctx(self.ws["id"], self.user, "owner", ai_external_allowed=True)
        provider = self.registry.for_ctx(ctx, "extraction")
        self.assertTrue(provider.external)
        provider.complete_json("s", "Contact jane.doe@acme.com or +1 (555) 123-4567", SCHEMA)
        sent = self.client.messages.requests[0]["messages"][0]["content"]
        self.assertNotIn("jane.doe@acme.com", sent)
        self.assertIn("[email]", sent)
        self.assertIn("[phone]", sent)
        usage = self.store.list(ctx, "usage_events", {"provider": "ai:claude"}).rows
        self.assertEqual(len(usage), 1)
        self.assertTrue(usage[0]["success"])

    def test_rules_provider_is_the_default(self) -> None:
        platform = Platform(self.store)
        ctx = Ctx(self.ws["id"], self.user, "owner", ai_external_allowed=True)
        self.assertEqual(AIRegistry(platform).for_ctx(ctx, "x").name, "rules")
        self.assertEqual(AIRegistry(platform).describe(ctx)["configured"], "rules")


class HelpersTests(unittest.TestCase):
    def test_redact(self) -> None:
        self.assertEqual(redact("mail a@b.co now"), "mail [email] now")

    def test_schema_validator(self) -> None:
        self.assertEqual(validate_against_schema({"name": "x"}, SCHEMA), [])
        self.assertTrue(validate_against_schema({"name": 3}, SCHEMA))
        self.assertTrue(validate_against_schema({}, SCHEMA))


if __name__ == "__main__":
    unittest.main()
