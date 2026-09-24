"""Choosing an AI provider for one call, under the workspace's data policy.

``AIRegistry(platform).for_ctx(ctx, purpose)`` returns:

* the configured external provider (``CAREERCLOUD_AI_PROVIDER`` = ``claude``,
  ``gemini`` or ``openai_compatible``) **only** when the workspace has set
  ``ai_external_allowed`` — private workspace data never leaves otherwise;
* :class:`~cloud.intel.ai.rules.RulesProvider` in every other case (not
  configured, not allowed, SDK or key missing), which makes callers use their
  deterministic path.

The provider handed back is wrapped so that every call

* has emails and phone numbers redacted from the prompt unless the purpose is
  in :data:`PII_PURPOSES` (e.g. personalisation, which needs a name);
* records a ``usage_events`` row (provider, purpose, success, latency) in the
  workspace, via the system scope.

**Per-workspace choice.** A workspace may pick its own provider and model in
``settings.ai`` (``{"provider": "gemini", "model": "…"}``) and may list
``fallbacks`` (``[{"provider": "claude"}]``). Fallbacks are used **only** when
listed, and only when the primary is unavailable or failing transiently — never
after a refusal. API keys stay in server environment variables; nothing here is
exposed to the browser.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional

from cloud.intel.ai.base import AIError, AIProvider, AIRefused, AIRetryable, AIUnavailable
from cloud.intel.ai.rules import RulesProvider
from cloud.intel.core.context import Ctx

__all__ = ["AIRegistry", "PII_PURPOSES", "redact"]

log = logging.getLogger(__name__)

#: Purposes allowed to see contact details in the prompt.
PII_PURPOSES = frozenset({"personalization"})

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


def redact(text: str) -> str:
    """Replace email addresses and phone numbers with placeholders."""
    text = _EMAIL.sub("[email]", text or "")
    return _PHONE.sub("[phone]", text)


def _build(name: str, model: Optional[str]) -> AIProvider:
    if name == "claude":
        from cloud.intel.ai.claude import ClaudeProvider

        return ClaudeProvider(model=model)
    if name == "gemini":
        from cloud.intel.ai.rest import GeminiProvider

        return GeminiProvider(model=model)
    if name in ("openai_compatible", "openai"):
        from cloud.intel.ai.rest import OpenAICompatibleProvider

        return OpenAICompatibleProvider(model=model)
    raise AIUnavailable(f"unknown AI provider {name!r}")


class _Tracked(AIProvider):
    """Redacts prompts and records usage around a real provider."""

    def __init__(self, inner: AIProvider, registry: "AIRegistry", ctx: Ctx, purpose: str) -> None:
        self.inner, self.registry, self.ctx, self.purpose = inner, registry, ctx, purpose
        self.name, self.external, self.model = inner.name, inner.external, inner.model

    def _prep(self, text: str) -> str:
        return text if self.purpose in PII_PURPOSES else redact(text)

    def _run(self, operation: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        error: Optional[str] = None
        try:
            return fn()
        except AIError as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.registry.record(self.ctx, self.name, f"{operation}:{self.purpose}", error,
                                 (time.monotonic() - started) * 1000)

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        return self._run("json", lambda: self.inner.complete_json(system, self._prep(prompt), schema,
                                                                  max_tokens=max_tokens))

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self._run("text", lambda: self.inner.complete_text(system, self._prep(prompt), max_tokens=max_tokens))

    def describe(self) -> Dict[str, Any]:
        return self.inner.describe()


class FallbackProvider(AIProvider):
    """Try providers in the order the workspace listed them; stop at a refusal."""

    def __init__(self, providers: list) -> None:
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.external = any(p.external for p in providers)
        self.model = providers[0].model

    def _try(self, call: Callable[[AIProvider], Any]) -> Any:
        last: Optional[Exception] = None
        for provider in self.providers:
            try:
                return call(provider)
            except AIRefused:
                raise
            except (AIUnavailable, AIRetryable) as error:
                log.warning("AI provider %s failed (%s); trying the next configured fallback", provider.name, error)
                last = error
        raise last or AIUnavailable("no AI provider available")

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        return self._try(lambda p: p.complete_json(system, prompt, schema, max_tokens=max_tokens))

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self._try(lambda p: p.complete_text(system, prompt, max_tokens=max_tokens))

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "external": self.external, "chain": [p.describe() for p in self.providers]}


class AIRegistry:
    def __init__(self, platform: Any, *, factory: Optional[Callable[[str, Optional[str]], AIProvider]] = None) -> None:
        self.platform = platform
        self._factory = factory or _build
        self._cache: Dict[str, AIProvider] = {}

    @property
    def configured(self) -> str:
        return (self.platform.config.ai_provider or "rules").lower()

    def _named(self, name: str, model: Optional[str]) -> AIProvider:
        name = (name or "rules").lower()
        if name == "rules":
            return RulesProvider()
        key = f"{name}:{model or ''}"
        if key not in self._cache:
            try:
                self._cache[key] = self._factory(name, model)
            except AIUnavailable as error:
                log.warning("AI provider %s unavailable: %s", name, error)
                return RulesProvider(str(error))
        return self._cache[key]

    def _provider(self) -> AIProvider:
        return self._named(self.configured, self.platform.config.ai_model)

    def workspace_config(self, ctx: Ctx) -> Dict[str, Any]:
        lookup = getattr(self.platform.store, "system_membership", None)
        info = lookup(ctx.workspace_id) if callable(lookup) else None
        ai = ((info or {}).get("settings") or {}).get("ai") or {}
        return {"provider": ai.get("provider"), "model": ai.get("model"), "fallbacks": list(ai.get("fallbacks") or [])}

    def for_ctx(self, ctx: Ctx, purpose: str) -> AIProvider:
        config = self.workspace_config(ctx)
        if config["provider"]:
            provider = self._named(config["provider"], config["model"] or self.platform.config.ai_model)
        else:
            provider = self._provider()
        chain = [provider] + [self._named(f.get("provider", ""), f.get("model")) for f in config["fallbacks"]
                              if isinstance(f, Mapping) and f.get("provider")]
        chain = [p for p in chain if p.external] or [provider]
        if len(chain) > 1:
            provider = FallbackProvider(chain)
        if provider.external and not ctx.ai_external_allowed:
            return RulesProvider("this workspace has not allowed its data to be sent to external AI providers")
        if not provider.external:
            return provider
        return _Tracked(provider, self, ctx, purpose)

    def describe(self, ctx: Ctx) -> Dict[str, Any]:
        """What a workspace would get, without secrets."""
        provider = self._provider()
        chosen = self.for_ctx(ctx, "describe")
        return {"configured": self.configured, "available": provider.describe(),
                "workspace_allows_external": ctx.ai_external_allowed, "in_use": chosen.describe()}

    def record(self, ctx: Ctx, provider: str, operation: str, error: Optional[str], latency_ms: float) -> None:
        try:
            self.platform.store.insert(ctx.as_system(), "usage_events", {
                "provider": f"ai:{provider}"[:60], "operation": operation[:100], "units": 1,
                "success": error is None, "latency_ms": latency_ms, "error": (error or None) and error[:1000]})
        except Exception:  # noqa: BLE001 - usage logging must never break the call
            log.exception("could not record AI usage")
