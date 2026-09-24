"""Choosing an AI provider for one call, under the workspace's configuration and data policy.

``AIRegistry(platform).for_ctx(ctx, purpose)`` returns a real provider only when
**all** of these hold, and :class:`~cloud.intel.ai.rules.RulesProvider` (which
makes every caller take its deterministic path) otherwise:

1. AI is enabled for the workspace (``settings.ai.enabled``, default on once a
   provider is chosen);
2. a provider is configured — the workspace's ``settings.ai.provider`` or the
   server default ``CAREERCLOUD_AI_PROVIDER`` — and it is not ``rules``;
3. the purpose is one of the workspace's ``allowed_actions``;
4. the workspace allows its data to be sent to external AI
   (``ai_external_allowed``, the data policy);
5. this month's recorded AI spend is below ``max_budget_usd`` (when set);
6. a key exists: the workspace's own key (saved in Settings, Fernet-encrypted,
   never shown again) or else the server's environment variable.

The provider handed back is wrapped so that every call

* has emails and phone numbers redacted from the prompt unless the purpose is
  in :data:`PII_PURPOSES` (e.g. personalisation, which needs a name);
* records one ``ai_usage`` row — provider, model, purpose, prompt/completion
  tokens, estimated cost, request id, latency, success or error — in the
  workspace, via the system scope. Keys are never recorded.

**Fallbacks.** A workspace may list ``fallbacks`` (``[{"provider": "claude"}]``).
They are used **only** when listed, and only when the primary is unavailable or
failing transiently — never after a refusal.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from cloud.intel.ai.base import AIError, AIProvider, AIRefused, AIResult, AIRetryable, AIUnavailable
from cloud.intel.ai.rules import RulesProvider
from cloud.intel.core.context import Ctx

__all__ = ["AIRegistry", "AI_ACTIONS", "FallbackProvider", "PII_PURPOSES", "PROVIDERS", "redact"]

log = logging.getLogger(__name__)

#: Purposes allowed to see contact details in the prompt.
PII_PURPOSES = frozenset({"personalization"})

#: The AI actions a workspace can allow or forbid, with the purposes that map onto them.
AI_ACTIONS: Dict[str, str] = {
    "intent_interpretation": "Understand what a request asks for",
    "research_planning": "Propose research plans (tool calls the server validates)",
    "plan_explanation": "Explain a plan in plain language",
    "result_summarization": "Summarise results",
    "follow_up_understanding": "Understand follow-up messages in a conversation",
    "extraction": "Extract fields from scraped pages (AI scraper)",
    "personalization": "Personalise outreach drafts",
}
_PURPOSE_ALIASES = {"agent_planning": "research_planning", "describe": "describe"}

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "claude": {"label": "Claude (Anthropic)", "env": ["ANTHROPIC_API_KEY"], "default_model": "claude-opus-5"},
    "gemini": {"label": "Gemini (Google)", "env": ["GEMINI_API_KEY"], "default_model": "gemini-2.5-pro"},
    "openai_compatible": {"label": "OpenAI-compatible", "env": ["OPENAI_COMPATIBLE_API_KEY",
                                                                 "OPENAI_COMPATIBLE_BASE_URL"],
                          "default_model": None},
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


def redact(text: str) -> str:
    """Replace email addresses and phone numbers with placeholders."""
    text = _EMAIL.sub("[email]", text or "")
    return _PHONE.sub("[phone]", text)


def _build(name: str, model: Optional[str], secrets: Optional[Mapping[str, str]] = None,
           settings: Optional[Mapping[str, Any]] = None) -> AIProvider:
    secrets, settings = dict(secrets or {}), dict(settings or {})
    prices = {k: tuple(v) for k, v in (settings.get("prices") or {}).items() if isinstance(v, (list, tuple))}
    if name == "claude":
        from cloud.intel.ai.claude import ClaudeProvider

        return ClaudeProvider(model=model, api_key=secrets.get("api_key"))
    if name == "gemini":
        from cloud.intel.ai.rest import GeminiProvider

        return GeminiProvider(model=model, api_key=secrets.get("api_key"), prices=prices)
    if name in ("openai_compatible", "openai"):
        from cloud.intel.ai.rest import OpenAICompatibleProvider

        return OpenAICompatibleProvider(model=model, api_key=secrets.get("api_key"),
                                        base_url=settings.get("base_url"), prices=prices)
    raise AIUnavailable(f"unknown AI provider {name!r}")


class _Tracked(AIProvider):
    """Redacts prompts and records usage (tokens, cost, request id) around a real provider."""

    def __init__(self, inner: AIProvider, registry: "AIRegistry", ctx: Ctx, purpose: str,
                 run_id: Optional[str] = None) -> None:
        self.inner, self.registry, self.ctx, self.purpose, self.run_id = inner, registry, ctx, purpose, run_id
        self.name, self.external, self.model = inner.name, inner.external, inner.model

    def _prep(self, text: str) -> str:
        return text if self.purpose in PII_PURPOSES else redact(text)

    def _run(self, fn: Callable[[], AIResult]) -> AIResult:
        started = time.monotonic()
        error: Optional[str] = None
        result: Optional[AIResult] = None
        try:
            result = fn()
            return result
        except AIError as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            usage = (result.usage if result is not None else None) or getattr(self.inner, "last_usage", None)
            self.last_usage, self.last_error = usage, error
            self.registry.record(self.ctx, self.inner, self.purpose, usage, error,
                                 (time.monotonic() - started) * 1000, run_id=self.run_id)

    def generate(self, system: str, prompt: str, *, max_tokens: int = 2000) -> AIResult:
        return self._run(lambda: self.inner.generate(system, self._prep(prompt), max_tokens=max_tokens))

    def structured_generate(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                            max_tokens: int = 4000) -> AIResult:
        return self._run(lambda: self.inner.structured_generate(system, self._prep(prompt), schema,
                                                                max_tokens=max_tokens))

    def stream(self, system: str, prompt: str, *, max_tokens: int = 2000) -> Iterator[str]:
        started = time.monotonic()
        error = None
        try:
            yield from self.inner.stream(system, self._prep(prompt), max_tokens=max_tokens)
        except AIError as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.registry.record(self.ctx, self.inner, self.purpose, getattr(self.inner, "last_usage", None),
                                 error, (time.monotonic() - started) * 1000, run_id=self.run_id)

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        return self.structured_generate(system, prompt, schema, max_tokens=max_tokens).data or {}

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self.generate(system, prompt, max_tokens=max_tokens).text

    def health_check(self, *, live: bool = False) -> Dict[str, Any]:
        return self.inner.health_check(live=live)

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
                value = call(provider)
                self.last_usage = getattr(provider, "last_usage", None)
                return value
            except AIRefused:
                raise
            except (AIUnavailable, AIRetryable) as error:
                log.warning("AI provider %s failed (%s); trying the next configured fallback", provider.name, error)
                last = error
        raise last or AIUnavailable("no AI provider available")

    def generate(self, system: str, prompt: str, *, max_tokens: int = 2000) -> AIResult:
        return self._try(lambda p: p.generate(system, prompt, max_tokens=max_tokens))

    def structured_generate(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                            max_tokens: int = 4000) -> AIResult:
        return self._try(lambda p: p.structured_generate(system, prompt, schema, max_tokens=max_tokens))

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        return self._try(lambda p: p.complete_json(system, prompt, schema, max_tokens=max_tokens))

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self._try(lambda p: p.complete_text(system, prompt, max_tokens=max_tokens))

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "external": self.external, "chain": [p.describe() for p in self.providers]}


class AIRegistry:
    def __init__(self, platform: Any, *, factory: Optional[Callable[..., AIProvider]] = None) -> None:
        self.platform = platform
        self._factory = factory or _build
        self._cache: Dict[str, AIProvider] = {}

    @property
    def configured(self) -> str:
        return (self.platform.config.ai_provider or "rules").lower()

    # --- configuration ------------------------------------------------------------------

    def workspace_config(self, ctx: Ctx) -> Dict[str, Any]:
        lookup = getattr(self.platform.store, "system_membership", None)
        info = lookup(ctx.workspace_id) if callable(lookup) else None
        ai = ((info or {}).get("settings") or {}).get("ai") or {}
        provider = ai.get("provider")
        return {
            "provider": provider,
            "model": ai.get("model"),
            "enabled": bool(ai.get("enabled", True)),
            "fallbacks": list(ai.get("fallbacks") or []),
            "max_budget_usd": ai.get("max_budget_usd"),
            "allowed_actions": list(ai.get("allowed_actions") or AI_ACTIONS),
        }

    def _secrets(self, ctx: Ctx, name: str) -> Dict[str, Any]:
        """The workspace's own key for ``name`` (decrypted, server-side only), or {}."""
        try:
            registry = self.platform.service("providers")
            secrets = registry.get_secrets(ctx.as_system(), name)
            settings = registry._settings(ctx.as_system(), name)  # noqa: SLF001
            return {"secrets": secrets, "settings": settings}
        except Exception:  # noqa: BLE001 - no key store configured: the server environment is used
            return {"secrets": {}, "settings": {}}

    def key_source(self, ctx: Ctx, name: str) -> Optional[str]:
        """``workspace`` / ``server`` / None — never the key itself."""
        import os

        if name not in PROVIDERS:
            return None
        if self._secrets(ctx, name)["secrets"].get("api_key"):
            return "workspace"
        return "server" if all(os.environ.get(v) for v in PROVIDERS[name]["env"]) else None

    def _named(self, name: str, model: Optional[str], ctx: Optional[Ctx] = None) -> AIProvider:
        name = (name or "rules").lower()
        if name == "rules":
            return RulesProvider("AI provider not configured")
        found = self._secrets(ctx, name) if ctx is not None else {"secrets": {}, "settings": {}}
        fingerprint = hashlib.sha256(repr(sorted(found["secrets"].items())).encode()).hexdigest()[:12]
        key = f"{ctx.workspace_id if ctx else '-'}:{name}:{model or ''}:{fingerprint}"
        if key not in self._cache:
            try:
                self._cache[key] = self._factory(name, model, found["secrets"], found["settings"])
            except TypeError:  # a two-argument factory (tests, older wiring)
                try:
                    self._cache[key] = self._factory(name, model)
                except AIUnavailable as error:
                    return RulesProvider(str(error))
            except AIUnavailable as error:
                log.info("AI provider %s unavailable: %s", name, error)
                return RulesProvider(str(error))
        return self._cache[key]

    def _resolve(self, name: str, model: Optional[str], ctx: Ctx) -> AIProvider:
        try:
            return self._named(name, model, ctx)
        except TypeError:  # a replaced two-argument _named (tests)
            return self._named(name, model)

    def _provider(self) -> AIProvider:
        return self._named(self.configured, self.platform.config.ai_model)

    def month_spend(self, ctx: Ctx) -> float:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = self.platform.store.all(ctx.as_system(), "ai_usage", {"created_at__gte": start}, cap=100_000)
        return round(sum(float(r["estimated_cost_usd"] or 0) for r in rows), 6)

    def for_ctx(self, ctx: Ctx, purpose: str, *, run_id: Optional[str] = None) -> AIProvider:
        config = self.workspace_config(ctx)
        action = _PURPOSE_ALIASES.get(purpose, purpose)
        name = (config["provider"] or self.configured or "rules").lower()
        if name == "rules":
            return RulesProvider("AI provider not configured")
        if not config["enabled"]:
            return RulesProvider("AI is turned off for this workspace")
        if action != "describe" and action not in config["allowed_actions"]:
            return RulesProvider(f"the workspace does not allow AI for {action.replace('_', ' ')}")
        if not ctx.ai_external_allowed:
            return RulesProvider("this workspace has not allowed its data to be sent to external AI providers")
        budget = config["max_budget_usd"]
        if budget is not None and action != "describe" and self.month_spend(ctx) >= float(budget):
            return RulesProvider(f"this month's AI budget of ${float(budget):g} has been reached")
        model = config["model"] if config["provider"] else self.platform.config.ai_model
        provider = self._resolve(name, model or (config["model"] if not config["provider"] else None), ctx)
        chain = [provider] + [self._resolve(f.get("provider", ""), f.get("model"), ctx) for f in config["fallbacks"]
                              if isinstance(f, Mapping) and f.get("provider")]
        chain = [p for p in chain if p.external] or [provider]
        if len(chain) > 1:
            provider = FallbackProvider(chain)
        if not provider.external:
            return provider
        return _Tracked(provider, self, ctx, action, run_id=run_id)

    def status(self, ctx: Ctx) -> Dict[str, Any]:
        """What the workspace would get and why — safe to show in the UI (no key values)."""
        config = self.workspace_config(ctx)
        name = (config["provider"] or self.configured or "rules").lower()
        chosen = self.for_ctx(ctx, "research_planning")
        return {
            "configured": name != "rules",
            "provider": name,
            "model": (config["model"] or self.platform.config.ai_model
                      or (PROVIDERS.get(name) or {}).get("default_model")),
            "enabled": config["enabled"],
            "key_present": self.key_source(ctx, name) is not None,
            "key_source": self.key_source(ctx, name),
            "external_allowed": ctx.ai_external_allowed,
            "active": chosen.external,
            "reason": None if chosen.external else getattr(chosen, "reason", "AI provider not configured"),
            "fallbacks": config["fallbacks"],
            "max_budget_usd": config["max_budget_usd"],
            "spent_this_month_usd": self.month_spend(ctx),
            "allowed_actions": config["allowed_actions"],
            "actions": AI_ACTIONS,
            "providers": {k: {"label": v["label"], "default_model": v["default_model"],
                              "key_present": self.key_source(ctx, k) is not None} for k, v in PROVIDERS.items()},
        }

    def describe(self, ctx: Ctx) -> Dict[str, Any]:
        """What a workspace would get, without secrets."""
        provider = self._provider()
        chosen = self.for_ctx(ctx, "describe")
        return {"configured": self.configured, "available": provider.describe(),
                "workspace_allows_external": ctx.ai_external_allowed, "in_use": chosen.describe()}

    def usage(self, ctx: Ctx, *, limit: int = 100) -> Dict[str, Any]:
        rows = self.platform.store.list(ctx, "ai_usage", {}, order="-created_at", limit=limit).rows
        totals: Dict[str, Dict[str, float]] = {}
        for row in self.platform.store.all(ctx, "ai_usage", {}, cap=100_000):
            t = totals.setdefault(f"{row['provider']}:{row['model']}", {"calls": 0, "prompt_tokens": 0,
                                                                        "completion_tokens": 0, "cost_usd": 0.0})
            t["calls"] += 1
            t["prompt_tokens"] += row["prompt_tokens"] or 0
            t["completion_tokens"] += row["completion_tokens"] or 0
            t["cost_usd"] = round(t["cost_usd"] + float(row["estimated_cost_usd"] or 0), 6)
        return {"recent": rows, "totals": totals, "spent_this_month_usd": self.month_spend(ctx)}

    def record(self, ctx: Ctx, provider: AIProvider, purpose: str, usage: Any, error: Optional[str],
               latency_ms: float, *, run_id: Optional[str] = None) -> None:
        try:
            self.platform.store.insert(ctx.as_system(), "ai_usage", {
                "provider": (getattr(usage, "provider", None) or provider.name)[:60],
                "model": (getattr(usage, "model", None) or provider.model or "unknown")[:120],
                "purpose": purpose[:60], "success": error is None, "error": (error or None) and error[:1000],
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None) if usage is not None else None,
                "estimated_cost_usd": getattr(usage, "estimated_cost_usd", None),
                "request_id": (getattr(usage, "request_id", None) or None),
                "latency_ms": latency_ms, "run_id": run_id})
            # The generic provider-usage stream (analytics, source health) keeps its row too.
            self.platform.store.insert(ctx.as_system(), "usage_events", {
                "provider": f"ai:{provider.name}"[:60], "operation": f"ai:{purpose}"[:100], "units": 1,
                "success": error is None, "latency_ms": latency_ms, "error": (error or None) and error[:1000]})
        except Exception:  # noqa: BLE001 - usage logging must never break the call
            log.exception("could not record AI usage")
